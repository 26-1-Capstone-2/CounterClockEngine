"""
POST /internal/alarm/appointment
  그룹 약속 기반 출발 알람 시간 계산 (스프링 내부 호출용)
  - personal/departure 와 동일 로직, is_last_mode 없음
"""

from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, abort, current_app

from gps_api.routes.personal import _get_duration, _parse_datetime

bp = Blueprint("alarm", __name__)


@bp.post("/appointment")
def appointment_alarm():
    """
    약속 목표 도착 시간 기준으로 출발 알람 시간과 예상 도착 시간을 반환합니다.

    Request JSON:
      {
        "current_lat": 37.49796,
        "current_lng": 127.02759,
        "dest_lat": 37.51234,
        "dest_lng": 127.05678,
        "transport_type": "DRIVING",   // TRANSIT | DRIVING
        "target_time": "2026-05-25T18:00:00",
        "preparation_time": 10         // 분 단위
      }

    Response JSON:
      {
        "departure_alarm_time": "2026-05-25T17:30:00",
        "estimated_arrival": "2026-05-25T18:00:00"
      }
    """
    body = request.get_json(silent=True) or {}

    required = ("current_lat", "current_lng", "dest_lat", "dest_lng",
                "transport_type", "target_time")
    for field in required:
        if field not in body:
            abort(400, description=f"{field} field is required.")

    try:
        current_lat = float(body["current_lat"])
        current_lng = float(body["current_lng"])
        dest_lat    = float(body["dest_lat"])
        dest_lng    = float(body["dest_lng"])
    except (TypeError, ValueError):
        abort(400, description="lat/lng values must be numeric.")

    transport_type   = str(body["transport_type"]).upper()
    target_time      = _parse_datetime(body["target_time"])
    preparation_time = int(body.get("preparation_time", 0))

    if transport_type not in ("DRIVING", "TRANSIT"):
        abort(400, description="transport_type must be DRIVING or TRANSIT.")

    try:
        duration_sec = _get_duration(
            current_lat, current_lng, dest_lat, dest_lng,
            transport_type, current_app.config,
        )
    except ValueError as e:
        abort(502, description=str(e))
    except Exception as e:
        abort(502, description=f"경로 API 호출 실패: {e}")

    departure_time       = target_time - timedelta(seconds=duration_sec)
    departure_alarm_time = departure_time - timedelta(minutes=preparation_time)
    estimated_arrival    = departure_time + timedelta(seconds=duration_sec)

    return jsonify({
        "departure_alarm_time": departure_alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "estimated_arrival":    estimated_arrival.strftime("%Y-%m-%dT%H:%M:%S"),
    })
