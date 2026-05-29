"""
POST /internal/alarm/journey      — Personal journey departure alarm (for internal Spring calls)
POST /internal/alarm/appointment  — Group appointment departure alarm (for internal Spring calls)

Common calculations:
  departure_time       = target_time - duration_sec
  latency_buffer       = recommended_buffer(member_id)  # lateness pattern based, 10 min for cold-start
  departure_alarm_time = departure_time - preparation_time - latency_buffer
  estimated_arrival    = departure_time + duration_sec

interval (seconds): GPS polling interval based on time remaining until departure alarm
  > 60 min  → 60 sec
  30~60 min → 30 sec
  < 30 min  → 15 sec
"""

from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, abort, current_app

from gps_api.routes.personal import _get_duration, _parse_datetime
from gps_api.core.latency import recommended_buffer
from gps_api.core.transit_route import (
    fetch_transit_route,
    find_last_train_departure,
    first_walk_min,
    SEARCH_TYPE_MAP,
    PRIORITY_OPT_MAP,
)

bp = Blueprint("alarm", __name__)

_DEFAULT_BUFFER_MIN = 10.0


_TRANSIT_MODES = {"SUBWAY", "BUS", "ALL"}
_VALID_MODES   = {"DRIVING"} | _TRANSIT_MODES
_VALID_PRIORITIES = {"MIN_TIME", "MIN_TRANSFER", "MIN_WALK"}


def _compute_alarm(body: dict) -> dict:
    """
    Common alarm calculation logic.
    If member_id is provided, applies an additional latency_buffer based on lateness patterns.
    """
    required = ("current_lat", "current_lng", "dest_lat", "dest_lng",
                "transport_mode", "target_time")
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

    transport_mode = str(body["transport_mode"]).upper()
    priority_type  = str(body.get("priority_type", "MIN_TIME")).upper()
    target_time    = _parse_datetime(body["target_time"])
    preparation_time = float(body.get("preparation_time", 0))
    member_id      = body.get("member_id")
    is_last_mode   = bool(body.get("is_last_mode", False))

    if transport_mode not in _VALID_MODES:
        abort(400, description=f"transport_mode must be one of: {', '.join(sorted(_VALID_MODES))}.")

    if priority_type not in _VALID_PRIORITIES:
        abort(400, description=f"priority_type must be one of: {', '.join(sorted(_VALID_PRIORITIES))}.")

    if is_last_mode and transport_mode == "DRIVING":
        abort(400, description="is_last_mode can only be used with transport_mode other than DRIVING.")

    is_transit = transport_mode in _TRANSIT_MODES
    search_type = SEARCH_TYPE_MAP.get(transport_mode, 0)
    opt         = PRIORITY_OPT_MAP.get(priority_type, 0)

    # Lateness pattern buffer
    latency_buffer_min = recommended_buffer(member_id) if member_id else _DEFAULT_BUFFER_MIN
    total_buffer_min   = preparation_time + latency_buffer_min

    if is_last_mode:
        odsay_key = current_app.config.get("ODSAY_API_KEY", "")
        result = find_last_train_departure(
            current_lat, current_lng, dest_lat, dest_lng,
            odsay_key, target_time,
            search_type=search_type, opt=opt,
        )
        if result is None:
            abort(404, description="No valid last-train route found for the given date. The last train may have already departed.")

        last_departure_dt, last_duration_sec, walk_min = result
        last_arrival_dt = last_departure_dt + timedelta(seconds=last_duration_sec)

        # Calculate normal departure time based on target_time (for comparison with last train)
        try:
            odsay_key = current_app.config.get("ODSAY_API_KEY", "")
            _, normal_duration_sec, _, _ = fetch_transit_route(
                current_lat, current_lng, dest_lat, dest_lng, odsay_key,
                search_type=search_type, opt=opt,
            )
        except Exception:
            normal_duration_sec = last_duration_sec
        normal_departure_dt = target_time - timedelta(seconds=normal_duration_sec)

        # Set alarm based on whichever departure time is earlier
        if last_departure_dt <= normal_departure_dt:
            effective_departure = last_departure_dt
            effective_arrival   = last_arrival_dt
        else:
            effective_departure = normal_departure_dt
            effective_arrival   = target_time

        departure_alarm_time = effective_departure - timedelta(minutes=total_buffer_min)

        return {
            "departure_alarm_time":  departure_alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "estimated_arrival":     effective_arrival.strftime("%Y-%m-%dT%H:%M:%S"),
            "latency_buffer_min":    round(latency_buffer_min, 1),
            "last_train_departure":  last_departure_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "walk_to_station_min":   walk_min,
        }

    # Normal mode
    try:
        if is_transit:
            odsay_key = current_app.config.get("ODSAY_API_KEY", "")
            _, duration_sec, _, legs = fetch_transit_route(
                current_lat, current_lng, dest_lat, dest_lng, odsay_key,
                search_type=search_type, opt=opt,
            )
            walk_min = first_walk_min(legs)
        else:
            duration_sec = _get_duration(
                current_lat, current_lng, dest_lat, dest_lng,
                transport_mode, current_app.config,
            )
            walk_min = 0
    except ValueError as e:
        abort(502, description=str(e))
    except Exception as e:
        abort(502, description=f"Route API call failed: {e}")

    departure_time       = target_time - timedelta(seconds=duration_sec)
    departure_alarm_time = departure_time - timedelta(minutes=total_buffer_min)
    estimated_arrival    = departure_time + timedelta(seconds=duration_sec)

    response = {
        "departure_alarm_time": departure_alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "estimated_arrival":    estimated_arrival.strftime("%Y-%m-%dT%H:%M:%S"),
        "latency_buffer_min":   round(latency_buffer_min, 1),
    }
    if is_transit:
        response["walk_to_station_min"] = walk_min
    return response


