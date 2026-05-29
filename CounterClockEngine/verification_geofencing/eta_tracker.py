"""
ETA Tracker — Real-time location tracking and ETA update system based on Gradient Descent

Loss function  : Current location → destination distance  d(t)
Gradient       : d(t) - d(t-1)  (rate of distance change)
Goal           : Determine ETA update strategy in the direction that minimizes Loss (distance)
"""

import math


# ─────────────────────────────────────────────────────────────
# 1. haversine
# ─────────────────────────────────────────────────────────────

def haversine(loc1: tuple, loc2: tuple) -> float:
    """Returns the actual distance in meters between two GPS coordinates.

    Calculates distance accounting for Earth's curvature using the Haversine formula.

    Args:
        loc1: (latitude, longitude) tuple — departure point
        loc2: (latitude, longitude) tuple — arrival point

    Returns:
        Distance between the two coordinates (meters)
    """
    R = 6_371_000  # Earth radius (m)
    lat1, lon1 = math.radians(loc1[0]), math.radians(loc1[1])
    lat2, lon2 = math.radians(loc2[0]), math.radians(loc2[1])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# Unit test
_d = haversine((37.5665, 126.9780), (37.5665, 126.9780))
assert abs(_d) < 1e-6, "Same coordinates should be 0m"

_d = haversine((0.0, 0.0), (0.0, 1.0))
assert 111_000 < _d < 112_000, f"1 degree longitude ≈ 111km, actual: {_d:.0f}m"

print(f"[haversine] Seoul City Hall → Seoul City Hall    : {haversine((37.5665, 126.9780), (37.5665, 126.9780)):.1f}m")
print(f"[haversine] Gangnam Station → Samsung Station    : {haversine((37.4979, 127.0276), (37.5088, 127.0632)):.0f}m")


# ─────────────────────────────────────────────────────────────
# 2. compute_gradient
# ─────────────────────────────────────────────────────────────

def compute_gradient(prev_loc: tuple, curr_loc: tuple, destination: tuple) -> float:
    """Calculates the rate of distance change (gradient) for the current step.

    gradient = d(t) - d(t-1)
      - Positive: moving away from destination  (Loss increasing)
      - Negative: approaching destination  (Loss decreasing)
      - Near 0: stationary

    Args:
        prev_loc    : Previous location (latitude, longitude)
        curr_loc    : Current location (latitude, longitude)
        destination : Destination   (latitude, longitude)

    Returns:
        gradient value (meters)
    """
    d_prev = haversine(prev_loc, destination)
    d_curr = haversine(curr_loc, destination)
    return d_curr - d_prev


# Unit test
# Direction: Gangnam Station (start) → Samsung Station (mid) → Samsung Station (destination)
_dest = (37.5088, 127.0632)   # Samsung Station
_g_closer  = compute_gradient((37.4979, 127.0276), (37.5020, 127.0400), _dest)
_g_farther = compute_gradient((37.5020, 127.0400), (37.4900, 127.0100), _dest)

assert _g_closer  < 0, "Approaching destination should give gradient < 0"
assert _g_farther > 0, "Moving away from destination should give gradient > 0"

print(f"\n[compute_gradient] Approaching : {_g_closer:.2f}m")
print(f"[compute_gradient] Moving away  : {_g_farther:.2f}m")


# ─────────────────────────────────────────────────────────────
# 3. update_with_momentum
# ─────────────────────────────────────────────────────────────

def update_with_momentum(gradient: float, velocity: float, momentum: float = 0.9) -> float:
    """Updates velocity reflecting movement trend (inertia) by applying momentum.

    velocity = momentum * velocity + (1 - momentum) * gradient

      - Sustained positive velocity → trend of moving away
      - Sustained negative velocity → trend of approaching
      - Sign change               → direction change detected

    Args:
        gradient : Rate of distance change for current step
        velocity : Previous velocity (initial value 0.0)
        momentum : Inertia coefficient (default 0.9)

    Returns:
        Updated velocity value
    """
    return momentum * velocity + (1 - momentum) * gradient


# Unit test
_v = 0.0
for _g in [-100, -80, -70]:   # Sustained approach → velocity becomes increasingly negative
    _v = update_with_momentum(_g, _v)
assert _v < 0, "Sustained approach should give velocity < 0"

