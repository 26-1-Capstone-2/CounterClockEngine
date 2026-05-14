"""
ETA Tracker — Gradient Descent 기반 실시간 위치 추적 및 ETA 갱신 시스템

Loss function  : 현재 위치 → 목적지 거리  d(t)
Gradient       : d(t) - d(t-1)  (거리 변화율)
목표           : Loss(거리)를 최소화하는 방향으로 ETA 갱신 전략 결정
"""

import math


# ─────────────────────────────────────────────────────────────
# 1. haversine
# ─────────────────────────────────────────────────────────────

def haversine(loc1: tuple, loc2: tuple) -> float:
    """두 GPS 좌표 사이의 실제 거리를 미터 단위로 반환한다.

    Haversine 공식을 사용하여 지구 곡률을 반영한 거리를 계산한다.

    Args:
        loc1: (위도, 경도) 튜플 — 출발점
        loc2: (위도, 경도) 튜플 — 도착점

    Returns:
        두 좌표 사이의 거리 (미터)
    """
    R = 6_371_000  # 지구 반지름 (m)
    lat1, lon1 = math.radians(loc1[0]), math.radians(loc1[1])
    lat2, lon2 = math.radians(loc2[0]), math.radians(loc2[1])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# 단위 테스트
_d = haversine((37.5665, 126.9780), (37.5665, 126.9780))
assert abs(_d) < 1e-6, "동일 좌표는 0m여야 한다"

_d = haversine((0.0, 0.0), (0.0, 1.0))
assert 111_000 < _d < 112_000, f"경도 1도 ≈ 111km, 실제: {_d:.0f}m"

print(f"[haversine] 서울시청 → 서울시청    : {haversine((37.5665, 126.9780), (37.5665, 126.9780)):.1f}m")
print(f"[haversine] 강남역  → 삼성역        : {haversine((37.4979, 127.0276), (37.5088, 127.0632)):.0f}m")


# ─────────────────────────────────────────────────────────────
# 2. compute_gradient
# ─────────────────────────────────────────────────────────────

def compute_gradient(prev_loc: tuple, curr_loc: tuple, destination: tuple) -> float:
    """현재 스텝의 거리 변화율(gradient)을 계산한다.

    gradient = d(t) - d(t-1)
      - 양수 : 목적지에서 멀어지는 중  (Loss 증가)
      - 음수 : 목적지에 가까워지는 중  (Loss 감소)
      - 0 근처: 정지 상태

    Args:
        prev_loc    : 이전 위치 (위도, 경도)
        curr_loc    : 현재 위치 (위도, 경도)
        destination : 목적지   (위도, 경도)

    Returns:
        gradient 값 (미터)
    """
    d_prev = haversine(prev_loc, destination)
    d_curr = haversine(curr_loc, destination)
    return d_curr - d_prev


# 단위 테스트
# 강남역(출발) → 삼성역(중간) → 삼성역(목적지) 방향으로 이동
_dest = (37.5088, 127.0632)   # 삼성역
_g_closer  = compute_gradient((37.4979, 127.0276), (37.5020, 127.0400), _dest)
_g_farther = compute_gradient((37.5020, 127.0400), (37.4900, 127.0100), _dest)

assert _g_closer  < 0, "목적지에 다가가면 gradient < 0"
assert _g_farther > 0, "목적지에서 멀어지면 gradient > 0"

print(f"\n[compute_gradient] 가까워지는 중 : {_g_closer:.2f}m")
print(f"[compute_gradient] 멀어지는 중   : {_g_farther:.2f}m")


# ─────────────────────────────────────────────────────────────
# 3. update_with_momentum
# ─────────────────────────────────────────────────────────────

def update_with_momentum(gradient: float, velocity: float, momentum: float = 0.9) -> float:
    """모멘텀을 적용하여 이동 추세(관성)를 반영한 velocity를 갱신한다.

    velocity = momentum * velocity + (1 - momentum) * gradient

      - velocity 양수 지속 → 멀어지는 추세
      - velocity 음수 지속 → 가까워지는 추세
      - 부호 전환           → 방향 전환 감지

    Args:
        gradient : 현재 스텝의 거리 변화율
        velocity : 이전 velocity (초기값 0.0)
        momentum : 관성 계수 (기본 0.9)

    Returns:
        갱신된 velocity 값
    """
    return momentum * velocity + (1 - momentum) * gradient


# 단위 테스트
_v = 0.0
for _g in [-100, -80, -70]:   # 지속 접근 → velocity 점점 음수
    _v = update_with_momentum(_g, _v)
assert _v < 0, "지속 접근 시 velocity < 0이어야 한다"

_v2 = 0.0
for _g in [100, 120, 110]:    # 지속 이탈 → velocity 점점 양수
    _v2 = update_with_momentum(_g, _v2)
assert _v2 > 0, "지속 이탈 시 velocity > 0이어야 한다"

