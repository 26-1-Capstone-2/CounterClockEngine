"""
POST /api/route/fetch      — Fetch route via Kakao Mobility API
POST /api/route/simulate   — Fetch route + battery simulation combined
POST /api/route/departure  — Calculate departure time from target arrival time
"""

from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, abort, current_app

from gps_api.core.kakao_route import fetch_route, resample_route, calculate_departure_time
from gps_api.core.optimizer import Geofence
from gps_api.core.battery_simulation import simulate_normal_mode, simulate_optimized_mode

bp = Blueprint("kakao", __name__)


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


def _get_api_key(body: dict) -> str:
    key = body.get("api_key") or current_app.config.get("KAKAO_API_KEY", "")
    if not key:
        abort(400, description="api_key is required. Provide it in the request body or set the KAKAO_API_KEY environment variable.")
    return key


@bp.post("/fetch")
def route_fetch():
    """
    Calls the Kakao Mobility Directions API and returns route coordinates and duration.

    Request JSON:
      {
        "origin_lat": 37.4979, "origin_lon": 127.0276,
        "dest_lat":   37.5088, "dest_lon":   127.0632,
        "api_key":    "...",    // optional if KAKAO_API_KEY env var is set
        "priority":   "RECOMMEND"
      }

    Response JSON:
      {
        "coords": [[lat, lon], ...],
        "duration_sec": 480,
        "distance_m": 3200,
        "num_coords": 42
      }
    """
    body = request.get_json(silent=True) or {}
    api_key = _get_api_key(body)

    try:
        origin_lat = float(body["origin_lat"])
        origin_lon = float(body["origin_lon"])
        dest_lat   = float(body["dest_lat"])
        dest_lon   = float(body["dest_lon"])
    except (KeyError, TypeError, ValueError):
        abort(400, description="origin_lat, origin_lon, dest_lat, dest_lon fields are required.")

    priority = body.get("priority", "RECOMMEND")

    try:
        coords, duration_sec, distance_m = fetch_route(
            origin_lat, origin_lon, dest_lat, dest_lon, api_key, priority
        )
    except Exception as e:
        abort(400, description=str(e))

    return jsonify({
        "coords": coords,
        "duration_sec": duration_sec,
        "distance_m": distance_m,
        "num_coords": len(coords),
    })


@bp.post("/simulate")
def route_simulate():
    """
    Fetches a Kakao route, resamples it, and runs a battery simulation in one call.

    Request JSON:
      {
        "origin_lat": 37.4979, "origin_lon": 127.0276,
        "dest_lat":   37.5088, "dest_lon":   127.0632,
        "api_key":    "...",
        "step_sec":   10,
        "priority":   "RECOMMEND",
        "geofences":  []          // optional; destination is auto-registered as a geofence
      }

    Response JSON:
      {
        "route_info": { "duration_sec": ..., "distance_m": ..., "num_steps": ... },
        "normal": { ... },
        "optimized": { ... },
        "saving": { "battery_pct": ..., "gps_calls_pct": ... },
        "optimized_mode_distribution": { ... },
        "optimized_interval_samples": [ ... ]
      }
    """
    body = request.get_json(silent=True) or {}
    api_key = _get_api_key(body)

    try:
        origin_lat = float(body["origin_lat"])
        origin_lon = float(body["origin_lon"])
        dest_lat   = float(body["dest_lat"])
        dest_lon   = float(body["dest_lon"])
    except (KeyError, TypeError, ValueError):
        abort(400, description="origin_lat, origin_lon, dest_lat, dest_lon fields are required.")

    step_sec = int(body.get("step_sec", 10))
    priority = body.get("priority", "RECOMMEND")

    extra_fences = _parse_geofences(body.get("geofences") or [])
    dest_fence = Geofence("destination", dest_lat, dest_lon, 200)
    geofences = [dest_fence] + extra_fences

    try:
        coords, duration_sec, distance_m = fetch_route(
            origin_lat, origin_lon, dest_lat, dest_lon, api_key, priority
        )
    except Exception as e:
        abort(400, description=str(e))

    route = resample_route(coords, duration_sec, step_sec=step_sec)

    normal_result    = simulate_normal_mode(route, step_sec=step_sec, geofences=geofences)
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
        }
        for log in optimized_result.logs[:20]
    ]

    def _to_dict(r):
        return {
            "mode": r.mode,
            "duration_sec": r.duration_sec,
            "total_battery_drain_pct": round(r.total_battery_drain, 4),
            "battery_per_hour_pct": round(r.battery_per_hour, 4),
            "total_gps_calls": r.total_gps_calls,
            "calls_per_min": round(r.calls_per_min, 2),
        }

    dist: dict[str, int] = {}
    for log in optimized_result.logs:
        dist[log.gps_mode] = dist.get(log.gps_mode, 0) + 1
    total = sum(dist.values()) or 1
    mode_dist = {m: {"count": c, "pct": round(c / total * 100, 1)} for m, c in dist.items()}

    return jsonify({
        "route_info": {
            "duration_sec": duration_sec,
            "distance_m": distance_m,
            "num_steps": len(route),
        },
        "normal": _to_dict(normal_result),
        "optimized": _to_dict(optimized_result),
        "saving": {
            "battery_pct": round(battery_saving, 2),
            "gps_calls_pct": round(call_saving, 2),
        },
        "optimized_mode_distribution": mode_dist,
        "optimized_interval_samples": samples,
    })


@bp.post("/departure")
def route_departure():
    """
    Calculates the departure time needed to reach the destination by a target arrival time.

    Request JSON:
      {
        "origin_lat": 37.4979, "origin_lon": 127.0276,
        "dest_lat":   37.5088, "dest_lon":   127.0632,
        "api_key":    "...",
        "arrival_time": "2026-05-14T18:00:00",
        "priority": "RECOMMEND"
      }

    Response JSON:
      {
        "arrival_time":   "2026-05-14T18:00:00",
        "departure_time": "2026-05-14T17:52:00",
        "duration_sec":   480,
        "distance_m":     3200,
        "seconds_until_departure": 300,
        "is_late": false
      }
    """
    body = request.get_json(silent=True) or {}
    api_key = _get_api_key(body)

    try:
        origin_lat = float(body["origin_lat"])
        origin_lon = float(body["origin_lon"])
        dest_lat   = float(body["dest_lat"])
        dest_lon   = float(body["dest_lon"])
    except (KeyError, TypeError, ValueError):
        abort(400, description="origin_lat, origin_lon, dest_lat, dest_lon fields are required.")

    arrival_str = body.get("arrival_time")
    if not arrival_str:
        abort(400, description="arrival_time field is required. (ISO format: 2026-05-14T18:00:00)")
    try:
        arrival_time = datetime.fromisoformat(arrival_str)
    except ValueError:
        abort(400, description="Invalid arrival_time format. Use ISO format (e.g. 2026-05-14T18:00:00)")

    priority = body.get("priority", "RECOMMEND")

    try:
        departure_time, duration_sec, distance_m = calculate_departure_time(
            origin_lat, origin_lon, dest_lat, dest_lon, api_key, arrival_time, priority
        )
    except Exception as e:
        abort(400, description=str(e))

    now = datetime.now()
    seconds_until = int((departure_time - now).total_seconds())

    return jsonify({
        "arrival_time": arrival_time.isoformat(),
        "departure_time": departure_time.isoformat(),
        "duration_sec": duration_sec,
        "distance_m": distance_m,
        "seconds_until_departure": seconds_until,
        "is_late": seconds_until < 0,
    })
