"""
POST /api/appointment/register  — 약속 등록 및 추적 시작 시간 반환
POST /api/appointment/departure — 현재 위치 기반 출발 시간 계산
"""

from datetime import datetime
from flask import Blueprint, request, jsonify, abort

from gps_api.core.appointment import compute_departure, get_tracking_info

bp = Blueprint("appointment", __name__)


def _parse_loc(value, name: str) -> tuple[float, float]:
    try:
        return float(value[0]), float(value[1])
    except (TypeError, IndexError, ValueError):
        abort(400, description=f"{name} must be in [lat, lon] format.")


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        abort(400, description="appointment_time must be ISO 8601 format (e.g. 2026-05-14T15:00:00).")


@bp.post("/register")
def register():
    """
    약속을 등록하고 GPS 추적 시작 시간을 반환합니다.
    약속 시간 4시간 전부터 추적이 활성화됩니다.

    Request JSON:
      {
        "destination": [37.5088, 127.0632],
        "appointment_time": "2026-05-14T15:00:00"
      }

    Response JSON:
      {
        "destination": [37.5088, 127.0632],
        "appointment_time": "2026-05-14T15:00:00",
        "tracking_start_time": "2026-05-14T11:00:00",
        "tracking_active": false,
        "seconds_until_tracking": 7200
      }
    """
    body = request.get_json(silent=True) or {}

    if "destination" not in body or "appointment_time" not in body:
        abort(400, description="destination and appointment_time fields are required.")

    dest = _parse_loc(body["destination"], "destination")
    appt_time = _parse_datetime(body["appointment_time"])

    info = get_tracking_info(appt_time)

    return jsonify({
        "destination": list(dest),
        "appointment_time": appt_time.isoformat(),
        **info,
    })


@bp.post("/departure")
def departure():
    """
    현재 위치를 기반으로 출발 시간과 현재 상태를 반환합니다.

    Request JSON:
      {
        "current_loc": [37.4979, 127.0276],
        "destination": [37.5088, 127.0632],
        "appointment_time": "2026-05-14T15:00:00",
        "speed_mps": 1.4,       // optional (default: 1.4 m/s 도보)
        "buffer_min": 10        // optional (default: 10분 여유)
      }

    Response JSON:
      {
        "departure_time": "2026-05-14T14:33:00",
        "time_until_departure_sec": 1980,
        "eta_sec": 1320,
        "eta_min": 22.0,
        "distance_m": 1848.0,
        "status": "on_time",        // on_time | leave_soon | hurry | critical | late
        "tracking_active": true,
        "tracking_start_time": "2026-05-14T11:00:00"
      }
    """
    body = request.get_json(silent=True) or {}

    for field in ("current_loc", "destination", "appointment_time"):
        if field not in body:
            abort(400, description=f"{field} field is required.")

    curr = _parse_loc(body["current_loc"], "current_loc")
    dest = _parse_loc(body["destination"], "destination")
    appt_time = _parse_datetime(body["appointment_time"])

    speed = float(body.get("speed_mps", 1.4))
    buffer = float(body.get("buffer_min", 10))

    if speed <= 0:
        abort(400, description="speed_mps must be a positive number.")

    result = compute_departure(curr, dest, appt_time, speed, buffer)
    return jsonify(result)
