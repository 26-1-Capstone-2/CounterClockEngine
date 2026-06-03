"""
POST /internal/alarm/journey      — Personal journey departure alarm (for internal Spring calls)
POST /internal/alarm/appointment  — Group appointment departure alarm (for internal Spring calls)

Common calculations:
  departure_time       = target_time - duration_sec
  latency_buffer       = recommended_buffer(member_id)  # lateness pattern based, 10 min for cold-start
  departure_alarm_time = departure_time - preparation_time - latency_buffer
  estimated_arrival    = departure_time + duration_sec

interval (seconds): Adaptive GPS polling interval via Cosine Blend + Sigmoid Activity Recognition
  urgency = blend(distance_urgency, time_urgency)  →  interval in [3, 300] seconds
"""

from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, abort, current_app

from gps_api.routes.personal import _get_duration, _parse_datetime
from gps_api.core.latency import recommended_buffer
from gps_api.core.optimizer import (
    calculate_next_interval, LocationPoint, Geofence,
    classify_gps_mode, INTERVAL_MIN_S, INTERVAL_MAX_S,
)
from gps_api.core.transit_route import (
    fetch_transit_route,
    find_last_train_departure,
    first_walk_min,
    total_walk_min,
    boarding_station,
    walk_fallback,
    SEARCH_TYPE_MAP,
    PRIORITY_OPT_MAP,
)

bp = Blueprint("alarm", __name__)

_DEFAULT_BUFFER_MIN = 10.0


_TRANSIT_MODES = {"SUBWAY", "BUS", "ALL"}
_VALID_MODES   = {"DRIVING"} | _TRANSIT_MODES
_VALID_PRIORITIES = {"MIN_TIME", "MIN_TRANSFER", "MIN_WALK"}

# ODSAY search_type values for fallback logic
_SEARCH_TYPE_ALL = 0
_FALLBACK_SEARCH_TYPES = {1, 2}  # SUBWAY=1, BUS=2


def _fetch_transit_with_fallback(
    origin_lat, origin_lon, dest_lat, dest_lon, odsay_key,
    search_type: int, opt: int,
    search_dt=None,
):
    """Call fetch_transit_route; if BUS/SUBWAY returns no route, retry with ALL (search_type=0)."""
    try:
        return fetch_transit_route(
            origin_lat, origin_lon, dest_lat, dest_lon, odsay_key,
            search_dt=search_dt, search_type=search_type, opt=opt,
        )
    except ValueError:
        if search_type in _FALLBACK_SEARCH_TYPES:
            return fetch_transit_route(
                origin_lat, origin_lon, dest_lat, dest_lon, odsay_key,
                search_dt=search_dt, search_type=_SEARCH_TYPE_ALL, opt=opt,
            )
        raise


def _find_last_train_with_fallback(
    origin_lat, origin_lon, dest_lat, dest_lon, odsay_key,
    target_time, search_type: int, opt: int,
):
    """Call find_last_train_departure; if BUS/SUBWAY returns None, retry with ALL (search_type=0)."""
    result = find_last_train_departure(
        origin_lat, origin_lon, dest_lat, dest_lon,
        odsay_key, target_time,
        search_type=search_type, opt=opt,
    )
    if result is None and search_type in _FALLBACK_SEARCH_TYPES:
        result = find_last_train_departure(
            origin_lat, origin_lon, dest_lat, dest_lon,
            odsay_key, target_time,
            search_type=_SEARCH_TYPE_ALL, opt=opt,
        )
    return result


def _transit_duration(
    origin_lat, origin_lon, dest_lat, dest_lon,
    search_type: int, opt: int, config,
) -> tuple[int, int, int, str | None]:
    """
    Resolve transit travel time, falling back to a walking estimate when the
    trip is too short for ODSAY (under ~700 m, where it returns no route).

    Returns (duration_sec, walk_to_station_min, total_walk_min, boarding_station_name).
    """
    is_short, walk_est_min = walk_fallback(origin_lat, origin_lon, dest_lat, dest_lon)
    if not is_short:
        odsay_key = config.get("ODSAY_API_KEY", "")
        try:
            _, duration_sec, _, legs = _fetch_transit_with_fallback(
                origin_lat, origin_lon, dest_lat, dest_lon, odsay_key,
                search_type=search_type, opt=opt,
            )
            return duration_sec, first_walk_min(legs), total_walk_min(legs), boarding_station(legs)
        except ValueError:
            # Near the 700 m boundary ODSAY can still return no route — walk instead.
            pass
    return walk_est_min * 60, walk_est_min, walk_est_min, None


