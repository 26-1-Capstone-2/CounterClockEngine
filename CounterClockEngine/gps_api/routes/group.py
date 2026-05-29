"""
Group Appointment API

REST:
  POST   /api/group/create              — Create a group
  POST   /api/group/<id>/join           — Join a group
  POST   /api/group/<id>/location       — Update location + recalculate all ETAs
  GET    /api/group/<id>                — Get group status
  POST   /api/group/<id>/arrive         — Mark arrival
  DELETE /api/group/<id>/leave          — Leave a group

WebSocket and client Push are handled by the Spring server.
Engine (CounterClockEngine) returns calculation results only via REST response.
"""

from datetime import datetime

from flask import Blueprint, request, jsonify, abort

from gps_api.core import group as group_core

bp = Blueprint("group", __name__)


@bp.post("/join-by-invite")
def join_by_invite():
    """
    Finds and joins an appointment using an invite code.

    Request JSON:
      {
        "invite_code": "ABC123",
        "member_id": "user_002",
        "name": "이영희",
        "travel_mode": "transit",           // optional
        "origin": [37.4979, 127.0276],      // optional (origin coordinates)
        "origin_name": "집",                // optional
        "origin_address": "서울시 ..."      // optional
      }
    """
    body = request.get_json(silent=True) or {}
    for f in ("invite_code", "member_id", "name"):
        if f not in body:
            abort(400, description=f"{f} field is required.")

    group = group_core.get_group_by_invite(body["invite_code"])
    if not group:
        abort(404, description=f"invite_code '{body['invite_code']}' not found.")

    origin_lat, origin_lon = None, None
    if body.get("origin"):
        try:
            origin_lat, origin_lon = float(body["origin"][0]), float(body["origin"][1])
        except (TypeError, IndexError, ValueError):
            abort(400, description="origin must be [lat, lon].")

    participant = group_core.join_group(
        group_id=group.group_id,
        member_id=body["member_id"],
        name=body["name"],
        travel_mode=body.get("travel_mode", "transit"),
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        origin_name=body.get("origin_name"),
        origin_address=body.get("origin_address"),
    )
    if participant is None:
        abort(500, description="Failed to join group.")

    return jsonify({
        "group_id": group.group_id,
        "title": group.title,
        "participant_id": participant.participant_id,
        "member_id": participant.member_id,
        "name": participant.name,
        "status": participant.status,
    })


def _parse_loc(value, name: str) -> tuple[float, float]:
    try:
        return float(value[0]), float(value[1])
    except (TypeError, IndexError, ValueError):
        abort(400, description=f"{name} must be [lat, lon].")


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        abort(400, description="appointment_time must be ISO 8601 (e.g. 2026-05-14T15:00:00).")


# ------------------------------------------------------------------
# REST endpoints
# ------------------------------------------------------------------

@bp.post("/create")
def create():
    """
    Creates a group.

    Request JSON:
      {
        "name": "금요일 저녁 약속",
        "destination": [37.5088, 127.0632],
        "appointment_time": "2026-05-23T19:00:00"
      }

    Response JSON:
      {
        "group_id": "uuid",
        "name": "...",
        "destination": [lat, lon],
        "appointment_time": "..."
      }
    """
    body = request.get_json(silent=True) or {}
    for f in ("name", "destination", "appointment_time"):
        if f not in body:
            abort(400, description=f"{f} field is required.")

    group = group_core.create_group(
        title=body["name"],
        destination=_parse_loc(body["destination"], "destination"),
        appointment_time=_parse_dt(body["appointment_time"]),
    )
    return jsonify({
        "group_id": group.group_id,
        "name": group.title,
        "destination": list(group.destination),
        "appointment_time": group.appointment_time.isoformat(),
    }), 201


