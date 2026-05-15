"""
POST /api/simulation/run            — Battery simulation: normal vs optimized mode comparison
POST /api/simulation/route/generate — Generate a sample route
"""

import random
from datetime import datetime
from flask import Blueprint, request, jsonify, abort

from gps_api.core.optimizer import Geofence
from gps_api.core.battery_simulation import (
    generate_route,
    simulate_normal_mode,
    simulate_optimized_mode,
)

bp = Blueprint("simulation", __name__)


def _parse_geofences(raw: list) -> list[Geofence]:
    return [
        Geofence(
            id=str(item.get("id", "fence")),
            lat=float(item["lat"]),
            lon=float(item["lon"]),
            radius=float(item.get("radius", 200)),
        )
        for item in raw
    ]


def _result_to_dict(result) -> dict:
    return {
        "mode": result.mode,
        "duration_sec": result.duration_sec,
        "total_battery_drain_pct": round(result.total_battery_drain, 4),
        "battery_per_hour_pct": round(result.battery_per_hour, 4),
        "total_gps_calls": result.total_gps_calls,
        "calls_per_min": round(result.calls_per_min, 2),
    }


def _mode_distribution(logs) -> dict:
    dist: dict[str, int] = {}
    for log in logs:
        dist[log.gps_mode] = dist.get(log.gps_mode, 0) + 1
    total = sum(dist.values()) or 1
    return {mode: {"count": cnt, "pct": round(cnt / total * 100, 1)} for mode, cnt in dist.items()}


@bp.post("/run")
def run_simulation():
    """
    Runs a battery simulation comparing normal mode vs optimized mode.

    Request JSON (all optional):
      {
        "route": [                             // coordinate list — auto-generated if omitted
          [37.498, 127.021, "vehicle"],
          ...
        ],
        "step_sec": 10,                        // step interval in seconds (default 10)
        "seed": 42,                            // random seed for reproducibility (optional)
        "geofences": [
          {"id": "dest", "lat": 37.5088, "lon": 127.0632, "radius": 200}
        ]
      }

    Response JSON:
      {
        "normal":    { ... },
        "optimized": { ... },
        "saving": {"battery_pct": 55.2, "gps_calls_pct": 70.1},
        "optimized_mode_distribution": { "HIGH": {...}, "LOW": {...} },
        "optimized_interval_samples": [ ... ]
      }
    """
    body = request.get_json(silent=True) or {}

    step_sec = int(body.get("step_sec", 10))
    if step_sec < 1:
        abort(400, description="step_sec must be at least 1.")

    seed = body.get("seed")
    if seed is not None:
        random.seed(int(seed))

    geofences = _parse_geofences(body.get("geofences") or [])
    geofences = geofences or None  # None falls back to battery_simulation defaults

    raw_route = body.get("route")
    if raw_route:
        try:
            route = [
                (float(p[0]), float(p[1]), str(p[2]) if len(p) > 2 else "unknown")
                for p in raw_route
            ]
        except (TypeError, IndexError, ValueError):
            abort(400, description="Invalid route format. Expected [[lat, lon, phase], ...].")
    else:
        route = generate_route()

    normal_result = simulate_normal_mode(route, step_sec=step_sec, geofences=geofences)
    optimized_result = simulate_optimized_mode(route, step_sec=step_sec, geofences=geofences)

    battery_saving = (
        (normal_result.total_battery_drain - optimized_result.total_battery_drain)
        / normal_result.total_battery_drain * 100
        if normal_result.total_battery_drain > 0 else 0.0
    )
    call_saving = (
        (normal_result.total_gps_calls - optimized_result.total_gps_calls)
        / normal_result.total_gps_calls * 100
        if normal_result.total_gps_calls > 0 else 0.0
    )

    samples = [
        {
            "timestamp": log.timestamp.isoformat(),
            "lat": log.lat,
            "lon": log.lon,
            "gps_mode": log.gps_mode,
            "interval": log.interval,
            "distance_to_fence": round(log.distance_to_fence, 1),
            "activity": log.activity,
            "cumulative_battery_pct": round(log.battery_drain, 4),
        }
        for log in optimized_result.logs[:20]
    ]

    return jsonify({
        "normal": _result_to_dict(normal_result),
        "optimized": _result_to_dict(optimized_result),
        "saving": {
            "battery_pct": round(battery_saving, 2),
            "gps_calls_pct": round(call_saving, 2),
        },
        "optimized_mode_distribution": _mode_distribution(optimized_result.logs),
        "optimized_interval_samples": samples,
    })


@bp.post("/route/generate")
def generate_sample_route():
    """
    Generates a sample route (Gangnam Station → Samsung Station) for simulation.

    Request JSON (optional):
      { "seed": 42, "noise": 0.00002 }

    Response JSON:
      { "route": [[lat, lon, phase], ...], "count": 120 }
    """
    body = request.get_json(silent=True) or {}
    seed = body.get("seed", 42)
    noise = float(body.get("noise", 0.00002))
    random.seed(int(seed))

    route = generate_route(noise=noise)
    return jsonify({
        "route": route,
        "count": len(route),
    })