_v2 = 0.0
for _g in [100, 120, 110]:    # Sustained departure → velocity becomes increasingly positive
    _v2 = update_with_momentum(_g, _v2)
assert _v2 > 0, "Sustained departure should give velocity > 0"

print(f"\n[update_with_momentum] velocity after sustained approach : {_v:.2f}")
print(f"[update_with_momentum] velocity after sustained departure : {_v2:.2f}")


# ─────────────────────────────────────────────────────────────
# 4. adaptive_update_interval
# ─────────────────────────────────────────────────────────────

def adaptive_update_interval(gradient: float, base_interval: float = 60.0) -> float:
    """Dynamically adjusts the location update interval based on gradient magnitude.

    urgency  = |gradient| / 100
    interval = base_interval / (1 + urgency)
    Minimum guaranteed 10 seconds

    Larger gradient (rapid change) → shorter interval (same principle as Adaptive Learning Rate)
    Smaller gradient (stalled state) → longer interval

    Args:
        gradient      : Current rate of distance change (meters)
        base_interval : Base update interval (seconds, default 60s)

    Returns:
        Adjusted update interval (seconds), minimum 10 seconds
    """
    urgency = abs(gradient) / 100.0
    interval = base_interval / (1.0 + urgency)
    return max(10.0, interval)


# Unit test
assert adaptive_update_interval(0)     == 60.0,  "gradient=0 should keep base_interval"
assert adaptive_update_interval(100)   == 30.0,  "urgency=1 → 60/2=30s"
assert adaptive_update_interval(10000) == 10.0,  "very large gradient → minimum 10s"
assert adaptive_update_interval(-200)  == 20.0,  "negative gradient uses absolute value"

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
    """Analyzes movement state and determines the ETA update strategy.

    Internally calls compute_gradient → update_with_momentum → adaptive_update_interval
    in order, and returns one of three actions based on gradient magnitude.

      gradient >  threshold : Immediately recalculate ETA + warning message
      gradient < -threshold : Advance ETA    + positive message
      Otherwise             : Maintain existing ETA

    Args:
        prev_loc    : Previous location (latitude, longitude)
        curr_loc    : Current location (latitude, longitude)
        destination : Destination   (latitude, longitude)
        velocity    : Previous velocity (initial value 0.0)
        threshold   : Action branching threshold (meters, default 50m)

    Returns:
        {
            "action"        : "recalculate" | "advance_eta" | "maintain",
            "message"       : str,
            "next_interval" : float,   # Wait time until next update (seconds)
            "velocity"      : float,   # Updated velocity
        }
    """
    gradient      = compute_gradient(prev_loc, curr_loc, destination)
    new_velocity  = update_with_momentum(gradient, velocity)
    next_interval = adaptive_update_interval(gradient)

    if gradient > threshold:
        action  = "recalculate"
        message = (
            f"Warning: moved {gradient:.0f}m farther from destination. "
            f"Recalculating ETA immediately. (velocity={new_velocity:.1f})"
        )
    elif gradient < -threshold:
        action  = "advance_eta"
        message = (
            f"Great! {abs(gradient):.0f}m closer to destination. "
            f"Advancing ETA. (velocity={new_velocity:.1f})"
        )
    else:
        action  = "maintain"
        message = (
            f"Minor movement change (gradient={gradient:.1f}m). "
            f"Maintaining existing ETA. (velocity={new_velocity:.1f})"
        )

    return {
        "action":        action,
        "message":       message,
        "next_interval": next_interval,
        "velocity":      new_velocity,
    }


# Unit test
_dest = (37.5088, 127.0632)
_r1 = handle_movement(
    (37.4979, 127.0276), (37.4900, 127.0100), _dest, velocity=0.0
)
assert _r1["action"] == "recalculate", "Moving away should recalculate"

_r2 = handle_movement(
    (37.4979, 127.0276), (37.5050, 127.0500), _dest, velocity=0.0
)
assert _r2["action"] == "advance_eta", "Approaching should advance_eta"

print(f"\n[handle_movement] Moving away case → action: {_r1['action']}")
print(f"  {_r1['message']}")
print(f"  next_interval={_r1['next_interval']:.1f}s, velocity={_r1['velocity']:.2f}")