def _compute_alarm(body: dict) -> dict:
    """
    Common alarm calculation logic.
    If member_id is provided, applies an additional latency_buffer based on lateness patterns.
    """
    required = ("current_lat", "current_lng", "dest_lat", "dest_lng", "transport_mode")
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
    preparation_time = float(body.get("preparation_time", 0))
    member_id        = body.get("member_id")
    is_last_mode     = bool(body.get("is_last_mode", False))

    target_time_raw = body.get("target_time")
    if target_time_raw is not None:
        target_time = _parse_datetime(target_time_raw)
    elif not is_last_mode:
        abort(400, description="target_time field is required when is_last_mode is false.")
    else:
        target_time = None  # is_last_mode=True: last train time will be computed below

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
        # When target_time is null, use today as the date reference for last-train search.
        search_ref = target_time if target_time is not None else datetime.now()

        # Too close for transit: walking has no schedule, so the latest
        # departure is simply search_ref minus the walking duration.
        is_short, walk_est_min = walk_fallback(current_lat, current_lng, dest_lat, dest_lng)
        if is_short:
            last_departure_dt    = search_ref - timedelta(minutes=walk_est_min)
            estimated_arrival    = last_departure_dt + timedelta(minutes=walk_est_min)
            departure_alarm_time = last_departure_dt - timedelta(minutes=total_buffer_min)
            interval, gps_mode   = _adaptive_gps_interval(
                current_lat, current_lng, dest_lat, dest_lng,
                walk_est_min * 60, search_ref,
                member_id=member_id,
            )
            return {
                "target_time":          last_departure_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "departure_alarm_time": departure_alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
                "estimated_arrival":    estimated_arrival.strftime("%Y-%m-%dT%H:%M:%S"),
                "latency_buffer_min":   round(latency_buffer_min, 1),
                "boarding_time":        last_departure_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "interval":             interval,
                "gps_mode":             gps_mode,
                "walk_to_station_min":  walk_est_min,
                "total_walk_time":      walk_est_min,
                "which_station":        None,
            }

        odsay_key = current_app.config.get("ODSAY_API_KEY", "")
        result = _find_last_train_with_fallback(
            current_lat, current_lng, dest_lat, dest_lng,
            odsay_key, search_ref,
            search_type=search_type, opt=opt,
        )
        if result is None:
            abort(404, description="No valid last-train route found for the given date. The last train may have already departed.")

        last_departure_dt, last_duration_sec, walk_min, walk_total_min, last_station = result
        last_arrival_dt      = last_departure_dt + timedelta(seconds=last_duration_sec)
        departure_alarm_time = last_departure_dt - timedelta(minutes=total_buffer_min)
        interval, gps_mode   = _adaptive_gps_interval(
            current_lat, current_lng, dest_lat, dest_lng,
            last_duration_sec, last_arrival_dt,
            member_id=member_id,
        )

        return {
            "target_time":           last_departure_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "departure_alarm_time":  departure_alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "estimated_arrival":     last_arrival_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "latency_buffer_min":    round(latency_buffer_min, 1),
            "boarding_time":         last_departure_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "interval":              interval,
            "gps_mode":              gps_mode,
            "walk_to_station_min":   walk_min,
            "total_walk_time":       walk_total_min,
            "which_station":         last_station,
        }

    # Normal mode
    which_station = None
    try:
        if is_transit:
            duration_sec, walk_min, walk_total, which_station = _transit_duration(
                current_lat, current_lng, dest_lat, dest_lng,
                search_type, opt, current_app.config,
            )
        else:
            duration_sec = _get_duration(
                current_lat, current_lng, dest_lat, dest_lng,
                transport_mode, current_app.config,
            )
            walk_min = walk_total = 0
    except ValueError as e:
        abort(502, description=str(e))
    except Exception as e:
        abort(502, description=f"Route API call failed: {e}")

    departure_time       = target_time - timedelta(seconds=duration_sec)
    departure_alarm_time = departure_time - timedelta(minutes=total_buffer_min)
    estimated_arrival    = departure_time + timedelta(seconds=duration_sec)
    # Time the user actually boards transit: leave origin, then walk to the station.
    boarding_dt          = departure_time + timedelta(minutes=walk_min) if is_transit else None
    interval, gps_mode   = _adaptive_gps_interval(
        current_lat, current_lng, dest_lat, dest_lng,
        duration_sec, target_time,
        member_id=member_id,
    )

    response = {
        "target_time":          target_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "departure_alarm_time": departure_alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "estimated_arrival":    estimated_arrival.strftime("%Y-%m-%dT%H:%M:%S"),
        "latency_buffer_min":   round(latency_buffer_min, 1),
        "boarding_time":        boarding_dt.strftime("%Y-%m-%dT%H:%M:%S") if boarding_dt else None,
        "interval":             interval,
        "gps_mode":             gps_mode,
        "which_station":        which_station,
    }
    if is_transit:
        response["walk_to_station_min"] = walk_min
    response["total_walk_time"] = walk_total
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
        "target_time": "2026-05-25T18:00:00",  // optional when is_last_mode=true (null → Flask computes from last train)
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
        "boarding_time": "2026-05-25T17:23:00",  // time the user boards transit (null for DRIVING)
        "interval": 30,                    // front-end GPS API polling interval (seconds)
        "walk_to_station_min": 3,          // included only for transit modes
        "total_walk_time": 8               // total walking minutes across the whole trip (0 for DRIVING)
      }
    """
    return jsonify(_compute_alarm(request.get_json(silent=True) or {}))


# member_id → last LocationPoint (in-memory, process-scoped)
_location_history: dict[str, LocationPoint] = {}


def _adaptive_gps_interval(
    current_lat: float,
    current_lng: float,
    dest_lat: float,
    dest_lng: float,
    duration_sec: float,
    target_time: datetime,
    member_id: str | None = None,
) -> tuple[int, str]:
    """Returns (interval_sec, gps_mode) using Cosine Blend + Sigmoid activity recognition.

    When member_id is provided, the previous location is recalled from an in-memory
    cache so that real movement speed can be fed into the sigmoid multiplier.
    """
    current_point = LocationPoint(lat=current_lat, lon=current_lng)

    if member_id:
        prev = _location_history.get(member_id)
        history = [prev, current_point] if prev is not None else [current_point]
        _location_history[member_id] = current_point
    else:
        history = [current_point]

    geofences = [Geofence(id="destination", lat=dest_lat, lon=dest_lng, radius=100.0)]
    appointment_remaining_sec = max((target_time - datetime.now()).total_seconds(), 0.0)
    result = calculate_next_interval(
        user_lat=current_lat,
        user_lon=current_lng,
        history=history,
        geofences=geofences,
        eta_sec=duration_sec,
        appointment_remaining_sec=appointment_remaining_sec,
    )

    if len(history) < 2:
        # Pre-departure: no real speed data, so act_mult (≈3.0) and slc_mult (2.0)
        # would incorrectly inflate the interval. Use only the cosine urgency directly.
        import math
        raw = INTERVAL_MIN_S + 0.5 * (INTERVAL_MAX_S - INTERVAL_MIN_S) * (1.0 + math.cos(math.pi * result.urgency))
        interval = max(int(min(round(raw), INTERVAL_MAX_S)), 1)
        return interval, classify_gps_mode(interval)

    return result.next_interval, result.gps_mode


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

    which_station = None
    try:
        if is_transit:
            duration_sec, walk_min, walk_total, which_station = _transit_duration(
                current_lat, current_lng, dest_lat, dest_lng,
                search_type, opt, current_app.config,
            )
        else:
            duration_sec = _get_duration(
                current_lat, current_lng, dest_lat, dest_lng,
                transport_mode, current_app.config,
            )
            walk_min = walk_total = 0
    except ValueError as e:
        abort(502, description=str(e))
    except Exception as e:
        abort(502, description=f"Route API call failed: {e}")

    departure_time       = target_time - timedelta(seconds=duration_sec)
    departure_alarm_time = departure_time - timedelta(minutes=total_buffer_min)
    estimated_arrival    = departure_time + timedelta(seconds=duration_sec)
    # Time the user actually boards transit: leave origin, then walk to the station.
    boarding_dt          = departure_time + timedelta(minutes=walk_min) if is_transit else None
    interval, gps_mode   = _adaptive_gps_interval(
        current_lat, current_lng, dest_lat, dest_lng,
        duration_sec, target_time,
        member_id=member_id,
    )

    response = {
        "departure_alarm_time": departure_alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "estimated_arrival":    estimated_arrival.strftime("%Y-%m-%dT%H:%M:%S"),
        "boarding_time":        boarding_dt.strftime("%Y-%m-%dT%H:%M:%S") if boarding_dt else None,
        "interval":             interval,
        "gps_mode":             gps_mode,
        "which_station":        which_station,
    }
    if is_transit:
        response["walk_to_station_min"] = walk_min
    response["total_walk_time"] = walk_total
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
        "boarding_time": "2026-05-25T17:33:00",  // time the user boards transit (null for DRIVING)
        "interval": 30,                    // front-end GPS API polling interval (seconds)
        "which_station": "강남역",         // boarding station/stop name w/ 역·정류장 suffix (null for DRIVING / walk fallback)
        "walk_to_station_min": 3,          // included only for transit modes
        "total_walk_time": 8               // total walking minutes across the whole trip (0 for DRIVING)
      }
    """
    return jsonify(_compute_appointment_alarm(request.get_json(silent=True) or {}))
