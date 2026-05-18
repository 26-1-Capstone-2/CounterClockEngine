"""
그룹 약속 API

REST:
  POST   /api/group/create              — 그룹 생성
  POST   /api/group/<id>/join           — 그룹 참가
  POST   /api/group/<id>/location       — 위치 업데이트 + 전체 ETA 재계산 + broadcast
  GET    /api/group/<id>                — 그룹 현황 조회
  POST   /api/group/<id>/arrive         — 도착 처리
  DELETE /api/group/<id>/leave          — 그룹 탈퇴

WebSocket:
  client → server : emit("join_group", {"group_id": "..."})
  server → client : emit("group_update", {group_summary})
"""

from datetime import datetime

from flask import Blueprint, request, jsonify, abort
from flask_socketio import join_room

from gps_api.core import group as group_core
from gps_api.extensions import socketio

bp = Blueprint("group", __name__)


@bp.post("/join-by-invite")
def join_by_invite():
    """
    초대 코드로 약속을 찾아 참가합니다.

    Request JSON:
      {
        "invite_code": "ABC123",
        "member_id": "user_002",
        "name": "이영희",
        "travel_mode": "transit",           // optional
        "origin": [37.4979, 127.0276],      // optional (출발지 좌표)
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
    그룹을 생성합니다.

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
        name=body["name"],
        destination=_parse_loc(body["destination"], "destination"),
        appointment_time=_parse_dt(body["appointment_time"]),
    )
    return jsonify({
        "group_id": group.group_id,
        "name": group.name,
        "destination": list(group.destination),
        "appointment_time": group.appointment_time.isoformat(),
    }), 201


@bp.post("/<group_id>/join")
def join(group_id: str):
    """
    그룹에 참가합니다.

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
    참가자의 현재 위치를 업데이트합니다.
    서버에서 전체 참가자 ETA를 병렬 계산하고
    WebSocket으로 그룹 전체에 결과를 broadcast합니다.

    Request JSON:
      {
        "user_id": "user_001",
        "current_loc": [37.4979, 127.0276],
        "kakao_api_key": "..."   // optional
      }
    """
    body = request.get_json(silent=True) or {}
    for f in ("user_id", "current_loc"):
        if f not in body:
            abort(400, description=f"{f} field is required.")

    curr = _parse_loc(body["current_loc"], "current_loc")
    kakao_key = body.get("kakao_api_key", "")

    summary = group_core.update_location(group_id, body["user_id"], curr[0], curr[1], kakao_key)
    if summary is None:
        abort(404, description="Group or user not found.")

    socketio.emit("group_update", summary, room=group_id)
    return jsonify(summary)


@bp.get("/<group_id>")
def get_group(group_id: str):
    """그룹의 현재 ETA 현황을 반환합니다."""
    group = group_core.get_group(group_id)
    if not group:
        abort(404, description=f"Group '{group_id}' not found.")
    return jsonify(group_core.get_group_summary(group))


@bp.post("/<group_id>/arrive")
def arrive(group_id: str):
    """
    참가자가 도착했음을 알립니다.
    WebSocket으로 그룹 전체에 broadcast됩니다.

    Request JSON:
      { "user_id": "user_001" }
    """
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    if not user_id:
        abort(400, description="user_id field is required.")

    summary = group_core.mark_arrived(group_id, user_id)
    if summary is None:
        abort(404, description="Group or user not found.")

    socketio.emit("group_update", summary, room=group_id)
    return jsonify({"group_id": group_id, "user_id": user_id, "status": "arrived"})


@bp.delete("/<group_id>/leave")
def leave(group_id: str):
    """
    그룹에서 탈퇴합니다.

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
    if group:
        socketio.emit("group_update", group_core.get_group_summary(group), room=group_id)

    return jsonify({"group_id": group_id, "user_id": user_id, "left": True})


# ------------------------------------------------------------------
# WebSocket event handlers
# ------------------------------------------------------------------

@socketio.on("join_group")
def handle_join_group(data):
    """
    클라이언트가 그룹 room에 입장합니다.
    이후 해당 그룹의 group_update 이벤트를 수신합니다.

    emit("join_group", {"group_id": "..."})
    """
    group_id = data.get("group_id")
    if not group_id:
        return
    join_room(group_id)

    group = group_core.get_group(group_id)
    if group:
        socketio.emit("group_update", group_core.get_group_summary(group), room=group_id)