print(f"\n[update_with_momentum] 지속 접근 후 velocity : {_v:.2f}")
print(f"[update_with_momentum] 지속 이탈 후 velocity : {_v2:.2f}")


# ─────────────────────────────────────────────────────────────
# 4. adaptive_update_interval
# ─────────────────────────────────────────────────────────────

def adaptive_update_interval(gradient: float, base_interval: float = 60.0) -> float:
    """gradient 크기에 따라 위치 갱신 주기를 동적으로 조정한다.

    urgency  = |gradient| / 100
    interval = base_interval / (1 + urgency)
    최솟값 10초 보장

    gradient가 클수록(급격한 변화) → 간격 짧아짐 (Adaptive Learning Rate와 동일 원리)
    gradient가 작을수록(정체 상태) → 간격 길어짐

    Args:
        gradient      : 현재 거리 변화율 (미터)
        base_interval : 기본 갱신 주기 (초, 기본 60s)

    Returns:
        조정된 갱신 주기 (초), 최소 10초
    """
    urgency = abs(gradient) / 100.0
    interval = base_interval / (1.0 + urgency)
    return max(10.0, interval)


# 단위 테스트
assert adaptive_update_interval(0)     == 60.0,  "gradient=0이면 base_interval 그대로"
assert adaptive_update_interval(100)   == 30.0,  "urgency=1 → 60/2=30s"
assert adaptive_update_interval(10000) == 10.0,  "매우 큰 gradient → 최소 10s"
assert adaptive_update_interval(-200)  == 20.0,  "음수 gradient도 절댓값 사용"

print(f"\n[adaptive_update_interval] gradient=0    : {adaptive_update_interval(0):.1f}s")
print(f"[adaptive_update_interval] gradient=100  : {adaptive_update_interval(100):.1f}s")
print(f"[adaptive_update_interval] gradient=500  : {adaptive_update_interval(500):.1f}s")
print(f"[adaptive_update_interval] gradient=10000: {adaptive_update_interval(10000):.1f}s")


# ─────────────────────────────────────────────────────────────
# 5. handle_movement
# ─────────────────────────────────────────────────────────────

def handle_movement(
    prev_loc: tuple,
    curr_loc: tuple,
    destination: tuple,
    velocity: float,
    threshold: float = 50.0,
) -> dict:
    """이동 상태를 분석하고 ETA 갱신 전략을 결정한다.

    내부적으로 compute_gradient → update_with_momentum → adaptive_update_interval
    순서로 호출하며, gradient 크기에 따라 세 가지 액션 중 하나를 반환한다.

      gradient >  threshold : 즉시 ETA 재계산 + 경고 메시지
      gradient < -threshold : ETA 앞당김    + 긍정 메시지
      그 외                 : 기존 ETA 유지

    Args:
        prev_loc    : 이전 위치 (위도, 경도)
        curr_loc    : 현재 위치 (위도, 경도)
        destination : 목적지   (위도, 경도)
        velocity    : 이전 velocity (초기값 0.0)
        threshold   : 액션 분기 임계값 (미터, 기본 50m)

    Returns:
        {
            "action"        : "recalculate" | "advance_eta" | "maintain",
            "message"       : str,
            "next_interval" : float,   # 다음 갱신까지 대기 시간 (초)
            "velocity"      : float,   # 갱신된 velocity
        }
    """
    gradient      = compute_gradient(prev_loc, curr_loc, destination)
    new_velocity  = update_with_momentum(gradient, velocity)
    next_interval = adaptive_update_interval(gradient)

    if gradient > threshold:
        action  = "recalculate"
        message = (
            f"경고: 목적지에서 {gradient:.0f}m 멀어졌습니다. "
            f"ETA를 즉시 재계산합니다. (velocity={new_velocity:.1f})"
        )
    elif gradient < -threshold:
        action  = "advance_eta"
        message = (
            f"좋아요! 목적지에 {abs(gradient):.0f}m 더 가까워졌습니다. "
            f"ETA를 앞당깁니다. (velocity={new_velocity:.1f})"
        )
    else:
        action  = "maintain"
        message = (
            f"이동 변화 미미 (gradient={gradient:.1f}m). "
            f"기존 ETA를 유지합니다. (velocity={new_velocity:.1f})"
        )

    return {
        "action":        action,
        "message":       message,
        "next_interval": next_interval,
        "velocity":      new_velocity,
    }


# 단위 테스트
_dest = (37.5088, 127.0632)
_r1 = handle_movement(
    (37.4979, 127.0276), (37.4900, 127.0100), _dest, velocity=0.0
)
assert _r1["action"] == "recalculate", "멀어지면 recalculate"

_r2 = handle_movement(
    (37.4979, 127.0276), (37.5050, 127.0500), _dest, velocity=0.0
)
assert _r2["action"] == "advance_eta", "가까워지면 advance_eta"

