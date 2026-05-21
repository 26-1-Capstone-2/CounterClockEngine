"""
POST /api/optimizer/interval
  GPS interval optimization (distance-based + Activity Recognition + SLC)
"""

from datetime import datetime
from flask import Blueprint, request, jsonify, abort

from gps_api.core.optimizer import calculate_next_interval, LocationPoint, Geofence

bp = Blueprint("optimizer", __name__)


def _parse_geofences(raw: list) -> list[Geofence]:
    fences = []
    for item in raw:
        fences.append(Geofence(
            id=str(item.get("id", "fence")),
            lat=float(item["lat"]),
            lon=float(item["lon"]),
            radius=float(item.get("radius", 200)),
        ))
    return fences


def _parse_history(raw: list) -> list[LocationPoint]:
    points = []
    for item in raw:
        ts_str = item.get("timestamp")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                ts = datetime.now()
        else:
            ts = datetime.now()
        points.append(LocationPoint(
            lat=float(item["lat"]),
            lon=float(item["lon"]),
            timestamp=ts,
        ))
    return points


@bp.post("/interval")
def get_interval():
    """
    Calculates the next GPS polling interval based on current location, history, and geofences.

    Request JSON:
      {
        "lat": 37.5,
        "lon": 127.0,
        "history": [                          // optional, recent location history (max 5)
          {"lat": 37.49, "lon": 127.01, "timestamp": "2026-05-14T10:00:00"}
        ],
        "geofences": [                        // optional
          {"id": "dest", "lat": 37.5088, "lon": 127.0632, "radius": 200}
        ]
      }

    Response JSON:
      {
        "next_interval": 10,
        "activity": "vehicle",
        "gps_mode": "BALANCED",
        "is_significant_change": true,
        "moved_distance": 320.5,
        "distance_to_nearest_fence": 450.2,
        "entered_zones": [],
        "debug": {"base_interval": 10, "activity_multiplier": 0.3, "slc_multiplier": 1.0}
      }
    """
    body = request.get_json(silent=True) or {}

    lat = body.get("lat")
    lon = body.get("lon")
    if lat is None or lon is None:
        abort(400, description="lat and lon fields are required.")

    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        abort(400, description="lat and lon must be numeric.")

    history = _parse_history(body.get("history") or [])
    geofences = _parse_geofences(body.get("geofences") or [])

    # Append current position to history
    history.append(LocationPoint(lat, lon, datetime.now()))
    if len(history) > 5:
        history = history[-5:]

    result = calculate_next_interval(lat, lon, history, geofences)

    return jsonify({
        "next_interval": result.next_interval,
        "activity": result.activity,
        "gps_mode": result.gps_mode,
        "is_significant_change": result.is_significant_change,
        "moved_distance": result.moved_distance,
        "distance_to_nearest_fence": result.distance_to_nearest_fence,
        "entered_zones": result.entered_zones,
        "debug": {
            "u_dist": result.u_dist,
            "u_time": result.u_time,
            "urgency": result.urgency,
            "activity_multiplier": result.activity_multiplier,
            "slc_multiplier": result.slc_multiplier,
        },
    })
