"""
개인 여정 API

GET  /api/journey/<journey_id>          — 여정 단건 조회
GET  /api/journey/member/<member_id>    — 멤버의 여정 목록 조회
POST /api/journey/<journey_id>/location — 현재 위치 업데이트 + ETA 재계산
POST /api/journey/eta                   — 임시 ETA 계산 (DB 저장 없음, 데모/테스트용)

WebSocket:
  server → client: emit("journey_update", {journey_dict})  — 위치 업데이트 시 개인 알림
"""

from flask import Blueprint, request, jsonify, abort

from gps_api.core import journey as journey_core
from gps_api.extensions import socketio

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
    """여정 단건 조회."""
    journey = journey_core.get_journey(journey_id)
    if not journey:
        abort(404, description=f"Journey '{journey_id}' not found.")
    return jsonify(journey_core.to_dict(journey))


@bp.get("/member/<member_id>")
def get_member_journeys(member_id: str):
    """멤버의 전체 여정 목록 조회."""
    journeys = journey_core.get_member_journeys(member_id)
    return jsonify({
        "member_id": member_id,
        "total": len(journeys),
        "journeys": [journey_core.to_dict(j) for j in journeys],
    })


@bp.post("/<journey_id>/location")
def update_location(journey_id: str):
    """
    현재 위치를 업데이트하고 ETA와 출발 알람 시각을 재계산합니다.
    DB에 결과를 write-back하고 WebSocket으로 본인에게 알립니다.

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
        ...
      }
    """
    body = request.get_json(silent=True) or {}
    if "current_loc" not in body:
        abort(400, description="current_loc field is required.")

    curr = _parse_loc(body["current_loc"], "current_loc")
    kakao_key = body.get("kakao_api_key", "")

    result = journey_core.update_location(journey_id, curr[0], curr[1], kakao_key)
    if result is None:
        abort(404, description=f"Journey '{journey_id}' not found.")

    # 본인에게만 업데이트 알림 (room = journey_id)
    socketio.emit("journey_update", result, room=journey_id)
    return jsonify(result)


@bp.post("/eta")
def eta_preview():
    """
    DB 저장 없이 즉시 ETA를 계산합니다 (데모/테스트용).

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


# ------------------------------------------------------------------
# WebSocket
# ------------------------------------------------------------------

@socketio.on("join_journey")
def handle_join_journey(data):
    """
    본인의 여정 room에 입장해 journey_update 이벤트를 수신합니다.

    emit("join_journey", {"journey_id": "..."})
    """
    from flask_socketio import join_room
    journey_id = data.get("journey_id")
    if journey_id:
        join_room(journey_id)
