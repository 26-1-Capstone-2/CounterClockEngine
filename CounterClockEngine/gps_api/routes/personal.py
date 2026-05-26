"""
POST /api/personal/departure
  귀가/개인 일정 기반 출발 알람 시간 계산
  - DRIVING  → 카카오 모빌리티 길찾기 API
  - TRANSIT  → ODsay 대중교통 API
"""

import os
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, abort, current_app

from gps_api.core.kakao_route import fetch_route as kakao_fetch_route
from gps_api.core.transit_route import fetch_transit_route

bp = Blueprint("personal", __name__)


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        abort(400, description="target_time must be ISO 8601 format (e.g. 2026-05-25T18:00:00).")


@bp.post("/departure")
def departure():
    """
    목표 도착 시간 기준으로 출발 알람 시간과 예상 도착 시간을 반환합니다.

    Request JSON:
      {
        "current_lat": 37.49796,
        "current_lng": 127.02759,
        "dest_lat": 37.51234,
        "dest_lng": 127.05678,
        "transport_type": "TRANSIT",   // TRANSIT | DRIVING
        "target_time": "2026-05-25T18:00:00",
        "is_last_mode": false,
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

    # 실제 출발 시각 = 목표 도착 - 이동 시간
    departure_time      = target_time - timedelta(seconds=duration_sec)
    # 알람 시각 = 출발 시각 - 준비 시간
    departure_alarm_time = departure_time - timedelta(minutes=preparation_time)
    # 예상 도착 = 출발 시각 + 이동 시간 (= target_time)
    estimated_arrival   = departure_time + timedelta(seconds=duration_sec)

    return jsonify({
        "departure_alarm_time": departure_alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "estimated_arrival":    estimated_arrival.strftime("%Y-%m-%dT%H:%M:%S"),
    })


def _get_duration(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    transport_type: str,
    config,
) -> int:
    """경로 API를 호출해 소요 시간(초)을 반환."""
    if transport_type == "DRIVING":
        kakao_key = config.get("KAKAO_API_KEY", "")
        if not kakao_key:
            raise ValueError("KAKAO_API_KEY가 설정되지 않았습니다.")
        _, duration_sec, _ = kakao_fetch_route(
            origin_lat, origin_lng, dest_lat, dest_lng, kakao_key
        )
        return duration_sec

    # TRANSIT
    odsay_key = config.get("ODSAY_API_KEY", "")
    if not odsay_key:
        raise ValueError("ODSAY_API_KEY가 설정되지 않았습니다.")
    _, duration_sec, _, _ = fetch_transit_route(
        origin_lat, origin_lng, dest_lat, dest_lng, odsay_key
    )
    return duration_sec