@bp.post("/journey")
def journey_alarm():
    """
    Returns the departure alarm time and estimated arrival time based on the personal journey (commute home) target arrival time.

    Request JSON:
      {
        "current_lat": 37.49796,
        "current_lng": 127.02759,
        "dest_lat": 37.51234,
        "dest_lng": 127.05678,
        "transport_mode": "ALL",           // DRIVING | SUBWAY | BUS | ALL
        "priority_type": "MIN_TIME",       // MIN_TIME | MIN_TRANSFER | MIN_WALK
        "target_time": "2026-05-25T18:00:00",
        "is_last_mode": false,
        "preparation_time": 10,            // in minutes
        "member_id": 1234,                 // optional — used for lateness pattern personalization
        "journey_id": 5678                 // optional — for identification
      }

    Response JSON:
      {
        "departure_alarm_time": "2026-05-25T17:20:00",
        "estimated_arrival": "2026-05-25T18:00:00",
        "latency_buffer_min": 5.2,         // actual applied lateness pattern buffer (minutes)
        "walk_to_station_min": 3           // included only for transit modes
      }
    """
    return jsonify(_compute_alarm(request.get_json(silent=True) or {}))


def _gps_interval(alarm_dt: datetime) -> int:
    """Returns the GPS polling interval (seconds) based on remaining time until the departure alarm."""
    remaining_min = (alarm_dt - datetime.now()).total_seconds() / 60
    if remaining_min > 60:
        return 60
    if remaining_min > 30:
        return 30
    return 15


def _compute_appointment_alarm(body: dict) -> dict:
    """
    Appointment alarm calculation logic (no is_last_mode).
    If member_id is provided, applies an additional latency_buffer based on lateness patterns.
    """
    required = ("current_lat", "current_lng", "dest_lat", "dest_lng",
                "transport_mode", "target_time")
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

    transport_mode   = str(body["transport_mode"]).upper()
    priority_type    = str(body.get("priority_type", "MIN_TIME")).upper()
    target_time      = _parse_datetime(body["target_time"])
    preparation_time = float(body.get("preparation_time", 0))
    member_id        = body.get("member_id")

    if transport_mode not in _VALID_MODES:
        abort(400, description=f"transport_mode must be one of: {', '.join(sorted(_VALID_MODES))}.")

    if priority_type not in _VALID_PRIORITIES:
        abort(400, description=f"priority_type must be one of: {', '.join(sorted(_VALID_PRIORITIES))}.")

    is_transit  = transport_mode in _TRANSIT_MODES
    search_type = SEARCH_TYPE_MAP.get(transport_mode, 0)
    opt         = PRIORITY_OPT_MAP.get(priority_type, 0)

    latency_buffer_min = recommended_buffer(member_id) if member_id else _DEFAULT_BUFFER_MIN
    total_buffer_min   = preparation_time + latency_buffer_min

    try:
        if is_transit:
            odsay_key = current_app.config.get("ODSAY_API_KEY", "")
            _, duration_sec, _, legs = fetch_transit_route(
                current_lat, current_lng, dest_lat, dest_lng, odsay_key,
                search_type=search_type, opt=opt,
            )
            walk_min = first_walk_min(legs)
        else:
            duration_sec = _get_duration(
                current_lat, current_lng, dest_lat, dest_lng,
                transport_mode, current_app.config,
            )
            walk_min = 0
    except ValueError as e:
        abort(502, description=str(e))
    except Exception as e:
        abort(502, description=f"Route API call failed: {e}")

    departure_time       = target_time - timedelta(seconds=duration_sec)
    departure_alarm_time = departure_time - timedelta(minutes=total_buffer_min)
    estimated_arrival    = departure_time + timedelta(seconds=duration_sec)

    response = {
        "departure_alarm_time": departure_alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "estimated_arrival":    estimated_arrival.strftime("%Y-%m-%dT%H:%M:%S"),
        "interval":             _gps_interval(departure_alarm_time),
    }
    if is_transit:
        response["walk_to_station_min"] = walk_min
    return response


@bp.post("/appointment")
def appointment_alarm():
    """
    Returns the departure alarm time and estimated arrival time based on the group appointment target arrival time.

    Request JSON:
      {
        "current_lat": 37.49796,
        "current_lng": 127.02759,
        "dest_lat": 37.51234,
        "dest_lng": 127.05678,
        "transport_mode": "DRIVING",       // DRIVING | SUBWAY | BUS | ALL
        "priority_type": "MIN_TIME",       // MIN_TIME | MIN_TRANSFER | MIN_WALK
        "target_time": "2026-05-25T18:00:00",
        "preparation_time": 10,            // in minutes
        "member_id": 1234,                 // optional — used for lateness pattern personalization
        "participant_id": 5678             // optional — for identification
      }

    Response JSON:
      {
        "departure_alarm_time": "2026-05-25T17:30:00",
        "estimated_arrival": "2026-05-25T17:55:00",
        "interval": 30                     // front-end GPS API polling interval (seconds)
      }
    """
    return jsonify(_compute_appointment_alarm(request.get_json(silent=True) or {}))
