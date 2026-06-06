"""
POST /api/personal/departure
  Calculate departure alarm time based on commute/personal schedule
  - DRIVING  → Kakao Mobility Directions API
  - TRANSIT  → ODsay Public Transit API
"""

import os
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, abort, current_app

from gps_api.core.kakao_route import fetch_route as kakao_fetch_route
from gps_api.core.transit_route import fetch_transit_route
from gps_api.core.timeutil import now_kst as _now_kst

bp = Blueprint("personal", __name__)


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        abort(400, description="target_time must be ISO 8601 format (e.g. 2026-05-25T18:00:00).")


@bp.post("/departure")
def departure():
    """
    Returns the departure alarm time and estimated arrival time based on the target arrival time.

    Request JSON:
      {
        "current_lat": 37.49796,
        "current_lng": 127.02759,
        "dest_lat": 37.51234,
        "dest_lng": 127.05678,
        "transport_type": "TRANSIT",   // TRANSIT | DRIVING
        "target_time": "2026-05-25T18:00:00",
        "is_last_mode": false,
        "preparation_time": 10         // in minutes
      }

    Response JSON:
      {
        "departure_alarm_time": "2026-05-25T17:30:00",
        "estimated_arrival": "2026-05-25T17:42:00"   // live ETA: now + travel time from the real-time current position (updates each poll)
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
        abort(502, description=f"Route API call failed: {e}")

    # Actual departure time = target arrival - travel duration
    departure_time      = target_time - timedelta(seconds=duration_sec)
    # Alarm time = departure time - preparation time
    departure_alarm_time = departure_time - timedelta(minutes=preparation_time)
    # Live ETA: from the real-time current position, arrival = now + travel time.
    # Updates on every poll as the user moves (may differ from target_time).
    estimated_arrival   = _now_kst() + timedelta(seconds=duration_sec)

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
    """Calls the route API and returns the travel duration in seconds."""
    if transport_type == "DRIVING":
        kakao_key = config.get("KAKAO_API_KEY", "")
        if not kakao_key:
            raise ValueError("KAKAO_API_KEY is not configured.")
        _, duration_sec, _ = kakao_fetch_route(
            origin_lat, origin_lng, dest_lat, dest_lng, kakao_key
        )
        return duration_sec

    # TRANSIT
    odsay_key = config.get("ODSAY_API_KEY", "")
    if not odsay_key:
        raise ValueError("ODSAY_API_KEY is not configured.")
    _, duration_sec, _, _ = fetch_transit_route(
        origin_lat, origin_lng, dest_lat, dest_lng, odsay_key
    )
    return duration_sec
