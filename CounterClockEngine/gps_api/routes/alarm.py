"""
POST /internal/alarm/journey      — 개인 여정 출발 알람 (스프링 내부 호출용)
POST /internal/alarm/appointment  — 그룹 약속 출발 알람 (스프링 내부 호출용)

공통 계산:
  departure_time       = target_time - duration_sec
  latency_buffer       = recommended_buffer(member_id)  # 지각 패턴 기반, cold-start 시 10분
  departure_alarm_time = departure_time - preparation_time - latency_buffer
  estimated_arrival    = departure_time + duration_sec
"""

from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, abort, current_app

from gps_api.routes.personal import _get_duration, _parse_datetime
from gps_api.core.latency import recommended_buffer

bp = Blueprint("alarm", __name__)

_DEFAULT_BUFFER_MIN = 10.0


def _compute_alarm(body: dict, require_is_last_mode: bool = False) -> dict:
    """
    공통 알람 계산 로직.
    member_id 가 있으면 지각 패턴 기반 latency_buffer 를 추가로 적용한다.
    """
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
    preparation_time = float(body.get("preparation_time", 0))
    member_id        = body.get("member_id")

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

    # 지각 패턴 버퍼: member_id 있으면 개인화, 없으면 cold-start 기본값
    latency_buffer_min = recommended_buffer(member_id) if member_id else _DEFAULT_BUFFER_MIN
    total_buffer_min   = preparation_time + latency_buffer_min

    departure_time       = target_time - timedelta(seconds=duration_sec)
    departure_alarm_time = departure_time - timedelta(minutes=total_buffer_min)
    estimated_arrival    = departure_time + timedelta(seconds=duration_sec)

    return {
        "departure_alarm_time": departure_alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "estimated_arrival":    estimated_arrival.strftime("%Y-%m-%dT%H:%M:%S"),
        "latency_buffer_min":   round(latency_buffer_min, 1),
    }


@bp.post("/journey")
def journey_alarm():
    """
    개인 여정(귀가) 목표 도착 시간 기준으로 출발 알람 시간과 예상 도착 시간을 반환합니다.

    Request JSON:
      {
        "current_lat": 37.49796,
        "current_lng": 127.02759,
        "dest_lat": 37.51234,
        "dest_lng": 127.05678,
        "transport_type": "TRANSIT",   // TRANSIT | DRIVING
        "target_time": "2026-05-25T18:00:00",
        "is_last_mode": false,
        "preparation_time": 10,        // 분 단위
        "member_id": "user_001"        // optional — 지각 패턴 개인화에 사용
      }

    Response JSON:
      {
        "departure_alarm_time": "2026-05-25T17:20:00",
        "estimated_arrival": "2026-05-25T18:00:00",
        "latency_buffer_min": 5.2      // 실제 적용된 지각 패턴 버퍼 (분)
      }
    """
    return jsonify(_compute_alarm(request.get_json(silent=True) or {}))


@bp.post("/appointment")
def appointment_alarm():
    """
    그룹 약속 목표 도착 시간 기준으로 출발 알람 시간과 예상 도착 시간을 반환합니다.

    Request JSON:
      {
        "current_lat": 37.49796,
        "current_lng": 127.02759,
        "dest_lat": 37.51234,
        "dest_lng": 127.05678,
        "transport_type": "DRIVING",   // TRANSIT | DRIVING
        "target_time": "2026-05-25T18:00:00",
        "preparation_time": 10,        // 분 단위
        "member_id": "user_001"        // optional — 지각 패턴 개인화에 사용
      }

    Response JSON:
      {
        "departure_alarm_time": "2026-05-25T17:20:00",
        "estimated_arrival": "2026-05-25T18:00:00",
        "latency_buffer_min": 5.2      // 실제 적용된 지각 패턴 버퍼 (분)
      }
    """
    return jsonify(_compute_alarm(request.get_json(silent=True) or {}))
