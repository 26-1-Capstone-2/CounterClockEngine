"""
GPS/Geofencing 배터리 최적화 핵심 로직
- 거리 기반 Adaptive Interval
- Activity Recognition 유사 구현
- Significant Location Change 유사 구현
"""

import math
from collections import deque
from datetime import datetime
from dataclasses import dataclass, field
from typing import Literal

# ── 타입 정의 ──────────────────────────────────────────────────
ActivityType = Literal["stationary", "walking", "vehicle", "unknown"]
GPSMode = Literal["LOW", "BALANCED", "HIGH"]

@dataclass
class LocationPoint:
    lat: float
    lon: float
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class Geofence:
    id: str
    lat: float
    lon: float
    radius: float  # meters

@dataclass
class OptimizationResult:
    next_interval: int          # 초
    activity: ActivityType
    is_significant_change: bool
    moved_distance: float       # meters
    distance_to_nearest_fence: float  # meters
    gps_mode: GPSMode
    entered_zones: list[str]
    # 디버깅용 중간 계산값
    base_interval: int
    activity_multiplier: float
    slc_multiplier: float


# ── Haversine 거리 계산 ────────────────────────────────────────
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 좌표 사이의 거리(미터) 반환"""
    R = 6_371_000  # 지구 반지름 (m)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── 전략 1: 거리 기반 base_interval ───────────────────────────
def get_base_interval(distance_to_fence: float) -> int:
    """
    Geofence 경계까지 거리에 따라 기본 GPS 호출 주기 반환
    distance_to_fence: 경계까지 남은 거리 (m), 음수 = 존 내부
    """
    if distance_to_fence <= 0:
        return 3    # 존 내부: 3초
    elif distance_to_fence <= 200:
        return 3    # 200m 이내: 3초 (HIGH)
    elif distance_to_fence <= 500:
        return 10   # 500m 이내: 10초 (BALANCED)
    elif distance_to_fence <= 1000:
        return 30   # 1km 이내: 30초 (LOW)
    else:
        return 60   # 1km 이상: 60초 (LOW)


# ── 전략 2: Activity Recognition 유사 구현 ─────────────────────
ACTIVITY_MULTIPLIER = {
    "stationary": 3.0,
    "walking":    1.0,
    "vehicle":    0.3,
    "unknown":    1.0,
}

def estimate_activity(history: list[LocationPoint]) -> ActivityType:
    """
    최근 위치 이력으로 이동 상태 추정
    연속된 위치 데이터의 평균 속도로 판단
    """
    if len(history) < 2:
        return "unknown"

    speeds = []
    for i in range(1, len(history)):
        prev, curr = history[i - 1], history[i]
        dist = haversine(prev.lat, prev.lon, curr.lat, curr.lon)
        dt = (curr.timestamp - prev.timestamp).total_seconds()
        if dt > 0:
            speeds.append(dist / dt)  # m/s

    if not speeds:
        return "unknown"

    avg_speed = sum(speeds) / len(speeds)

    if avg_speed < 0.5:
        return "stationary"
    elif avg_speed < 8.0:
        return "walking"
    else:
        return "vehicle"


# ── 전략 3: Significant Location Change 유사 구현 ──────────────
SLC_THRESHOLD_M = 500    # 유의미한 이동 기준 거리 (m)
SLC_MULTIPLIER  = 2.0    # 미이동 시 interval 배수
MAX_INTERVAL_S  = 300    # 최대 interval (5분)

def detect_significant_change(
    history: list[LocationPoint],
    threshold: float = SLC_THRESHOLD_M
) -> tuple[bool, float]:
    """
    가장 오래된 위치와 현재 위치 비교
    Returns: (유의미한 이동 여부, 이동 거리)
    """
    if len(history) < 2:
        return False, 0.0

    oldest, newest = history[0], history[-1]
    dist = haversine(oldest.lat, oldest.lon, newest.lat, newest.lon)
    return dist >= threshold, dist


# ── GPS 모드 분류 ──────────────────────────────────────────────
def classify_gps_mode(interval: int) -> GPSMode:
    if interval >= 30:
        return "LOW"
    elif interval >= 10:
        return "BALANCED"
    else:
        return "HIGH"


# ── 핵심: 통합 최적화 계산 ────────────────────────────────────
def calculate_next_interval(
    user_lat: float,
    user_lon: float,
    history: list[LocationPoint],
    geofences: list[Geofence],
) -> OptimizationResult:
    """
    3가지 전략을 결합하여 next_interval 결정
    next_interval = base × activity_multiplier × slc_multiplier
    """
    # ── 거리 계산 ──
    entered_zones = []
    min_fence_distance = float("inf")

    for fence in geofences:
        dist_to_center = haversine(user_lat, user_lon, fence.lat, fence.lon)
        dist_to_edge = dist_to_center - fence.radius  # 음수 = 존 내부
        min_fence_distance = min(min_fence_distance, dist_to_edge)
        if dist_to_edge <= 0:
            entered_zones.append(fence.id)

    if min_fence_distance == float("inf"):
        min_fence_distance = 9999.0

    # ── 전략 1: 거리 기반 base_interval ──
    base = get_base_interval(min_fence_distance)

    # ── 전략 2: Activity Recognition ──
    activity = estimate_activity(history)
    act_mult = ACTIVITY_MULTIPLIER[activity]

    # ── 전략 3: SLC ──
    is_significant, moved_dist = detect_significant_change(history)
    slc_mult = 1.0
    if not is_significant and activity == "stationary":
        slc_mult = SLC_MULTIPLIER

    # ── 최종 계산 ──
    raw = base * act_mult * slc_mult
    next_interval = int(min(round(raw), MAX_INTERVAL_S))
    next_interval = max(next_interval, 1)  # 최소 1초

    return OptimizationResult(
        next_interval=next_interval,
        activity=activity,
        is_significant_change=is_significant,
        moved_distance=round(moved_dist, 1),
        distance_to_nearest_fence=round(min_fence_distance, 1),
        gps_mode=classify_gps_mode(next_interval),
        entered_zones=entered_zones,
        base_interval=base,
        activity_multiplier=act_mult,
        slc_multiplier=slc_mult,
    )
