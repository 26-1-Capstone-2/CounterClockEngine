"""
verification0522.py
gps_api에 구현된 알고리즘 동작 검증 스크립트

검증 항목:
  1. Cosine Blend Interval  — 거리·시간 긴급도 기반 GPS 갱신 주기 최적화
  2. Sigmoid Activity Mult  — 속도 기반 부드러운 activity multiplier
  3. Significant Location   — 이동 감지로 갱신 주기 조절
  4. Group GPS Interval     — 그룹 참여자별 개인화 주기
  5. 상태 판단 (status)     — on_time / leave_soon / hurry / late
"""

import sys
import math
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from gps_api.core.optimizer import (
    LocationPoint, Geofence,
    calculate_next_interval,
    smooth_activity_multiplier,
    INTERVAL_MIN_S, INTERVAL_MAX_S,
)
from gps_api.core.group import (
    Participant, Group,
    _compute_status, compute_gps_interval,
)
from gps_api.core.journey import Journey, compute_eta

# ── 공통 좌표 ─────────────────────────────────────────────────────────
GANGNAM   = (37.4979, 127.0276)   # 강남역
SAMSUNG   = (37.5088, 127.0632)   # 삼성역 (목적지)
YEOKSAM   = (37.5008, 127.0364)   # 역삼역 (중간)
SEOCHO    = (37.4837, 127.0324)   # 서초역 (먼 곳)

SEP = "=" * 64


def header(title: str):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def subheader(title: str):
    print(f"\n  ── {title} {'─' * (55 - len(title))}")


def make_history(
    points: list[tuple],
    interval_sec: int = 10,
) -> list[LocationPoint]:
    base = datetime(2026, 5, 22, 9, 0, 0)
    return [
        LocationPoint(lat, lon, base + timedelta(seconds=i * interval_sec))
        for i, (lat, lon) in enumerate(points)
    ]


# ══════════════════════════════════════════════════════════════════════
# 1. Cosine Blend Interval — 거리·시간 긴급도 기반 GPS 주기
# ══════════════════════════════════════════════════════════════════════
def verify_cosine_blend():
    header("1. Cosine Blend Interval  (거리 + 시간 긴급도 → GPS 갱신 주기)")

    dest_fence = [Geofence("samsung", SAMSUNG[0], SAMSUNG[1], 200)]

    scenarios = [
        {
            "label": "출발 전  — 멀리 있고 시간 여유 있음",
            "pos": SEOCHO,
            "history": make_history([SEOCHO] * 3),
            "eta_sec": 600,
            "appt_remain": 7200,
        },
        {
            "label": "이동 중  — 중간 지점, 시간 보통",
            "pos": GANGNAM,
            "history": make_history([SEOCHO, YEOKSAM, GANGNAM], interval_sec=30),
            "eta_sec": 300,
            "appt_remain": 1200,
        },
        {
            "label": "거의 도착 — 목적지 근처, 시간 촉박",
            "pos": YEOKSAM,
            "history": make_history([GANGNAM, YEOKSAM], interval_sec=10),
            "eta_sec": 180,
            "appt_remain": 200,
        },
    ]

    print(f"\n  {'시나리오':<30} {'거리(m)':>7} {'u_dist':>7} {'u_time':>7} {'urgency':>8} {'interval':>9} {'mode':>8}")
    print(f"  {'-'*30} {'-'*7} {'-'*7} {'-'*7} {'-'*8} {'-'*9} {'-'*8}")

    for s in scenarios:
        r = calculate_next_interval(
            s["pos"][0], s["pos"][1],
            s["history"], dest_fence,
            eta_sec=s["eta_sec"],
            appointment_remaining_sec=s["appt_remain"],
        )
        print(
            f"  {s['label']:<30} {r.distance_to_nearest_fence:>7.0f}"
            f"  {r.u_dist:>6.3f}  {r.u_time:>6.3f}  {r.urgency:>7.3f}"
            f"  {r.next_interval:>7}초  {r.gps_mode:>8}"
        )

    print(f"\n  → urgency 0(여유) → {INTERVAL_MAX_S}s(LOW),  urgency 1(촉박) → {INTERVAL_MIN_S}s(HIGH)")