print(f"\n[handle_movement] Approaching case → action: {_r2['action']}")
print(f"  {_r2['message']}")
print(f"  next_interval={_r2['next_interval']:.1f}s, velocity={_r2['velocity']:.2f}")


# ─────────────────────────────────────────────────────────────
# 6. calculate_eta
# ─────────────────────────────────────────────────────────────

def calculate_eta(current_loc: tuple, destination: tuple, speed_mps: float = 1.4) -> float:
    """Calculates the ETA (seconds) from current location to destination.

    ETA = distance(m) / speed(m/s)
    Default speed of 1.4 m/s is based on average walking speed.

    Args:
        current_loc : Current location (latitude, longitude)
        destination : Destination   (latitude, longitude)
        speed_mps   : Movement speed (m/s, default 1.4m/s)

    Returns:
        Estimated arrival time (seconds)
    """
    distance = haversine(current_loc, destination)
    return distance / speed_mps


# Unit test
_eta = calculate_eta((37.4979, 127.0276), (37.4979, 127.0276))
assert abs(_eta) < 1e-6, "ETA at same location should be 0"

_eta2 = calculate_eta((37.4979, 127.0276), (37.5088, 127.0632))
assert _eta2 > 0, "ETA to different location should be positive"

print(f"\n[calculate_eta] Gangnam Station → Samsung Station (walking 1.4m/s) : {_eta2:.0f}s ({_eta2/60:.1f}min)")


# ─────────────────────────────────────────────────────────────
# Full simulation
# ─────────────────────────────────────────────────────────────

def run_simulation():
    """A → B route simulation (moving away case + approaching case)."""

    print("\n" + "=" * 65)
    print("  ETA Tracker - Real-time Tracking Simulation based on Gradient Descent")
    print("=" * 65)

    destination = (37.5088, 127.0632)   # Samsung Station (destination)
    print(f"  Destination: Samsung Station {destination}")

    # ── Case 1: Moving in correct direction (approaching case) ──────────
    print("\n" + "-" * 65)
    print("  [Case 1] Departing from Gangnam Station → approaching Samsung Station")
    print("-" * 65)

    waypoints_approach = [
        (37.4979, 127.0276),   # Gangnam Station (start)
        (37.5010, 127.0350),
        (37.5035, 127.0450),
        (37.5060, 127.0540),
        (37.5088, 127.0632),   # Samsung Station (arrival)
    ]

    velocity = 0.0
    for i in range(1, len(waypoints_approach)):
        prev = waypoints_approach[i - 1]
        curr = waypoints_approach[i]
        result = handle_movement(prev, curr, destination, velocity)
        velocity = result["velocity"]
        eta_sec  = calculate_eta(curr, destination)
        print(
            f"  Step {i}  pos={curr}  "
            f"ETA={eta_sec:.0f}s({eta_sec/60:.1f}min)  "
            f"action={result['action']}  interval={result['next_interval']:.1f}s"
        )
        print(f"         {result['message']}")

    # ── Case 2: Detouring in wrong direction then turning back (moving away case) ──
    print("\n" + "-" * 65)
    print("  [Case 2] Detour in wrong direction → turn around")
    print("-" * 65)

    waypoints_detour = [
        (37.5010, 127.0350),   # Start
        (37.4970, 127.0200),   # Detour in wrong direction 1
        (37.4940, 127.0100),   # Detour in wrong direction 2
        (37.4980, 127.0300),   # Start turning around
        (37.5040, 127.0480),   # Approaching again
        (37.5088, 127.0632),   # Arrive at destination
    ]

    velocity = 0.0
    for i in range(1, len(waypoints_detour)):
        prev = waypoints_detour[i - 1]
        curr = waypoints_detour[i]
        result = handle_movement(prev, curr, destination, velocity)
        velocity = result["velocity"]
        eta_sec  = calculate_eta(curr, destination)
        print(
            f"  Step {i}  pos={curr}  "
            f"ETA={eta_sec:.0f}s({eta_sec/60:.1f}min)  "
            f"action={result['action']}  interval={result['next_interval']:.1f}s"
        )
        print(f"         {result['message']}")

    print("\n" + "=" * 65)
    print("  Simulation complete")
    print("=" * 65)


if __name__ == "__main__":
    run_simulation()
