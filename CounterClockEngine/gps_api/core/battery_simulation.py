"""
GPS simulation + battery savings measurement
- Reproduces actual travel routes to compare normal mode vs optimized mode
- Scenario: Gangnam Station → Samsung Station bus trip, then stationary near Samsung Station
"""

import math
import random
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from gps_api.core.optimizer import (
    haversine, calculate_next_interval,
    LocationPoint, Geofence,
)


BATTERY_DRAIN_PER_SEC = {
    "HIGH":     12.0 / 3600,
    "BALANCED":  5.0 / 3600,
    "LOW":       1.5 / 3600,
    "OFF":       0.1 / 3600,
}

NORMAL_MODE_DRAIN = BATTERY_DRAIN_PER_SEC["HIGH"]


@dataclass
class SimulationLog:
    timestamp: datetime
    lat: float
    lon: float
    gps_mode: str
    interval: int
    battery_drain: float
    distance_to_fence: float
    activity: str


@dataclass
class SimulationResult:
    mode: str
    duration_sec: int
    total_battery_drain: float
    total_gps_calls: int
    logs: list[SimulationLog] = field(default_factory=list)

    @property
    def battery_per_hour(self) -> float:
        return self.total_battery_drain / (self.duration_sec / 3600)

    @property
    def calls_per_min(self) -> float:
        return self.total_gps_calls / (self.duration_sec / 60)


def generate_route(noise: float = 0.00002) -> list[tuple]:
    route = []

    start = (37.4980, 127.0210)
    end   = (37.5088, 127.0632)
    steps = 48
    for i in range(steps):
        t = i / steps
        lat = start[0] + (end[0] - start[0]) * t + random.uniform(-noise, noise)
        lon = start[1] + (end[1] - start[1]) * t + random.uniform(-noise, noise)
        route.append((lat, lon, "vehicle"))

    base = end
    for i in range(30):
        lat = base[0] + random.uniform(-0.0003, 0.0003)
        lon = base[1] + random.uniform(-0.0003, 0.0003)
        route.append((lat, lon, "walking"))

    stop = (37.5088, 127.0635)
    for _ in range(42):
        lat = stop[0] + random.uniform(-0.00005, 0.00005)
        lon = stop[1] + random.uniform(-0.00005, 0.00005)
        route.append((lat, lon, "stationary"))

    return route


def simulate_normal_mode(
    route: list[tuple],
    interval_sec: int = 1,
    step_sec: int = 10,
    geofences: list = None,
    departure_time: datetime = None,
) -> SimulationResult:
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


def simulate_optimized_mode(
    route: list[tuple],
    step_sec: int = 10,
    geofences: list = None,
    departure_time: datetime = None,
) -> SimulationResult:
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

        if elapsed < next_call_at:
            continue

        point = LocationPoint(lat, lon, t)
        history_deque.append(point)
        if len(history_deque) > 5:
            history_deque.pop(0)

        opt = calculate_next_interval(lat, lon, history_deque, geofences)
        next_call_at = elapsed + opt.next_interval

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