# ══════════════════════════════════════════════════════════════════════
# 2. Sigmoid Activity Multiplier — 속도에 따른 부드러운 배율
# ══════════════════════════════════════════════════════════════════════
def verify_sigmoid_activity():
    header("2. Sigmoid Activity Multiplier  (속도 → 배율 매핑)")

    speeds = [
        (0.0,  "완전 정지"),
        (0.3,  "아주 느린 이동"),
        (0.5,  "경계 (stationary→walking)"),
        (1.4,  "도보"),
        (4.0,  "빠른 도보 / 자전거"),
        (8.0,  "경계 (walking→vehicle)"),
        (12.0, "버스"),
        (30.0, "고속 차량"),
    ]

    print(f"\n  {'속도(m/s)':>10}  {'상태':>25}  {'multiplier':>12}  {'의미'}")
    print(f"  {'-'*10}  {'-'*25}  {'-'*12}  {'-'*20}")

    for spd, label in speeds:
        mult = smooth_activity_multiplier(spd)
        if spd < 0.5:
            meaning = "GPS 주기 3배 늘림 (절약)"
        elif spd < 8.0:
            meaning = "기본 주기 유지"
        else:
            meaning = "GPS 주기 0.3배 줄임 (빠른 갱신)"
        print(f"  {spd:>10.1f}  {label:>25}  {mult:>12.4f}  {meaning}")

    print("\n  → 이산 분류 대신 sigmoid로 경계 근처를 부드럽게 전환")


# ══════════════════════════════════════════════════════════════════════
# 3. Significant Location Change — 이동 감지
# ══════════════════════════════════════════════════════════════════════
def verify_slc():
    header("3. Significant Location Change  (이동 감지 → 배율 조절)")

    dest_fence = [Geofence("samsung", SAMSUNG[0], SAMSUNG[1], 200)]

    cases = [
        {
            "label": "정지 (SLC 미감지) → slc_mult=2.0 적용",
            "history": make_history([GANGNAM] * 5),
            "pos": GANGNAM,
        },
        {
            "label": "이동 (SLC 감지)   → slc_mult=1.0",
            "history": make_history([SEOCHO, GANGNAM], interval_sec=60),
            "pos": GANGNAM,
        },
    ]

    print(f"\n  {'시나리오':<38} {'이동거리(m)':>10} {'SLC':>5} {'slc_mult':>9} {'interval':>9}")
    print(f"  {'-'*38} {'-'*10} {'-'*5} {'-'*9} {'-'*9}")

    for c in cases:
        r = calculate_next_interval(c["pos"][0], c["pos"][1], c["history"], dest_fence)
        print(
            f"  {c['label']:<38} {r.moved_distance:>10.0f}"
            f"  {'O' if r.is_significant_change else 'X':>5}"
            f"  {r.slc_multiplier:>9.1f}"
            f"  {r.next_interval:>7}초"
        )

    print("\n  → 정지 상태면 interval을 2배 늘려 배터리 절약")


# ══════════════════════════════════════════════════════════════════════
# 4. Group GPS Interval — 참여자별 개인화 GPS 주기
# ══════════════════════════════════════════════════════════════════════
def verify_group_gps():
    header("4. Group GPS Interval  (참여자별 개인화 GPS 갱신 주기)")

    appointment_time = datetime.now() + timedelta(minutes=25)
    destination = SAMSUNG
    dest_fence = [Geofence("samsung", SAMSUNG[0], SAMSUNG[1], 200)]
    appt_remain = (appointment_time - datetime.now()).total_seconds()

    # 이동 이력을 포함한 시나리오: 각 참여자의 최근 위치 흐름 + ETA
    participants = [
        {
            "name": "Alice  (버스, 멀리)",
            # 서초 → 강남 방향으로 빠르게 이동 중 (~30 m/s)
            "history": make_history([
                (37.4650, 127.0200), (37.4720, 127.0230),
                (37.4800, 127.0250), (37.4900, 127.0270),
            ], interval_sec=10),
            "pos": (37.4900, 127.0270),
            "eta_sec": 900,
        },
        {
            "name": "Bob    (도보, 중간)",
            # 역삼 근처를 천천히 걷는 중 (~1.4 m/s)
            "history": make_history([
                (37.5000, 127.0350), (37.5003, 127.0360),
                (37.5006, 127.0370), (37.5008, 127.0380),
            ], interval_sec=10),
            "pos": (37.5008, 127.0380),
            "eta_sec": 300,
        },
        {
            "name": "Charlie (정지, 근처)",
            # 삼성역 바로 앞에서 멈춤
            "history": make_history([(37.5075, 127.0600)] * 4, interval_sec=10),
            "pos": (37.5075, 127.0600),
            "eta_sec": 30,
        },
    ]

    print(f"\n  약속 시간까지: {int(appt_remain / 60)}분  /  목적지: 삼성역 {destination}")
    print(f"\n  {'참여자':<24} {'이동상태':>10} {'거리(m)':>8} {'ETA':>7} {'interval':>10} {'mode':>8}")
    print(f"  {'-'*24} {'-'*10} {'-'*8} {'-'*7} {'-'*10} {'-'*8}")

    from gps_api.core.optimizer import haversine as hav, estimate_activity

    for p in participants:
        r = calculate_next_interval(
            p["pos"][0], p["pos"][1],
            p["history"], dest_fence,
            eta_sec=p["eta_sec"],
            appointment_remaining_sec=appt_remain,
        )
        dist = hav(p["pos"][0], p["pos"][1], SAMSUNG[0], SAMSUNG[1])
        print(
            f"  {p['name']:<24} {r.activity:>10}  {dist:>7.0f}m"
            f"  {int(p['eta_sec']):>5}초  {r.next_interval:>8}초  {r.gps_mode:>8}"
        )

    print("\n  → 이동 속도·거리·ETA에 따라 참여자마다 다른 GPS 주기 배정")


