"""
GPS 시뮬레이션 + 배터리 절약률 측정
- 실제 이동 경로를 재현하여 일반 모드 vs 최적화 모드 비교
- 시나리오: 강남역 → 삼성역 버스 이동 후 삼성역 근처 정지
"""

import sys
import math
import random
from datetime import datetime, timedelta
from dataclasses import dataclass, field

sys.path.insert(0, ".")
from optimizer import (
    haversine, calculate_next_interval,
    LocationPoint, Geofence,
)


# ── 배터리 소모 모델 ───────────────────────────────────────────
# GPS 정확도별 시간당 배터리 소모율 (%)
# 실측 기반 추정값 (Android HIGH_ACCURACY 기준 ~12%/hr)
BATTERY_DRAIN_PER_SEC = {
    "HIGH":     12.0 / 3600,   # ~0.00333 %/s
    "BALANCED":  5.0 / 3600,   # ~0.00139 %/s
    "LOW":       1.5 / 3600,   # ~0.00042 %/s
    "OFF":       0.1 / 3600,   # 거의 없음 (백그라운드 최소)
}

# 일반 앱(카카오맵 내비) 모드: 항상 HIGH, 1초 간격
NORMAL_MODE_DRAIN = BATTERY_DRAIN_PER_SEC["HIGH"]


@dataclass
class SimulationLog:
    timestamp: datetime
    lat: float
    lon: float
    gps_mode: str
    interval: int
    battery_drain: float   # 누적 배터리 소모 (%)
    distance_to_fence: float
    activity: str


@dataclass
class SimulationResult:
    mode: str
    duration_sec: int
    total_battery_drain: float    # %
    total_gps_calls: int
    logs: list[SimulationLog] = field(default_factory=list)

    @property
    def battery_per_hour(self) -> float:
        return self.total_battery_drain / (self.duration_sec / 3600)

    @property
    def calls_per_min(self) -> float:
        return self.total_gps_calls / (self.duration_sec / 60)


# ── 이동 경로 생성 ─────────────────────────────────────────────
def generate_route(noise: float = 0.00002) -> list[tuple]:
    """
    강남역 → 삼성역 버스 이동 후 삼성역 근처 정지하는 경로 생성
    총 약 20분 시뮬레이션용 좌표 시퀀스 반환
    (lat, lon, speed_phase)
    """
    route = []

    # Phase 1: 버스로 이동 (약 8분, 강남→삼성)
    start = (37.4980, 127.0210)
    end   = (37.5088, 127.0632)
    steps = 48  # 10초 간격 × 48 = 480초
    for i in range(steps):
        t = i / steps
        lat = start[0] + (end[0] - start[0]) * t + random.uniform(-noise, noise)
        lon = start[1] + (end[1] - start[1]) * t + random.uniform(-noise, noise)
        route.append((lat, lon, "vehicle"))

    # Phase 2: 삼성역 근처 도보 (약 5분)
    base = end
    for i in range(30):
        lat = base[0] + random.uniform(-0.0003, 0.0003)
        lon = base[1] + random.uniform(-0.0003, 0.0003)
        route.append((lat, lon, "walking"))

    # Phase 3: 삼성역 안에서 정지 (약 7분)
    stop = (37.5088, 127.0635)
    for _ in range(42):
        lat = stop[0] + random.uniform(-0.00005, 0.00005)
        lon = stop[1] + random.uniform(-0.00005, 0.00005)
        route.append((lat, lon, "stationary"))

    return route


# ── 일반 모드 시뮬레이션 ──────────────────────────────────────
def simulate_normal_mode(
    route: list[tuple],
    interval_sec: int = 1,
    step_sec: int = 10,
    geofences: list = None,
    departure_time: datetime = None,
) -> SimulationResult:
    """
    기존 내비게이션 앱 방식: 항상 1초마다 HIGH 정확도 GPS 호출
    """
    if geofences is None:
        geofences = [Geofence("samsung", 37.5088, 127.0632, 200)]

    result = SimulationResult(
        mode="normal",
        duration_sec=len(route) * step_sec,
        total_battery_drain=0.0,
        total_gps_calls=0,
    )

    base_time = departure_time if departure_time is not None else datetime.now()

    for i, (lat, lon, _) in enumerate(route):
        t    = base_time + timedelta(seconds=i * step_sec)
        dist = min(
            haversine(lat, lon, f.lat, f.lon) - f.radius
            for f in geofences
        )

        calls_this_step = step_sec // interval_sec
        drain = NORMAL_MODE_DRAIN * step_sec

        result.total_gps_calls += calls_this_step
        result.total_battery_drain += drain
        result.logs.append(SimulationLog(
            timestamp=t, lat=lat, lon=lon,
            gps_mode="HIGH", interval=interval_sec,
            battery_drain=result.total_battery_drain,
            distance_to_fence=max(dist, 0),
            activity="unknown",
        ))

    return result


