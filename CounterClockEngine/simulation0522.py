"""
simulation0522.py
카카오맵 실제 경로 기반 GPS 최적화 시뮬레이션 + 시각화

사용법:
    python3 simulation0522.py

입력:
    출발지 좌표 (lat lon)
    목적지 좌표 (lat lon)
    약속 시간  (HH:MM)
    이동 수단  (vehicle / walking)

출력:
    전체 경로 구간별 GPS 호출 횟수·interval·배터리 (텍스트)
    경로 지도 + 배터리 소모 시각화 (matplotlib)
"""

import os
import sys
import math
from datetime import datetime, timedelta
from dataclasses import dataclass, field

sys.path.insert(0, ".")

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

from gps_api.core.optimizer import (
    LocationPoint, Geofence,
    calculate_next_interval, haversine,
    INTERVAL_MIN_S, INTERVAL_MAX_S,
)
from gps_api.core.kakao_route import fetch_route, resample_route
from gps_api.core.transit_route import (
    fetch_transit_route, resample_transit_route,
    TransitLeg, TRAFFIC_LABEL as TRANSIT_MODE_LABEL,
)

# ── 배터리 모델 ──────────────────────────────────────────────────────
BATTERY_DRAIN_PER_SEC = {
    "HIGH":     12.0 / 3600,
    "BALANCED":  5.0 / 3600,
    "LOW":       1.5 / 3600,
}
NORMAL_INTERVAL_SEC = 5      # 일반 앱: 5초 고정 HIGH
STEP_SEC = 5                 # 시뮬레이션 스텝 간격


# ── 데이터 클래스 ─────────────────────────────────────────────────────

@dataclass
class GPSCall:
    step_idx: int
    elapsed_sec: float
    lat: float
    lon: float
    dist_to_dest: float
    activity: str
    gps_mode: str
    interval: int
    urgency: float
    battery_drain: float
    cum_battery: float


@dataclass
class SimResult:
    mode: str
    calls: list[GPSCall] = field(default_factory=list)
    total_battery: float = 0.0
    total_sec: float = 0.0

    @property
    def n_calls(self): return len(self.calls)

    @property
    def calls_per_min(self):
        return self.n_calls / (self.total_sec / 60) if self.total_sec else 0


# ── 경로 변환 ────────────────────────────────────────────────────────

def _haversine_m(a: tuple, b: tuple) -> float:
    return haversine(a[0], a[1], b[0], b[1])


def kakao_route_to_sim(
    resampled: list[tuple[float, float, str]],
    step_sec: int = STEP_SEC,
) -> list[tuple[float, float, float]]:
    """
    resample_route 결과 [(lat, lon, phase), ...] →
    시뮬레이션용 [(lat, lon, elapsed_sec), ...]
    """
    return [(lat, lon, i * step_sec) for i, (lat, lon, _) in enumerate(resampled)]


def fallback_linear_route(
    origin: tuple[float, float],
    dest: tuple[float, float],
    mode: str = "vehicle",
    step_sec: int = STEP_SEC,
) -> list[tuple[float, float, float]]:
    speed_mps = {"vehicle": 10.0, "transit": 6.0, "walking": 1.4}.get(mode, 1.4)
    total_dist = _haversine_m(origin, dest)
    total_sec = total_dist / speed_mps
    n_steps = max(2, int(total_sec / step_sec))
    route = []
    for i in range(n_steps + 1):
        t = i / n_steps
        lat = origin[0] + (dest[0] - origin[0]) * t
        lon = origin[1] + (dest[1] - origin[1]) * t
        route.append((lat, lon, t * total_sec))
    return route


# ── 시뮬레이션 ───────────────────────────────────────────────────────

def _estimate_speed(history: list[LocationPoint]) -> float | None:
    if len(history) < 2:
        return None
    speeds = []
    for i in range(1, len(history)):
        p, c = history[i - 1], history[i]
        d = haversine(p.lat, p.lon, c.lat, c.lon)
        dt = (c.timestamp - p.timestamp).total_seconds()
        if dt > 0:
            speeds.append(d / dt)
    return sum(speeds) / len(speeds) if speeds else None


