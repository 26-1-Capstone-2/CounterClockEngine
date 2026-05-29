"""
Personal Journey API

GET  /api/journey/<journey_id>          — Retrieve a single journey
GET  /api/journey/member/<member_id>    — Retrieve a member's journey list
POST /api/journey/<journey_id>/location — Update current location + recalculate ETA
POST /api/journey/eta                   — Temporary ETA calculation (no DB save, for demo/testing)

Client Push is handled by Spring.
Engine (CounterClockEngine) returns calculation results only via REST response.
"""

from flask import Blueprint, request, jsonify, abort

from gps_api.core import journey as journey_core

bp = Blueprint("journey", __name__)


def _parse_loc(value, name: str) -> tuple[float, float]:
    try:
        return float(value[0]), float(value[1])
    except (TypeError, IndexError, ValueError):
        abort(400, description=f"{name} must be [lat, lon].")


# ------------------------------------------------------------------
# REST endpoints
# ------------------------------------------------------------------

@bp.get("/<journey_id>")
def get_journey(journey_id: str):
    """Retrieve a single journey."""
    journey = journey_core.get_journey(journey_id)
    if not journey:
        abort(404, description=f"Journey '{journey_id}' not found.")
    return jsonify(journey_core.to_dict(journey))


@bp.get("/member/<member_id>")
def get_member_journeys(member_id: str):
    """Retrieve all journeys for a member."""
    journeys = journey_core.get_member_journeys(member_id)
    return jsonify({
        "member_id": member_id,
        "total": len(journeys),
        "journeys": [journey_core.to_dict(j) for j in journeys],
    })


@bp.post("/<journey_id>/location")
def update_location(journey_id: str):
    """
    Updates the current location and recalculates ETA and departure alarm time.
    Writes results back to DB and notifies the user via WebSocket.

    Request JSON:
      {
        "current_loc": [37.4979, 127.0276],
        "kakao_api_key": "..."   // optional
      }

    Response JSON:
      {
        "journey_id": "...",
        "eta_sec": 900,
        "eta_min": 15.0,
        "alarm_time": "2026-05-23T18:43:00",
        "status": "on_time",
        "next_interval_sec": 30,
        "gps_mode": "BALANCED"
      }
    Spring receives this response and pushes it to the corresponding member.
    """
    body = request.get_json(silent=True) or {}
    if "current_loc" not in body:
        abort(400, description="current_loc field is required.")

    curr = _parse_loc(body["current_loc"], "current_loc")
    kakao_key = body.get("kakao_api_key", "")

    result = journey_core.update_location(journey_id, curr[0], curr[1], kakao_key)
    if result is None:
        abort(404, description=f"Journey '{journey_id}' not found.")

    return jsonify(result)


@bp.post("/eta")
def eta_preview():
    """
    Calculates ETA immediately without saving to DB (for demo/testing).

    Request JSON:
      {
        "member_id": "user_001",
        "current_loc": [37.4979, 127.0276],
        "destination": [37.5088, 127.0632],
        "goal_time": "2026-05-23T19:00:00",  // optional
        "travel_mode": "transit",             // optional
        "kakao_api_key": "..."                // optional
      }
    """
    body = request.get_json(silent=True) or {}
    for f in ("member_id", "current_loc", "destination"):
        if f not in body:
            abort(400, description=f"{f} field is required.")

    curr = _parse_loc(body["current_loc"], "current_loc")
    dest = _parse_loc(body["destination"], "destination")

    from gps_api.core.journey import Journey, compute_eta
    from datetime import datetime

    goal_time = None
    if body.get("goal_time"):
        try:
            goal_time = datetime.fromisoformat(body["goal_time"])
        except ValueError:
            abort(400, description="goal_time must be ISO 8601.")

    j = Journey(
        journey_id="preview",
        member_id=body["member_id"],
        title="preview",
        current_lat=curr[0],
        current_lon=curr[1],
        dest_lat=dest[0],
        dest_lon=dest[1],
        travel_mode=body.get("travel_mode", "transit"),
        goal_time=goal_time,
    )
    compute_eta(j, body.get("kakao_api_key", ""))
    return jsonify(journey_core.to_dict(j))

