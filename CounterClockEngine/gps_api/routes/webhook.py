"""
DB 서버 → GPS 서버 Webhook 수신 엔드포인트.

DB 서버는 아래 두 가지 상황에서 즉시 POST합니다.
  1. 데이터 생성·변경 시 → 캐시 갱신
  2. 참가자가 출발지 500m 지오펜스를 이탈 시 → 이동 시작 처리 + WebSocket emit

엔드포인트:
  POST /webhook/db-sync

type별 처리:
  캐시 갱신:
    - "appointment"      : 약속 정보 upsert
    - "participant"      : 단일 참가자 upsert
    - "participants"     : 약속의 참가자 전체 목록 교체
    - "member_settings"  : 멤버 설정(버퍼 등) upsert
    - "journey"          : 개인 여정 단건 upsert
    - "member_journeys"  : 멤버의 여정 목록 전체 교체
  이벤트:
    - "geofence_exit"    : 이동 시작 처리 → group_update + request_gps emit
"""

from flask import Blueprint, request, jsonify, abort

from gps_api.core import cache
from gps_api.core import group as group_core
from gps_api.extensions import socketio

bp = Blueprint("webhook", __name__)


@bp.post("/db-sync")
def db_sync():
    body = request.get_json(silent=True) or {}
    event_type = body.get("type")

    if not event_type:
        abort(400, description="'type' field is required.")

    # ------------------------------------------------------------------
    # 캐시 갱신 이벤트
    # ------------------------------------------------------------------

    data = body.get("data")

    if event_type == "appointment":
        if not isinstance(data, dict):
            abort(400, description="'data' must be an object for type='appointment'.")
        cache.put_appointment(data)
        return jsonify({"cached": "appointment", "id": data.get("appointment_id")}), 200

    if event_type == "participant":
        if not isinstance(data, dict):
            abort(400, description="'data' must be an object for type='participant'.")
        appt_id = data.get("appointment_id")
        if not appt_id:
            abort(400, description="'appointment_id' is required inside participant data.")
        cache.put_participant(appt_id, data)
        return jsonify({"cached": "participant", "id": data.get("participant_id")}), 200

    if event_type == "participants":
        appt_id = body.get("appointment_id")
        if not appt_id:
            abort(400, description="'appointment_id' is required for type='participants'.")
        if not isinstance(data, list):
            abort(400, description="'data' must be an array for type='participants'.")
        cache.put_participants(appt_id, data)
        return jsonify({"cached": "participants", "count": len(data)}), 200

    # ------------------------------------------------------------------
    # 지오펜스 이탈 이벤트
    # ------------------------------------------------------------------

    if event_type == "geofence_exit":
        for field in ("appointment_id", "member_id", "current_lat", "current_lon"):
            if field not in body:
                abort(400, description=f"'{field}' is required for type='geofence_exit'.")

        appointment_id = body["appointment_id"]
        member_id = body["member_id"]
        try:
            lat = float(body["current_lat"])
            lon = float(body["current_lon"])
        except (TypeError, ValueError):
            abort(400, description="'current_lat' and 'current_lon' must be numbers.")

        result = group_core.handle_geofence_exit(appointment_id, member_id, lat, lon)
        if result is None:
            abort(404, description=f"Appointment '{appointment_id}' or member '{member_id}' not found.")

        summary, gps_intervals = result

        # 그룹 전체에 ETA 현황 broadcast
        socketio.emit("group_update", summary, room=appointment_id)

        # 각 참가자 개인 room에 GPS 갱신 주기 emit
        # (이탈한 참가자는 BALANCED 10초, 나머지는 Adaptive Interval)
        for mid, interval_info in gps_intervals.items():
            socketio.emit(
                "request_gps",
                {"group_id": appointment_id, **interval_info},
                room=f"member:{mid}",
            )

        return jsonify({
            "triggered": "geofence_exit",
            "appointment_id": appointment_id,
            "member_id": member_id,
            "gps_interval": gps_intervals.get(member_id),
        }), 200

    # ------------------------------------------------------------------
    # 멤버 설정 캐시
    # ------------------------------------------------------------------

    if event_type == "member_settings":
        if not isinstance(data, dict):
            abort(400, description="'data' must be an object for type='member_settings'.")
        member_id = data.get("member_id")
        if not member_id:
            abort(400, description="'member_id' is required inside member_settings data.")
        cache.put_member_settings(member_id, data)
        return jsonify({"cached": "member_settings", "member_id": member_id}), 200

    # ------------------------------------------------------------------
    # 개인 여정 캐시
    # ------------------------------------------------------------------

    if event_type == "journey":
        if not isinstance(data, dict):
            abort(400, description="'data' must be an object for type='journey'.")
        cache.put_journey(data)
        return jsonify({"cached": "journey", "id": data.get("journey_id")}), 200

    if event_type == "member_journeys":
        member_id = body.get("member_id")
        if not member_id:
            abort(400, description="'member_id' is required for type='member_journeys'.")
        if not isinstance(data, list):
            abort(400, description="'data' must be an array for type='member_journeys'.")
        cache.put_member_journeys(member_id, data)
        return jsonify({"cached": "member_journeys", "member_id": member_id, "count": len(data)}), 200

    abort(400, description=f"Unknown type '{event_type}'. Must be one of: appointment | participant | participants | member_settings | journey | member_journeys | geofence_exit.")