def _seed_history(
    origin: tuple[float, float],
    mode: str,
    n: int = 4,
    interval_sec: int = STEP_SEC,
) -> list[LocationPoint]:
    speed = {"vehicle": 10.0, "transit": 6.0, "walking": 1.4}.get(mode, 1.4)
    dlat_per_step = (speed * interval_sec) / 111_000
    history = []
    base_time = datetime.now() - timedelta(seconds=n * interval_sec)
    for i in range(n):
        lat = origin[0] - dlat_per_step * (n - i)
        lon = origin[1]
        ts = base_time + timedelta(seconds=i * interval_sec)
        history.append(LocationPoint(lat, lon, ts))
    return history


def run_optimized(
    route: list[tuple],
    dest: tuple[float, float],
    appt_remaining_sec: float,
    mode: str = "vehicle",
) -> SimResult:
    dest_fence = [Geofence("dest", dest[0], dest[1], 100)]
    result = SimResult(mode="optimized", total_sec=route[-1][2])
    history: list[LocationPoint] = _seed_history((route[0][0], route[0][1]), mode)
    cum_battery = 0.0
    next_call_at = 0.0

    for idx, (lat, lon, elapsed) in enumerate(route):
        if elapsed < next_call_at:
            continue

        ts = datetime.now() + timedelta(seconds=elapsed)
        pt = LocationPoint(lat, lon, ts)
        history.append(pt)
        if len(history) > 5:
            history.pop(0)

        appt_rem = max(appt_remaining_sec - elapsed, 1.0)
        dist_left = _haversine_m((lat, lon), dest)
        avg_spd = _estimate_speed(history) or 1.4
        eta_sec = dist_left / avg_spd if avg_spd > 0 else None

        r = calculate_next_interval(
            lat, lon, history, dest_fence,
            eta_sec=eta_sec,
            appointment_remaining_sec=appt_rem,
        )

        drain = BATTERY_DRAIN_PER_SEC[r.gps_mode] * r.next_interval
        cum_battery += drain

        result.calls.append(GPSCall(
            step_idx=idx,
            elapsed_sec=elapsed,
            lat=lat, lon=lon,
            dist_to_dest=dist_left,
            activity=r.activity,
            gps_mode=r.gps_mode,
            interval=r.next_interval,
            urgency=r.urgency,
            battery_drain=drain,
            cum_battery=cum_battery,
        ))

        next_call_at = elapsed + r.next_interval

    result.total_battery = cum_battery
    return result


def run_normal(route: list[tuple], dest: tuple[float, float]) -> SimResult:
    total_sec = route[-1][2]
    result = SimResult(mode="normal", total_sec=total_sec)
    n_calls = int(total_sec / NORMAL_INTERVAL_SEC)
    drain_each = BATTERY_DRAIN_PER_SEC["HIGH"] * NORMAL_INTERVAL_SEC
    total = drain_each * n_calls

    for i in range(n_calls):
        elapsed = i * NORMAL_INTERVAL_SEC
        t = elapsed / total_sec if total_sec else 0
        lat = route[0][0] + (route[-1][0] - route[0][0]) * t
        lon = route[0][1] + (route[-1][1] - route[0][1]) * t
        result.calls.append(GPSCall(
            step_idx=i, elapsed_sec=elapsed,
            lat=lat, lon=lon,
            dist_to_dest=0, activity="—",
            gps_mode="HIGH", interval=NORMAL_INTERVAL_SEC,
            urgency=0, battery_drain=drain_each,
            cum_battery=drain_each * (i + 1),
        ))

    result.total_battery = total
    return result


# ── 텍스트 출력 ──────────────────────────────────────────────────────

