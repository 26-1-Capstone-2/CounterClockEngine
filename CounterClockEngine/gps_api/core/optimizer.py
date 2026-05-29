"""
GPS/Geofencing battery optimization core logic
- Distance/time Cosine Blend Interval
- Activity Recognition-like implementation
- Significant Location Change-like implementation
"""

import math
from datetime import datetime
from dataclasses import dataclass, field
from typing import Literal, Optional

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
    activity_multiplier: float
    slc_multiplier: float
    u_dist: float       # Distance urgency [0=far, 1=close]
    u_time: float       # Time urgency [0=relaxed, 1=urgent]
    urgency: float      # Final urgency [0=relaxed, 1=very urgent]


def decayed_eta(eta_sec: Optional[float], last_updated: Optional[str]) -> Optional[float]:
    """
    Returns an approximate ETA (seconds) by subtracting elapsed time from the last computed ETA.
    Reduces staleness by accounting for time elapsed even without a GPS update.
    """
    if eta_sec is None or last_updated is None:
        return eta_sec
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(last_updated)).total_seconds()
        return max(0.0, eta_sec - elapsed)
    except (ValueError, TypeError):
        return eta_sec


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Cosine Blend parameters ───────────────────────────────────────
INTERVAL_MIN_S  = 3      # Minimum interval when urgency=1 (seconds)
INTERVAL_MAX_S  = 300    # Maximum interval when urgency=0 (seconds)
D_MAX_M         = 3000.0 # Distances beyond this are treated as distance urgency=0
TIME_ALPHA      = 0.5    # 0=time only, 1=distance only (0.5: equal blend)
# ────────────────────────────────────────────────────────────────

# Sigmoid transition threshold (m/s) and steepness
# stationary→walking: 0.5 m/s, walking→vehicle: 4.5 m/s (≈16 km/h urban driving)
_ACT_BOUNDARY  = (0.5,  4.5)
_ACT_STEEPNESS = (10.0, 3.0)   # Higher = sharper boundary, lower = more gradual
_ACT_ANCHORS   = (3.0,  1.0, 0.3)  # stationary / walking / vehicle multiplier

SLC_THRESHOLD_M = 500
SLC_MULTIPLIER  = 2.0


def _cosine_blend_interval(
    distance_to_fence: float,
    eta_sec: Optional[float],
    appointment_remaining_sec: Optional[float],
    alpha: float = TIME_ALPHA,
) -> tuple[float, float, float, float]:
    """
    Blends two urgency signals with a cosine curve to return a raw interval (seconds).
    Returns: (raw_interval, u_dist, u_time, urgency)

    urgency=0 → INTERVAL_MAX_S,  urgency=1 → INTERVAL_MIN_S
    """
    # Distance urgency: higher when closer to destination
    d_clamped = max(0.0, min(distance_to_fence, D_MAX_M))
    u_dist = 1.0 - d_clamped / D_MAX_M

    # Time urgency: higher when ETA is large relative to remaining appointment time
    if eta_sec is not None and appointment_remaining_sec and appointment_remaining_sec > 0:
        u_time = min(max(eta_sec / appointment_remaining_sec, 0.0), 1.0)
    else:
        u_time = u_dist  # Use distance urgency when no time information available

    urgency = alpha * u_dist + (1.0 - alpha) * u_time
    urgency = max(0.0, min(1.0, urgency))

    # Cosine curve: interval goes from MAX to MIN as urgency goes from 0 to 1
    raw = INTERVAL_MIN_S + 0.5 * (INTERVAL_MAX_S - INTERVAL_MIN_S) * (1.0 + math.cos(math.pi * urgency))
    return raw, u_dist, u_time, urgency


def _compute_avg_speed(history: list[LocationPoint]) -> Optional[float]:
    if len(history) < 2:
        return None
    speeds = []
    for i in range(1, len(history)):
        prev, curr = history[i - 1], history[i]
        dist = haversine(prev.lat, prev.lon, curr.lat, curr.lon)
        dt = (curr.timestamp - prev.timestamp).total_seconds()
        if dt > 0:
            speeds.append(dist / dt)
    return sum(speeds) / len(speeds) if speeds else None


def smooth_activity_multiplier(avg_speed: float) -> float:
    """
    Smoothly maps speed (m/s) to a multiplier using two sigmoids.

    Boundaries:  0.5 m/s (stationary→walking),  8.0 m/s (walking→vehicle)
    anchors: 3.0 → 1.0 → 0.3
    Formula:  mult = m0 - (m0-m1)*s1 - (m1-m2)*s2
    """
    v1, v2 = _ACT_BOUNDARY
    k1, k2 = _ACT_STEEPNESS
    m0, m1, m2 = _ACT_ANCHORS

    def _sigmoid(x: float) -> float:
        return 1.0 / (1.0 + math.exp(-max(-500.0, min(500.0, x))))

    s1 = _sigmoid(k1 * (avg_speed - v1))
    s2 = _sigmoid(k2 * (avg_speed - v2))
    return m0 - (m0 - m1) * s1 - (m1 - m2) * s2


def estimate_activity(history: list[LocationPoint]) -> ActivityType:
    avg_speed = _compute_avg_speed(history)
    if avg_speed is None:
        return "unknown"
    if avg_speed < 0.5:
        return "stationary"
    elif avg_speed < 4.5:
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
    eta_sec: Optional[float] = None,
    appointment_remaining_sec: Optional[float] = None,
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

    activity = estimate_activity(history)
    avg_speed = _compute_avg_speed(history) or 0.0
    act_mult = smooth_activity_multiplier(avg_speed)

    is_significant, moved_dist = detect_significant_change(history)
    slc_mult = 1.0
    if not is_significant and activity == "stationary":
        slc_mult = SLC_MULTIPLIER

    raw_base, u_dist, u_time, urgency = _cosine_blend_interval(
        min_fence_distance, eta_sec, appointment_remaining_sec
    )
    raw = raw_base * act_mult * slc_mult
    next_interval = max(int(min(round(raw), INTERVAL_MAX_S)), 1)

    return OptimizationResult(
        next_interval=next_interval,
        activity=activity,
        is_significant_change=is_significant,
        moved_distance=round(moved_dist, 1),
        distance_to_nearest_fence=round(min_fence_distance, 1),
        gps_mode=classify_gps_mode(next_interval),
        entered_zones=entered_zones,
        activity_multiplier=act_mult,
        slc_multiplier=slc_mult,
        u_dist=round(u_dist, 3),
        u_time=round(u_time, 3),
        urgency=round(urgency, 3),
    )