# ── 최적화 모드 시뮬레이션 ────────────────────────────────────
def simulate_optimized_mode(
    route: list[tuple],
    step_sec: int = 10,
    geofences: list = None,
    departure_time: datetime = None,
) -> SimulationResult:
    """
    우리 서비스 방식: 거리+Activity+SLC 기반 동적 interval
    """
    if geofences is None:
        geofences = [Geofence("samsung", 37.5088, 127.0632, 200)]

    result = SimulationResult(
        mode="optimized",
        duration_sec=len(route) * step_sec,
        total_battery_drain=0.0,
        total_gps_calls=0,
    )

    base_time = departure_time if departure_time is not None else datetime.now()
    history_deque = []

    elapsed = 0
    next_call_at = 0

    for i, (lat, lon, _) in enumerate(route):
        t = base_time + timedelta(seconds=i * step_sec)
        elapsed = i * step_sec

        # 이번 스텝에서 GPS 호출이 예정되어 있는가
        if elapsed < next_call_at:
            continue

        # GPS 호출 수행
        point = LocationPoint(lat, lon, t)
        history_deque.append(point)
        if len(history_deque) > 5:
            history_deque.pop(0)

        opt = calculate_next_interval(lat, lon, history_deque, geofences)
        next_call_at = elapsed + opt.next_interval

        # 배터리 소모: 이번 interval 동안 해당 모드 소모
        drain_rate = BATTERY_DRAIN_PER_SEC[opt.gps_mode]
        drain = drain_rate * opt.next_interval

        result.total_gps_calls += 1
        result.total_battery_drain += drain
        result.logs.append(SimulationLog(
            timestamp=t, lat=lat, lon=lon,
            gps_mode=opt.gps_mode,
            interval=opt.next_interval,
            battery_drain=result.total_battery_drain,
            distance_to_fence=max(opt.distance_to_nearest_fence, 0),
            activity=opt.activity,
        ))

    return result


# ── 비교 리포트 출력 ──────────────────────────────────────────
def print_report(normal: SimulationResult, optimized: SimulationResult, scenario: str = ""):
    saving_battery = (
        (normal.total_battery_drain - optimized.total_battery_drain)
        / normal.total_battery_drain * 100
    )
    saving_calls = (
        (normal.total_gps_calls - optimized.total_gps_calls)
        / normal.total_gps_calls * 100
    )

    duration_min = normal.duration_sec / 60

    print("\n" + "=" * 60)
    print("  GPS 배터리 최적화 시뮬레이션 결과")
    print("=" * 60)
    print(f"  시뮬레이션 시간: {duration_min:.0f}분")
    if scenario:
        print(f"  시나리오: {scenario}")
    print("-" * 60)
    print(f"  {'항목':<25} {'일반 모드':>10} {'최적화 모드':>12}")
    print("-" * 60)
    print(f"  {'배터리 소모 (%)':.<25} {normal.total_battery_drain:>9.3f}% {optimized.total_battery_drain:>11.3f}%")
    print(f"  {'시간당 배터리 소모':.<25} {normal.battery_per_hour:>9.2f}% {optimized.battery_per_hour:>11.2f}%")
    print(f"  {'GPS 호출 횟수':.<25} {normal.total_gps_calls:>10,} {optimized.total_gps_calls:>12,}")
    print(f"  {'분당 GPS 호출':.<25} {normal.calls_per_min:>9.1f}회 {optimized.calls_per_min:>10.1f}회")
    print("=" * 60)
    print(f"  🔋 배터리 절약률:   {saving_battery:>6.1f}%")
    print(f"  📍 GPS 호출 감소율: {saving_calls:>6.1f}%")
    print("=" * 60)

    # GPS 모드 분포
    mode_dist = {}
    for log in optimized.logs:
        mode_dist[log.gps_mode] = mode_dist.get(log.gps_mode, 0) + 1
    total = sum(mode_dist.values())
    print("\n  [최적화 모드] GPS 모드 분포:")
    for mode, cnt in sorted(mode_dist.items()):
        pct = cnt / total * 100
        bar = "█" * int(pct / 5)
        print(f"    {mode:<10} {bar:<20} {pct:5.1f}%")

    # 구간별 interval 샘플
    print("\n  [최적화 모드] interval 샘플 (처음 10개 호출):")
    print(f"    {'시각':>8}  {'거리(m)':>8}  {'Activity':>10}  {'mode':>8}  {'interval':>10}")
    for log in optimized.logs[:10]:
        print(f"    {log.timestamp.strftime('%H:%M:%S'):>8}  "
              f"{log.distance_to_fence:>8.0f}  "
              f"{log.activity:>10}  "
              f"{log.gps_mode:>8}  "
              f"{log.interval:>8}초")

    return saving_battery, saving_calls


# ── 메인 실행 ─────────────────────────────────────────────────
if __name__ == "__main__":
    random.seed(42)  # 재현 가능한 결과

    print("경로 생성 중...")
    route = generate_route()
    print(f"총 {len(route)}개 위치 포인트 생성 ({len(route)*10//60}분 시뮬레이션)")

    print("일반 모드 시뮬레이션...")
    normal = simulate_normal_mode(route)

    print("최적화 모드 시뮬레이션...")
    optimized = simulate_optimized_mode(route)

    saving_battery, saving_calls = print_report(normal, optimized)

    # 검증 기준: 최소 40% 이상 절약되어야 함
    assert saving_battery >= 40.0, f"배터리 절약률 기준 미달: {saving_battery:.1f}% < 40%"
    assert saving_calls   >= 60.0, f"GPS 호출 감소율 기준 미달: {saving_calls:.1f}% < 60%"
    print("\n✅ 검증 통과: 배터리 절약률 및 GPS 호출 감소율 기준 충족")