# ══════════════════════════════════════════════════════════════════════
# 5. 상태 판단 — 약속 시간 대비 ETA로 on_time / leave_soon / hurry / late
# ══════════════════════════════════════════════════════════════════════
def verify_status():
    header("5. 상태 판단  (약속 시간 - ETA → on_time / leave_soon / hurry / late)")

    now = datetime.now()
    appointment_time = now + timedelta(minutes=20)

    cases = [
        {"label": "여유 있음  (ETA 5분, 약속 20분 후)",  "eta": 300,  "dist": 1000},
        {"label": "곧 출발    (ETA 15분, 약속 20분 후)", "eta": 900,  "dist": 3000},
        {"label": "서두름     (ETA 22분, 약속 20분 후)", "eta": 1320, "dist": 4500},
        {"label": "지각       (약속 시간 이미 지남)",     "eta": 300,  "dist": 500,
         "appt": now - timedelta(minutes=5)},
        {"label": "도착       (거리 50m)",                "eta": 10,   "dist": 50},
    ]

    print(f"\n  {'시나리오':<40} {'상태':>12}")
    print(f"  {'-'*40} {'-'*12}")

    for c in cases:
        appt = c.get("appt", appointment_time)
        status = _compute_status(c["eta"], c["dist"], appt)
        print(f"  {c['label']:<40} {status:>12}")

    print("\n  → margin = 약속까지 남은 시간 - ETA")
    print("     ≥ 600s → on_time  /  0~600s → leave_soon  /  <0 → hurry  /  지남 → late")


# ══════════════════════════════════════════════════════════════════════
# 6. Journey ETA 계산 — 개인 여정 (카카오 없이 Haversine fallback)
# ══════════════════════════════════════════════════════════════════════
def verify_journey_eta():
    header("6. Journey ETA  (카카오 API 없이 Haversine fallback)")

    goal_time = datetime.now() + timedelta(minutes=30)

    journey = Journey(
        journey_id="j001",
        member_id="david",
        title="삼성역 약속",
        origin_lat=GANGNAM[0],
        origin_lon=GANGNAM[1],
        origin_name="강남역",
        dest_lat=SAMSUNG[0],
        dest_lon=SAMSUNG[1],
        dest_name="삼성역",
        current_lat=GANGNAM[0],
        current_lon=GANGNAM[1],
        goal_time=goal_time,
    )

    journey = compute_eta(journey)

    print(f"\n  출발지  : 강남역 {GANGNAM}")
    print(f"  목적지  : 삼성역 {SAMSUNG}")
    print(f"  약속 시간 : {goal_time.strftime('%H:%M')}")
    print(f"\n  거리    : {journey.distance_m:.0f}m")
    print(f"  ETA     : {journey.eta_sec:.0f}초 ({journey.eta_sec/60:.1f}분)")
    print(f"  알람 시각 : {journey.alarm_time[:19] if journey.alarm_time else 'N/A'}")
    print(f"  상태    : {journey.status}")
    print("\n  → 카카오 API 키 없어도 Haversine 직선거리로 ETA 계산 가능")


# ══════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"\n{'#' * 64}")
    print(f"  CounterClock gps_api 알고리즘 검증  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'#' * 64}")

    verify_cosine_blend()
    verify_sigmoid_activity()
    verify_slc()
    verify_group_gps()
    verify_status()
    verify_journey_eta()

    print(f"\n{'#' * 64}")
    print("  검증 완료")
    print(f"{'#' * 64}\n")
