"""
Pure functions extracted from eta_tracker.py.
The original eta_tracker.py runs assert/print at module level, which is
unsuitable for server environments — this module contains only the logic.
"""

import math


def haversine(loc1: tuple, loc2: tuple) -> float:
    R = 6_371_000
    lat1, lon1 = math.radians(loc1[0]), math.radians(loc1[1])
    lat2, lon2 = math.radians(loc2[0]), math.radians(loc2[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def compute_gradient(prev_loc: tuple, curr_loc: tuple, destination: tuple) -> float:
    """gradient = d(t) - d(t-1). Negative = approaching, positive = moving away."""
    return haversine(curr_loc, destination) - haversine(prev_loc, destination)


def update_with_momentum(gradient: float, velocity: float, momentum: float = 0.9) -> float:
    return momentum * velocity + (1 - momentum) * gradient


def adaptive_update_interval(gradient: float, base_interval: float = 60.0) -> float:
    urgency = abs(gradient) / 100.0
    return max(10.0, base_interval / (1.0 + urgency))


def calculate_eta(current_loc: tuple, destination: tuple, speed_mps: float = 1.4) -> float:
    return haversine(current_loc, destination) / speed_mps


def compute_adaptive_threshold(
    curr_loc: tuple,
    destination: tuple,
    velocity: float,
    distance_ratio: float = 0.05,
    velocity_multiplier: float = 1.5,
    min_threshold: float = 20.0,
) -> float:
    """
    Threshold that scales with remaining distance and current speed.

    - distance_ratio: recalculate if gradient exceeds this fraction of remaining distance
    - velocity_multiplier: adds speed-awareness so fast-moving users get a larger buffer
    - min_threshold: floor to avoid hair-trigger reactions near the destination
    """
    distance_remaining = haversine(curr_loc, destination)
    distance_based = distance_remaining * distance_ratio
    speed_based = abs(velocity) * velocity_multiplier
    return max(min_threshold, distance_based + speed_based)


def handle_movement(
    prev_loc: tuple,
    curr_loc: tuple,
    destination: tuple,
    velocity: float,
    threshold: float | None = None,
) -> dict:
    """
    threshold=None (default) uses compute_adaptive_threshold.
    Pass an explicit float to override with a fixed value.
    """
    gradient = compute_gradient(prev_loc, curr_loc, destination)
    new_velocity = update_with_momentum(gradient, velocity)
    next_interval = adaptive_update_interval(gradient)

    effective_threshold = (
        threshold
        if threshold is not None
        else compute_adaptive_threshold(curr_loc, destination, new_velocity)
    )

    if gradient > effective_threshold:
        action = "recalculate"
        message = (
            f"Warning: moved {gradient:.0f}m away from destination. "
            f"Recalculating ETA immediately. (velocity={new_velocity:.1f}, threshold={effective_threshold:.1f})"
        )
    elif gradient < -effective_threshold:
        action = "advance_eta"
        message = (
            f"Good progress! {abs(gradient):.0f}m closer to destination. "
            f"Advancing ETA. (velocity={new_velocity:.1f}, threshold={effective_threshold:.1f})"
        )
    else:
        action = "maintain"
        message = (
            f"Movement change minimal (gradient={gradient:.1f}m). "
            f"Keeping current ETA. (velocity={new_velocity:.1f}, threshold={effective_threshold:.1f})"
        )

    return {
        "action": action,
        "message": message,
        "next_interval": next_interval,
        "velocity": new_velocity,
        "gradient": gradient,
        "threshold": round(effective_threshold, 2),
    }