def fmt_time(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def _urgency_bar(urgency: float, width: int = 12) -> str:
    filled = round(urgency * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def print_route_table(calls: list[GPSCall]):
    print(f"\n  {'#':>4}  {'경과':>6}  {'목적지까지':>10}  {'Activity':>10}  "
          f"{'긴급도':>14}  {'mode':>10}  {'interval':>10}")
    print(f"  {'-'*4}  {'-'*6}  {'-'*10}  {'-'*10}  {'-'*14}  {'-'*10}  {'-'*10}")

    for i, c in enumerate(calls):
        if len(calls) > 40:
            if i == 15:
                print(f"\n  ... (중략 {len(calls) - 25}건) ...\n")
            if 15 <= i < len(calls) - 10:
                continue

        mode_sym = {"HIGH": "[!]", "BALANCED": "[~]", "LOW": "[ ]"}.get(c.gps_mode, "[ ]")
        print(
            f"  {i+1:>4}  {fmt_time(c.elapsed_sec):>6}  {c.dist_to_dest:>9.0f}m"
            f"  {c.activity:>10}  {_urgency_bar(c.urgency)} {c.urgency:.2f}"
            f"  {mode_sym}{c.gps_mode:<7}  {c.interval:>8}초"
        )


def print_summary(normal: SimResult, opt: SimResult, dist_m: float, mode: str):
    save_bat = (normal.total_battery - opt.total_battery) / normal.total_battery * 100
    save_calls = (normal.n_calls - opt.n_calls) / normal.n_calls * 100
    total_min = opt.total_sec / 60
    dist_km = dist_m / 1000

    print(f"\n{'=' * 64}")
    print(f"  시뮬레이션 요약")
    print(f"{'=' * 64}")
    print(f"  거리: {dist_km:.2f} km  /  예상 소요: {total_min:.1f}분  /  이동수단: {mode}")
    print(f"  {'':25}  {'일반 모드(5초)':>14}  {'최적화 모드':>12}")
    print(f"  {'-'*25}  {'-'*14}  {'-'*12}")
    print(f"  {'GPS 호출 횟수':<25}  {normal.n_calls:>14,}회  {opt.n_calls:>12,}회")
    print(f"  {'분당 GPS 호출':<25}  {normal.calls_per_min:>13.1f}회  {opt.calls_per_min:>11.1f}회")
    print(f"  {'배터리 소모':<25}  {normal.total_battery:>13.4f}%  {opt.total_battery:>11.4f}%")
    print(f"{'=' * 64}")
    print(f"  GPS 호출 감소: {save_calls:+.1f}%    배터리 절약: {save_bat:+.1f}%")
    print(f"{'=' * 64}")

    mode_cnt: dict[str, int] = {}
    for c in opt.calls:
        mode_cnt[c.gps_mode] = mode_cnt.get(c.gps_mode, 0) + 1
    total = sum(mode_cnt.values()) or 1
    print(f"\n  [최적화] GPS 모드 분포:")
    for m in ["HIGH", "BALANCED", "LOW"]:
        cnt = mode_cnt.get(m, 0)
        pct = cnt / total * 100
        bar = "█" * int(pct / 4)
        desc = {
            "HIGH":     "interval<10s  (목적지 근접 + 시간 촉박)",
            "BALANCED": "interval 10~30s  (중간 거리)",
            "LOW":      "interval≥30s  (여유 있음)",
        }
        print(f"    {m:<10} {bar:<25} {pct:5.1f}%  {desc.get(m,'')}")

    print(f"\n  * HIGH 모드 조건: 목적지 경계 100m 이내 + ETA ≈ 남은 약속 시간")


# ── 시각화 ───────────────────────────────────────────────────────────

MODE_COLOR = {"HIGH": "#e74c3c", "BALANCED": "#f39c12", "LOW": "#2ecc71"}
MODE_LABEL = {"HIGH": "HIGH (빠른 갱신)", "BALANCED": "BALANCED (보통)", "LOW": "LOW (절약)"}


TRANSIT_COLOR = {
    "WALK":   "#aaaaaa",
    "BUS":    "#3498db",
    "SUBWAY": "#9b59b6",
    "TRAIN":  "#1abc9c",
}


def visualize(
    raw_coords: list[tuple[float, float]],
    opt: SimResult,
    normal: SimResult,
    origin: tuple[float, float],
    dest: tuple[float, float],
    appt_time: datetime,
    mode: str,
    dist_m: float,
    transit_legs: list | None = None,
    save_path: str | None = None,
):
    try:
        import matplotlib
        matplotlib.use("Agg" if save_path else "TkAgg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        import matplotlib.patches as mpatches
        from matplotlib.collections import LineCollection
        import numpy as np
    except ImportError:
        return

    fig = plt.figure(figsize=(16, 12))
    fig.patch.set_facecolor("#1a1a2e")
    gs = gridspec.GridSpec(2, 2, figure=fig, hspace=0.38, wspace=0.32)

    # ── 공통 스타일 ──
    TITLE_KW = dict(color="white", fontsize=11, fontweight="bold", pad=8)
    TICK_KW  = dict(colors="#aaaaaa", labelsize=8)

    def _style_ax(ax):
        ax.set_facecolor("#16213e")
        ax.tick_params(axis="x", **TICK_KW)
        ax.tick_params(axis="y", **TICK_KW)
        for sp in ax.spines.values():
            sp.set_edgecolor("#444466")

    # ── [1] 경로 지도 ──────────────────────────────────────────────
    ax_map = fig.add_subplot(gs[0, 0])
    _style_ax(ax_map)
    ax_map.set_title("경로 지도 & GPS 호출 모드", **TITLE_KW)

    # 전체 경로 선 (대중교통은 구간별 색상)
    if transit_legs:
        for leg in transit_legs:
            if len(leg.coords) >= 2:
                lats = [c[0] for c in leg.coords]
                lons = [c[1] for c in leg.coords]
                color = TRANSIT_COLOR.get(leg.mode, "#334466")
                label = TRANSIT_MODE_LABEL.get(leg.mode, leg.mode)
                ax_map.plot(lons, lats, color=color, linewidth=2.0, zorder=1,
                            label=label, alpha=0.7)
    elif raw_coords:
        lats = [c[0] for c in raw_coords]
        lons = [c[1] for c in raw_coords]
        ax_map.plot(lons, lats, color="#334466", linewidth=1.2, zorder=1)

    # GPS 호출 포인트 (모드별 색상)
    for gps_m in ["LOW", "BALANCED", "HIGH"]:
        pts = [(c.lon, c.lat) for c in opt.calls if c.gps_mode == gps_m]
        if pts:
            xs, ys = zip(*pts)
            ax_map.scatter(xs, ys, c=MODE_COLOR[gps_m], s=18, zorder=3,
                           alpha=0.85, label=MODE_LABEL[gps_m])

    # 출발·도착 마커
    ax_map.scatter([origin[1]], [origin[0]], c="#00d2ff", s=120, marker="^",
                   zorder=5, label="출발지")
    ax_map.scatter([dest[1]], [dest[0]], c="#ff6b6b", s=120, marker="*",
                   zorder=5, label="목적지")

    ax_map.set_xlabel("경도", color="#aaaaaa", fontsize=8)
    ax_map.set_ylabel("위도", color="#aaaaaa", fontsize=8)
    leg = ax_map.legend(fontsize=7, facecolor="#16213e", edgecolor="#444466",
                        labelcolor="white", loc="best")

    # ── [2] 누적 배터리 소모 ───────────────────────────────────────
    ax_bat = fig.add_subplot(gs[0, 1])
    _style_ax(ax_bat)
    ax_bat.set_title("누적 배터리 소모 비교", **TITLE_KW)

    # 최적화 모드 — 구간별 색상
    prev_c = None
    for c in opt.calls:
        if prev_c is not None:
            ax_bat.plot(
                [prev_c.elapsed_sec / 60, c.elapsed_sec / 60],
                [prev_c.cum_battery, c.cum_battery],
                color=MODE_COLOR[c.gps_mode], linewidth=2,
            )
        prev_c = c

    # 일반 모드
    n_times = [c.elapsed_sec / 60 for c in normal.calls]
    n_bats  = [c.cum_battery for c in normal.calls]
    if n_times:
        ax_bat.plot(n_times, n_bats, color="#778899", linewidth=1.2,
                    linestyle="--", alpha=0.7, label=f"일반(5초) {normal.total_battery:.4f}%")

    # 범례용 더미 라인
    for gps_m in ["HIGH", "BALANCED", "LOW"]:
        ax_bat.plot([], [], color=MODE_COLOR[gps_m], linewidth=2,
                    label=f"opt·{gps_m}")

    save_pct = (normal.total_battery - opt.total_battery) / normal.total_battery * 100
    ax_bat.set_xlabel("경과 시간 (분)", color="#aaaaaa", fontsize=8)
    ax_bat.set_ylabel("누적 배터리 소모 (%)", color="#aaaaaa", fontsize=8)
    ax_bat.legend(fontsize=7, facecolor="#16213e", edgecolor="#444466",
                  labelcolor="white", loc="upper left")
    ax_bat.text(0.98, 0.05, f"절약: {save_pct:.1f}%",
                transform=ax_bat.transAxes, color="#2ecc71",
                ha="right", va="bottom", fontsize=9, fontweight="bold")

    # ── [3] GPS 호출 간격 타임라인 ────────────────────────────────
    ax_ivl = fig.add_subplot(gs[1, 0])
    _style_ax(ax_ivl)
    ax_ivl.set_title("GPS 호출 간격 (최적화 모드)", **TITLE_KW)

    for c in opt.calls:
        ax_ivl.bar(c.elapsed_sec / 60, c.interval,
                   width=c.interval / 60 * 0.85,
                   color=MODE_COLOR[c.gps_mode], alpha=0.8, align="edge")

    ax_ivl.axhline(NORMAL_INTERVAL_SEC, color="#778899", linewidth=1,
                   linestyle="--", alpha=0.6, label=f"일반 고정 {NORMAL_INTERVAL_SEC}s")
    ax_ivl.set_xlabel("경과 시간 (분)", color="#aaaaaa", fontsize=8)
    ax_ivl.set_ylabel("interval (초)", color="#aaaaaa", fontsize=8)
    ax_ivl.legend(fontsize=7, facecolor="#16213e", edgecolor="#444466", labelcolor="white")

    # ── [4] 긴급도 & 모드 파이 ────────────────────────────────────
    ax_pie = fig.add_subplot(gs[1, 1])
    ax_pie.set_facecolor("#16213e")
    ax_pie.set_title("GPS 모드 분포 (최적화)", **TITLE_KW)

    mode_cnt = {}
    for c in opt.calls:
        mode_cnt[c.gps_mode] = mode_cnt.get(c.gps_mode, 0) + 1

    labels, sizes, colors = [], [], []
    for m in ["HIGH", "BALANCED", "LOW"]:
        if mode_cnt.get(m, 0):
            labels.append(f"{m}\n({mode_cnt[m]}회)")
            sizes.append(mode_cnt[m])
            colors.append(MODE_COLOR[m])

    if sizes:
        wedges, texts, autotexts = ax_pie.pie(
            sizes, labels=labels, colors=colors,
            autopct="%1.1f%%", startangle=90,
            textprops={"color": "white", "fontsize": 8},
            wedgeprops={"edgecolor": "#1a1a2e", "linewidth": 1.5},
        )
        for at in autotexts:
            at.set_fontsize(8)
            at.set_color("white")

    # ── 전체 제목 ──
    appt_str = appt_time.strftime("%H:%M") if appt_time else "미지정"
    dist_km  = dist_m / 1000
    total_min = opt.total_sec / 60
    fig.suptitle(
        f"CounterClock GPS 최적화 시뮬레이션  |  {mode}  |  "
        f"{dist_km:.2f}km / {total_min:.0f}분  |  약속 {appt_str}",
        color="white", fontsize=12, fontweight="bold", y=0.98,
    )

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        print(f"\n  [저장] {save_path}")
    else:
        plt.tight_layout()
        plt.show()

    plt.close(fig)


# ── 입력 헬퍼 ────────────────────────────────────────────────────────

def ask_coord(prompt: str) -> tuple[float, float]:
    while True:
        raw = input(f"  {prompt} (lat lon): ").strip().split()
        if len(raw) == 2:
            try:
                return float(raw[0]), float(raw[1])
            except ValueError:
                pass
        print("  형식 오류. 예) 37.4979 127.0276")


def ask_appt_time() -> datetime:
    """약속 시간을 HH:MM 형식으로 입력받아 오늘 날짜 datetime 반환."""
    while True:
        raw = input("  약속 시간 (HH:MM, Enter=1시간 후): ").strip()
        if not raw:
            return datetime.now() + timedelta(hours=1)
        try:
            t = datetime.strptime(raw, "%H:%M")
            appt = datetime.now().replace(
                hour=t.hour, minute=t.minute, second=0, microsecond=0
            )
            # 이미 지났으면 내일로
            if appt < datetime.now():
                appt += timedelta(days=1)
            return appt
        except ValueError:
            print("  형식 오류. 예) 14:30")


def ask_mode() -> str:
    while True:
        raw = input("  이동 수단 [vehicle / transit / walking] (Enter=vehicle): ").strip().lower()
        if raw in ("vehicle", "v", ""):
            return "vehicle"
        if raw in ("transit", "t", "public", "대중교통"):
            return "transit"
        if raw in ("walking", "walk", "w"):
            return "walking"
        print("  vehicle / transit / walking 중 하나를 입력하세요.")


def ask_save() -> str | None:
    raw = input("  결과 이미지 저장 경로 (Enter=화면 출력): ").strip()
    return raw if raw else None


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    print(f"\n{'#' * 64}")
    print(f"  CounterClock GPS 최적화 시뮬레이션 (카카오맵 경로)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'#' * 64}\n")

    api_key       = os.getenv("KAKAO_API_KEY", "").strip()
    odsay_api_key = os.getenv("ODSAY_API_KEY", "").strip()

    if api_key:
        print(f"  KAKAO_API_KEY 로드 완료 ({api_key[:6]}...)")
    else:
        print("  [경고] KAKAO_API_KEY 환경변수 미설정 — 직선 보간 경로로 대체합니다.\n")
    if odsay_api_key:
        print(f"  ODSAY_API_KEY  로드 완료 ({odsay_api_key[:6]}...)")
    else:
        print("  [안내] ODSAY_API_KEY 없음 — transit 선택 시 카카오 자동차 경로(×1.8) 근사 사용")

    print("[입력]")
    origin = ask_coord("출발지")
    dest   = ask_coord("목적지")
    appt_time = ask_appt_time()
    mode   = ask_mode()
    save_path = ask_save()

    now = datetime.now()
    appt_remaining_sec = (appt_time - now).total_seconds()

    dist_m = _haversine_m(origin, dest)
    print(f"\n  직선 거리: {dist_m:.0f}m")
    print(f"  약속 시간: {appt_time.strftime('%Y-%m-%d %H:%M')}  "
          f"(지금부터 {appt_remaining_sec/60:.1f}분 후)")

    # ── 카카오맵 경로 호출 ──
    raw_coords: list[tuple[float, float]] = []
    route: list[tuple[float, float, float]] = []
    transit_legs: list[TransitLeg] = []
    kakao_dist_m = dist_m
    fallback_speed = {"vehicle": 10.0, "transit": 6.0, "walking": 1.4}.get(mode, 1.4)
    kakao_dur_sec = dist_m / fallback_speed

    if api_key:
        try:
            if mode == "transit":
                if odsay_api_key:
                    print("\n  ODSAY 대중교통 경로 조회 중...")
                    coords, kakao_dur_sec, kakao_dist_m, transit_legs = fetch_transit_route(
                        origin[0], origin[1], dest[0], dest[1], odsay_api_key,
                    )
                    raw_coords = coords
                    leg_summary = "  ".join(
                        f"{TRANSIT_MODE_LABEL.get(l.mode, l.mode)}"
                        f"({l.distance_m}m/{l.duration_sec//60}분)"
                        for l in transit_legs
                    )
                    print(f"  → 대중교통 경로: {kakao_dist_m/1000:.2f}km / {kakao_dur_sec/60:.1f}분 / {len(transit_legs)}구간")
                    print(f"     {leg_summary}")
                    resampled = resample_transit_route(transit_legs, kakao_dur_sec, step_sec=STEP_SEC)
                else:
                    # ODSAY 키 없음 → 카카오 자동차 경로 × 1.8 근사
                    TRANSIT_TIME_FACTOR = 1.8
                    print("\n  카카오맵 경로 조회 후 대중교통 근사 적용 중...")
                    coords, car_sec, kakao_dist_m = fetch_route(
                        origin[0], origin[1], dest[0], dest[1], api_key
                    )
                    kakao_dur_sec = int(car_sec * TRANSIT_TIME_FACTOR)
                    raw_coords = coords
                    transit_legs = []
                    print(f"  → 자동차 {car_sec/60:.1f}분 × {TRANSIT_TIME_FACTOR} = {kakao_dur_sec/60:.1f}분 (근사)")
                    resampled = resample_route(coords, kakao_dur_sec, step_sec=STEP_SEC)
                    resampled = [(lat, lon, "TRANSIT") for lat, lon, _ in resampled]

                route = kakao_route_to_sim(resampled, step_sec=STEP_SEC)
                print(f"  → 리샘플링: {len(route)}개 시뮬레이션 포인트")

            else:
                print("\n  카카오맵 경로 조회 중...")
                coords, kakao_dur_sec, kakao_dist_m = fetch_route(
                    origin[0], origin[1], dest[0], dest[1], api_key
                )
                raw_coords = coords
                print(f"  → 실제 경로: {kakao_dist_m/1000:.2f}km / {kakao_dur_sec/60:.1f}분 / {len(coords)}개 좌표")

                resampled = resample_route(coords, kakao_dur_sec, step_sec=STEP_SEC)
                route = kakao_route_to_sim(resampled, step_sec=STEP_SEC)
                print(f"  → 리샘플링: {len(route)}개 시뮬레이션 포인트")

        except Exception as e:
            print(f"  [카카오 API 오류] {e}")
            print("  → 직선 보간 경로로 대체합니다.")

    if not route:
        print("\n  직선 보간 경로 생성 중...")
        route = fallback_linear_route(origin, dest, mode=mode)
        raw_coords = [(lat, lon) for lat, lon, _ in route]
        kakao_dist_m = dist_m
        kakao_dur_sec = route[-1][2]
        print(f"  → {len(route)}개 포인트 ({kakao_dur_sec:.0f}초)")

    # 약속 시간이 예상 소요 시간보다 너무 짧으면 경고
    if appt_remaining_sec < kakao_dur_sec:
        print(f"\n  ⚠️  약속까지 {appt_remaining_sec/60:.1f}분밖에 없는데 "
              f"예상 소요 {kakao_dur_sec/60:.1f}분입니다 — 지각 위험!")

    # ── 시뮬레이션 실행 ──
    print("\n  최적화 모드 시뮬레이션 중...")
    opt = run_optimized(route, dest, appt_remaining_sec, mode=mode)

    print("  일반 모드 계산 중...")
    normal = run_normal(route, dest)

    # ── 텍스트 결과 ──
    print(f"\n{'=' * 64}")
    print(f"  전체 경로 GPS 호출 상세 (최적화 모드)")
    print(f"{'=' * 64}")
    print(f"  범례: [!]HIGH(빠른 갱신)  [~]BALANCED  [ ]LOW(절약)")
    print_route_table(opt.calls)
    print_summary(normal, opt, kakao_dist_m, mode)

    # ── 시각화 ──
    print("\n  시각화 생성 중...")
    visualize(
        raw_coords=raw_coords,
        opt=opt,
        normal=normal,
        origin=origin,
        dest=dest,
        appt_time=appt_time,
        mode=mode,
        dist_m=kakao_dist_m,
        transit_legs=transit_legs or None,
        save_path=save_path,
    )

    print()


if __name__ == "__main__":
    main()
