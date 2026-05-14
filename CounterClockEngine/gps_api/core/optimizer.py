"""
GPS/Geofencing 배터리 최적화 핵심 로직
- 거리 기반 Adaptive Interval
- Activity Recognition 유사 구현
- Significant Location Change 유사 구현
"""

import math
from datetime import datetime
from dataclasses import dataclass, field
from typing import Literal

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
    next_interval: int
    activity: ActivityType
    is_significant_change: bool
    moved_distance: float
    distance_to_nearest_fence: float
    gps_mode: GPSMode
    entered_zones: list[str]
    base_interval: int
    activity_multiplier: float
    slc_multiplier: float


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_base_interval(distance_to_fence: float) -> int:
    if distance_to_fence <= 200:
        return 3
    elif distance_to_fence <= 500:
        return 10
    elif distance_to_fence <= 1000:
        return 30
    else:
        return 60


ACTIVITY_MULTIPLIER = {
    "stationary": 3.0,
    "walking":    1.0,
    "vehicle":    0.3,
    "unknown":    1.0,
}

SLC_THRESHOLD_M = 500
SLC_MULTIPLIER  = 2.0
MAX_INTERVAL_S  = 300


def estimate_activity(history: list[LocationPoint]) -> ActivityType:
    if len(history) < 2:
        return "unknown"

    speeds = []
    for i in range(1, len(history)):
        prev, curr = history[i - 1], history[i]
        dist = haversine(prev.lat, prev.lon, curr.lat, curr.lon)
        dt = (curr.timestamp - prev.timestamp).total_seconds()
        if dt > 0:
            speeds.append(dist / dt)

    if not speeds:
        return "unknown"

    avg_speed = sum(speeds) / len(speeds)
    if avg_speed < 0.5:
        return "stationary"
    elif avg_speed < 8.0:
        return "walking"
    else:
        return "vehicle"


def detect_significant_change(
    history: list[LocationPoint],
    threshold: float = SLC_THRESHOLD_M,
) -> tuple[bool, float]:
    if len(history) < 2:
        return False, 0.0
    oldest, newest = history[0], history[-1]
    dist = haversine(oldest.lat, oldest.lon, newest.lat, newest.lon)
    return dist >= threshold, dist


def classify_gps_mode(interval: int) -> GPSMode:
    if interval >= 30:
        return "LOW"
    elif interval >= 10:
        return "BALANCED"
    else:
        return "HIGH"


def calculate_next_interval(
    user_lat: float,
    user_lon: float,
    history: list[LocationPoint],
    geofences: list[Geofence],
) -> OptimizationResult:
    entered_zones = []
    min_fence_distance = float("inf")

    for fence in geofences:
        dist_to_center = haversine(user_lat, user_lon, fence.lat, fence.lon)
        dist_to_edge = dist_to_center - fence.radius
        min_fence_distance = min(min_fence_distance, dist_to_edge)
        if dist_to_edge <= 0:
            entered_zones.append(fence.id)

    if min_fence_distance == float("inf"):
        min_fence_distance = 9999.0

    base = get_base_interval(min_fence_distance)
    activity = estimate_activity(history)
    act_mult = ACTIVITY_MULTIPLIER[activity]

    is_significant, moved_dist = detect_significant_change(history)
    slc_mult = 1.0
    if not is_significant and activity == "stationary":
        slc_mult = SLC_MULTIPLIER

    raw = base * act_mult * slc_mult
    next_interval = max(int(min(round(raw), MAX_INTERVAL_S)), 1)

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
