"""
GPS simulation + battery savings measurement
- Reproduce actual movement route to compare normal mode vs optimized mode
- Scenario: Gangnam Station → Samsung Station bus trip then stop near Samsung Station
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


# ── Battery drain model ────────────────────────────────────────
# Battery drain rate per second by GPS accuracy (%)
# Estimated from real measurements (Android HIGH_ACCURACY baseline ~12%/hr)
BATTERY_DRAIN_PER_SEC = {
    "HIGH":     12.0 / 3600,   # ~0.00333 %/s
    "BALANCED":  5.0 / 3600,   # ~0.00139 %/s
    "LOW":       1.5 / 3600,   # ~0.00042 %/s
    "OFF":       0.1 / 3600,   # Negligible (minimal background)
}

# Normal app (Kakao Maps navigation) mode: always HIGH, 1s interval
NORMAL_MODE_DRAIN = BATTERY_DRAIN_PER_SEC["HIGH"]


@dataclass
class SimulationLog:
    timestamp: datetime
    lat: float
    lon: float
    gps_mode: str
    interval: int
    battery_drain: float   # Cumulative battery drain (%)
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


# ── Route generation ──────────────────────────────────────────
def generate_route(noise: float = 0.00002) -> list[tuple]:
    """
    Generate a route simulating bus trip from Gangnam Station to Samsung Station
    then stopping near Samsung Station.
    Returns a coordinate sequence for ~20 min simulation.
    (lat, lon, speed_phase)
    """
    route = []

    # Phase 1: Bus travel (about 8 min, Gangnam → Samsung)
    start = (37.4980, 127.0210)
    end   = (37.5088, 127.0632)
    steps = 48  # 10s interval × 48 = 480s
    for i in range(steps):
        t = i / steps
        lat = start[0] + (end[0] - start[0]) * t + random.uniform(-noise, noise)
        lon = start[1] + (end[1] - start[1]) * t + random.uniform(-noise, noise)
        route.append((lat, lon, "vehicle"))

    # Phase 2: Walking near Samsung Station (about 5 min)
    base = end
    for i in range(30):
        lat = base[0] + random.uniform(-0.0003, 0.0003)
        lon = base[1] + random.uniform(-0.0003, 0.0003)
        route.append((lat, lon, "walking"))

    # Phase 3: Stationary inside Samsung Station (about 7 min)
    stop = (37.5088, 127.0635)
    for _ in range(42):
        lat = stop[0] + random.uniform(-0.00005, 0.00005)
        lon = stop[1] + random.uniform(-0.00005, 0.00005)
        route.append((lat, lon, "stationary"))

    return route


# ── Normal mode simulation ────────────────────────────────────
def simulate_normal_mode(
    route: list[tuple],
    interval_sec: int = 1,
    step_sec: int = 10,
    geofences: list = None,
    departure_time: datetime = None,
) -> SimulationResult:
    """
    Legacy navigation app approach: always calls HIGH accuracy GPS every 1 second
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


# ── Optimized mode simulation ─────────────────────────────────
def simulate_optimized_mode(
    route: list[tuple],
    step_sec: int = 10,
    geofences: list = None,
    departure_time: datetime = None,
) -> SimulationResult:
    """
    Our service approach: dynamic interval based on distance + Activity + SLC
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

        # Check if GPS call is scheduled for this step
        if elapsed < next_call_at:
            continue

        # Perform GPS call
        point = LocationPoint(lat, lon, t)
        history_deque.append(point)
        if len(history_deque) > 5:
            history_deque.pop(0)

        opt = calculate_next_interval(lat, lon, history_deque, geofences)
        next_call_at = elapsed + opt.next_interval

        # Battery drain: consume at this mode's rate for the current interval
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


# ── Comparison report output ──────────────────────────────────
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
    print("  GPS Battery Optimization Simulation Results")
    print("=" * 60)
    print(f"  Simulation duration: {duration_min:.0f} min")
    if scenario:
        print(f"  Scenario: {scenario}")
    print("-" * 60)
    print(f"  {'Item':<25} {'Normal Mode':>10} {'Optimized Mode':>12}")
    print("-" * 60)
    print(f"  {'Battery drain (%)':.<25} {normal.total_battery_drain:>9.3f}% {optimized.total_battery_drain:>11.3f}%")
    print(f"  {'Battery drain per hour':.<25} {normal.battery_per_hour:>9.2f}% {optimized.battery_per_hour:>11.2f}%")
    print(f"  {'GPS call count':.<25} {normal.total_gps_calls:>10,} {optimized.total_gps_calls:>12,}")
    print(f"  {'GPS calls per min':.<25} {normal.calls_per_min:>9.1f} {optimized.calls_per_min:>10.1f}")
    print("=" * 60)
    print(f"  🔋 Battery savings rate:   {saving_battery:>6.1f}%")
    print(f"  📍 GPS call reduction rate: {saving_calls:>6.1f}%")
    print("=" * 60)

    # GPS mode distribution
    mode_dist = {}
    for log in optimized.logs:
        mode_dist[log.gps_mode] = mode_dist.get(log.gps_mode, 0) + 1
    total = sum(mode_dist.values())
    print("\n  [Optimized Mode] GPS mode distribution:")
    for mode, cnt in sorted(mode_dist.items()):
        pct = cnt / total * 100
        bar = "█" * int(pct / 5)
        print(f"    {mode:<10} {bar:<20} {pct:5.1f}%")

    # Interval sample per segment
    print("\n  [Optimized Mode] interval sample (first 10 calls):")
    print(f"    {'Time':>8}  {'Dist(m)':>8}  {'Activity':>10}  {'mode':>8}  {'interval':>10}")
    for log in optimized.logs[:10]:
        print(f"    {log.timestamp.strftime('%H:%M:%S'):>8}  "
              f"{log.distance_to_fence:>8.0f}  "
              f"{log.activity:>10}  "
              f"{log.gps_mode:>8}  "
              f"{log.interval:>8}s")

    return saving_battery, saving_calls


# ── Main execution ────────────────────────────────────────────
if __name__ == "__main__":
    random.seed(42)  # Reproducible results

    print("Generating route...")
    route = generate_route()
    print(f"Total {len(route)} location points generated ({len(route)*10//60} min simulation)")

    print("Running normal mode simulation...")
    normal = simulate_normal_mode(route)

    print("Running optimized mode simulation...")
    optimized = simulate_optimized_mode(route)

    saving_battery, saving_calls = print_report(normal, optimized)

    # Validation criteria: must save at least 40%
    assert saving_battery >= 40.0, f"Battery savings below threshold: {saving_battery:.1f}% < 40%"
    assert saving_calls   >= 60.0, f"GPS call reduction below threshold: {saving_calls:.1f}% < 60%"
    print("\n✅ Validation passed: Battery savings and GPS call reduction meet criteria")