print(f"\n[handle_movement] 멀어지는 케이스 → action: {_r1['action']}")
print(f"  {_r1['message']}")
print(f"  next_interval={_r1['next_interval']:.1f}s, velocity={_r1['velocity']:.2f}")

print(f"\n[handle_movement] 가까워지는 케이스 → action: {_r2['action']}")
print(f"  {_r2['message']}")
print(f"  next_interval={_r2['next_interval']:.1f}s, velocity={_r2['velocity']:.2f}")


# ─────────────────────────────────────────────────────────────
# 6. calculate_eta
# ─────────────────────────────────────────────────────────────

def calculate_eta(current_loc: tuple, destination: tuple, speed_mps: float = 1.4) -> float:
    """현재 위치에서 목적지까지의 ETA(초)를 계산한다.

    ETA = 거리(m) / 속도(m/s)
    기본 속도 1.4 m/s는 도보 평균 속도 기준이다.

    Args:
        current_loc : 현재 위치 (위도, 경도)
        destination : 목적지   (위도, 경도)
        speed_mps   : 이동 속도 (m/s, 기본 1.4m/s)

    Returns:
        예상 도착 시간 (초)
    """
    distance = haversine(current_loc, destination)
    return distance / speed_mps


# 단위 테스트
_eta = calculate_eta((37.4979, 127.0276), (37.4979, 127.0276))
assert abs(_eta) < 1e-6, "동일 위치 ETA는 0이어야 한다"

_eta2 = calculate_eta((37.4979, 127.0276), (37.5088, 127.0632))
assert _eta2 > 0, "다른 위치 ETA는 양수여야 한다"

print(f"\n[calculate_eta] 강남역 → 삼성역 (도보 1.4m/s) : {_eta2:.0f}초 ({_eta2/60:.1f}분)")


# ─────────────────────────────────────────────────────────────
# 전체 시뮬레이션
# ─────────────────────────────────────────────────────────────

def run_simulation():
    """A → B 경로 시뮬레이션 (멀어지는 케이스 + 가까워지는 케이스)."""

    print("\n" + "=" * 65)
    print("  ETA Tracker - Gradient Descent 기반 실시간 추적 시뮬레이션")
    print("=" * 65)

    destination = (37.5088, 127.0632)   # 삼성역 (목적지)
    print(f"  목적지: 삼성역 {destination}")

    # ── 케이스 1: 올바른 방향으로 이동 (가까워지는 케이스) ───────────
    print("\n" + "-" * 65)
    print("  [케이스 1] 강남역 출발 → 삼성역 방향으로 접근")
    print("-" * 65)

    waypoints_approach = [
        (37.4979, 127.0276),   # 강남역 (출발)
        (37.5010, 127.0350),
        (37.5035, 127.0450),
        (37.5060, 127.0540),
        (37.5088, 127.0632),   # 삼성역 (도착)
    ]

    velocity = 0.0
    for i in range(1, len(waypoints_approach)):
        prev = waypoints_approach[i - 1]
        curr = waypoints_approach[i]
        result = handle_movement(prev, curr, destination, velocity)
        velocity = result["velocity"]
        eta_sec  = calculate_eta(curr, destination)
        print(
            f"  스텝 {i}  위치={curr}  "
            f"ETA={eta_sec:.0f}s({eta_sec/60:.1f}분)  "
            f"action={result['action']}  interval={result['next_interval']:.1f}s"
        )
        print(f"         {result['message']}")

    # ── 케이스 2: 잘못된 방향으로 이탈 후 방향 전환 (멀어지는 케이스) ──
    print("\n" + "-" * 65)
    print("  [케이스 2] 출발 후 반대 방향으로 이탈 → 방향 전환")
    print("-" * 65)

    waypoints_detour = [
        (37.5010, 127.0350),   # 출발
        (37.4970, 127.0200),   # 반대 방향 이탈 1
        (37.4940, 127.0100),   # 반대 방향 이탈 2
        (37.4980, 127.0300),   # 방향 전환 시작
        (37.5040, 127.0480),   # 다시 접근
        (37.5088, 127.0632),   # 목적지 도착
    ]

    velocity = 0.0
    for i in range(1, len(waypoints_detour)):
        prev = waypoints_detour[i - 1]
        curr = waypoints_detour[i]
        result = handle_movement(prev, curr, destination, velocity)
        velocity = result["velocity"]
        eta_sec  = calculate_eta(curr, destination)
        print(
            f"  스텝 {i}  위치={curr}  "
            f"ETA={eta_sec:.0f}s({eta_sec/60:.1f}분)  "
            f"action={result['action']}  interval={result['next_interval']:.1f}s"
        )
        print(f"         {result['message']}")

    print("\n" + "=" * 65)
    print("  시뮬레이션 완료")
    print("=" * 65)


if __name__ == "__main__":
    run_simulation()