@bp.post("/<group_id>/join")
def join(group_id: str):
    """
    Joins a group.

    Request JSON:
      { "user_id": "user_001", "name": "김철수" }
    """
    body = request.get_json(silent=True) or {}
    for f in ("user_id", "name"):
        if f not in body:
            abort(400, description=f"{f} field is required.")

    origin_lat, origin_lon = None, None
    if body.get("origin"):
        try:
            origin_lat, origin_lon = float(body["origin"][0]), float(body["origin"][1])
        except (TypeError, IndexError, ValueError):
            abort(400, description="origin must be [lat, lon].")

    participant = group_core.join_group(
        group_id=group_id,
        member_id=body["user_id"],
        name=body["name"],
        travel_mode=body.get("travel_mode", "transit"),
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        origin_name=body.get("origin_name"),
        origin_address=body.get("origin_address"),
    )
    if participant is None:
        abort(404, description=f"Group '{group_id}' not found.")

    return jsonify({
        "group_id": group_id,
        "participant_id": participant.participant_id,
        "member_id": participant.member_id,
        "name": participant.name,
        "is_host": participant.is_host,
        "travel_mode": participant.travel_mode,
        "status": participant.status,
    })


@bp.post("/<group_id>/location")
def update_location(group_id: str):
    """
    Updates a participant's current location.
    Computes ETAs for all participants in parallel and returns the results.
    Client Push (WebSocket/FCM) is handled by Spring after receiving this response.

    Request JSON:
      {
        "user_id": "user_001",
        "current_loc": [37.4979, 127.0276],
        "kakao_api_key": "..."   // optional
      }

    Response JSON:
      {
        "summary": {
          "group_id": "uuid",
          "participants": [
            {
              "member_id": "user_001",
              "name": "김철수",
              "eta_sec": 540,
              "eta_min": 9.0,
              "status": "on_time"
            }
          ]
        },
        "gps_intervals": {
          "user_001": { "next_interval_sec": 10, "gps_mode": "HIGH" },
          "user_002": { "next_interval_sec": 30, "gps_mode": "BALANCED" }
        }
      }
    """
    body = request.get_json(silent=True) or {}
    for f in ("user_id", "current_loc"):
        if f not in body:
            abort(400, description=f"{f} field is required.")

    curr = _parse_loc(body["current_loc"], "current_loc")
    kakao_key = body.get("kakao_api_key", "")

    result = group_core.update_location(group_id, body["user_id"], curr[0], curr[1], kakao_key)
    if result is None:
        abort(404, description="Group or user not found.")

    summary, gps_intervals = result
    return jsonify({"summary": summary, "gps_intervals": gps_intervals})


@bp.get("/<group_id>")
def get_group(group_id: str):
    """Returns the current ETA status of the group."""
    group = group_core.get_group(group_id)
    if not group:
        abort(404, description=f"Group '{group_id}' not found.")
    return jsonify(group_core.get_group_summary(group))


@bp.post("/<group_id>/arrive")
def arrive(group_id: str):
    """
    Notifies that a participant has arrived.
    Spring receives this response's summary and pushes it to the entire group.

    Request JSON:
      { "user_id": "user_001" }

    Response JSON:
      {
        "group_id": "uuid",
        "user_id": "user_001",
        "status": "arrived",
        "summary": { ... }
      }
    """
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    if not user_id:
        abort(400, description="user_id field is required.")

    summary = group_core.mark_arrived(group_id, user_id)
    if summary is None:
        abort(404, description="Group or user not found.")

    return jsonify({"group_id": group_id, "user_id": user_id, "status": "arrived", "summary": summary})


@bp.delete("/<group_id>/leave")
def leave(group_id: str):
    """
    Leaves a group.

    Request JSON:
      { "user_id": "user_001" }
    """
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    if not user_id:
        abort(400, description="user_id field is required.")

    removed = group_core.leave_group(group_id, user_id)
    if not removed:
        abort(404, description="Group or user not found.")

    group = group_core.get_group(group_id)
    summary = group_core.get_group_summary(group) if group else None

    return jsonify({"group_id": group_id, "user_id": user_id, "left": True, "summary": summary})
