"""
POST /api/eta/calculate — Calculate ETA from current location to destination
POST /api/eta/movement  — Analyze a single movement step (gradient descent based)
POST /api/eta/track     — Simulate tracking over a sequence of waypoints
"""

from flask import Blueprint, request, jsonify, abort

from gps_api.core.eta_functions import (
    calculate_eta,
    handle_movement,
    compute_gradient,
)

bp = Blueprint("eta", __name__)


def _parse_loc(value, name: str) -> tuple[float, float]:
    try:
        lat, lon = float(value[0]), float(value[1])
        return lat, lon
    except (TypeError, IndexError, ValueError):
        abort(400, description=f"{name} must be in [lat, lon] format.")


@bp.post("/calculate")
def eta_calculate():
    """
    Calculates ETA from the current location to the destination.

    Request JSON:
      {
        "current_loc": [37.4979, 127.0276],
        "destination": [37.5088, 127.0632],
        "speed_mps": 1.4          // optional, default 1.4 m/s (walking pace)
      }

    Response JSON:
      {
        "eta_sec": 1520,
        "eta_min": 25.3,
        "distance_m": 2128.0
      }
    """
    body = request.get_json(silent=True) or {}

    if "current_loc" not in body or "destination" not in body:
        abort(400, description="current_loc and destination fields are required.")

    curr = _parse_loc(body["current_loc"], "current_loc")
    dest = _parse_loc(body["destination"], "destination")
    speed = float(body.get("speed_mps", 1.4))
    if speed <= 0:
        abort(400, description="speed_mps must be a positive number.")

    from gps_api.core.eta_functions import haversine
    distance_m = haversine(curr, dest)
    eta_sec = distance_m / speed

    return jsonify({
        "eta_sec": round(eta_sec, 1),
        "eta_min": round(eta_sec / 60, 2),
        "distance_m": round(distance_m, 1),
    })


@bp.post("/movement")
def eta_movement():
    """
    Analyzes a single movement step and returns an ETA update strategy.

    Request JSON:
      {
        "prev_loc":    [37.4979, 127.0276],
        "curr_loc":    [37.5010, 127.0350],
        "destination": [37.5088, 127.0632],
        "velocity":    0.0,          // previous velocity (start at 0)
        "threshold":   50.0,         // optional, default 50m
        "speed_mps":   1.4           // optional, for ETA calculation
      }

    Response JSON:
      {
        "action":        "advance_eta",
        "message":       "...",
        "next_interval": 30.0,
        "velocity":      -8.5,
        "gradient":      -85.2,
        "eta_sec":       900,
        "eta_min":       15.0
      }
    """
    body = request.get_json(silent=True) or {}

    for field in ("prev_loc", "curr_loc", "destination"):
        if field not in body:
            abort(400, description=f"{field} field is required.")

    prev = _parse_loc(body["prev_loc"], "prev_loc")
    curr = _parse_loc(body["curr_loc"], "curr_loc")
    dest = _parse_loc(body["destination"], "destination")

    velocity  = float(body.get("velocity", 0.0))
    threshold = float(body.get("threshold", 50.0))
    speed     = float(body.get("speed_mps", 1.4))

    result = handle_movement(prev, curr, dest, velocity, threshold)
    eta_sec = calculate_eta(curr, dest, speed)

    return jsonify({
        "action":        result["action"],
        "message":       result["message"],
        "next_interval": result["next_interval"],
        "velocity":      round(result["velocity"], 4),
        "gradient":      round(result["gradient"], 2),
        "eta_sec":       round(eta_sec, 1),
        "eta_min":       round(eta_sec / 60, 2),
    })


@bp.post("/track")
def eta_track():
    """
    Tracks a sequence of waypoints and returns ETA and movement analysis at each step.

    Request JSON:
      {
        "waypoints":   [[37.4979, 127.0276], [37.5010, 127.0350], ...],
        "destination": [37.5088, 127.0632],
        "threshold":   50.0,
        "speed_mps":   1.4
      }

    Response JSON:
      {
        "destination": [lat, lon],
        "steps": [
          {
            "step": 1,
            "location": [lat, lon],
            "action": "advance_eta",
            "message": "...",
            "next_interval": 30.0,
            "velocity": -8.5,
            "gradient": -85.2,
            "eta_sec": 900,
            "eta_min": 15.0
          },
          ...
        ]
      }
    """
    body = request.get_json(silent=True) or {}

    if "waypoints" not in body or "destination" not in body:
        abort(400, description="waypoints and destination fields are required.")

    raw_wp = body["waypoints"]
    if not isinstance(raw_wp, list) or len(raw_wp) < 2:
        abort(400, description="waypoints must be an array of at least 2 coordinates.")

    dest      = _parse_loc(body["destination"], "destination")
    threshold = float(body.get("threshold", 50.0))
    speed     = float(body.get("speed_mps", 1.4))

    try:
        waypoints = [(_parse_loc(wp, f"waypoints[{i}]")) for i, wp in enumerate(raw_wp)]
    except Exception:
        abort(400, description="Invalid waypoints format. Expected [[lat, lon], ...].")

    steps = []
    velocity = 0.0

    for i in range(1, len(waypoints)):
        prev = waypoints[i - 1]
        curr = waypoints[i]

        result = handle_movement(prev, curr, dest, velocity, threshold)
        velocity = result["velocity"]
        eta_sec = calculate_eta(curr, dest, speed)

        steps.append({
            "step": i,
            "location": list(curr),
            "action": result["action"],
            "message": result["message"],
            "next_interval": result["next_interval"],
            "velocity": round(velocity, 4),
            "gradient": round(result["gradient"], 2),
            "eta_sec": round(eta_sec, 1),
            "eta_min": round(eta_sec / 60, 2),
        })

    return jsonify({
        "destination": list(dest),
        "steps": steps,
    })
