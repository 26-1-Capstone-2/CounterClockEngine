"""
Personal journey tracking module.

Journey = single-person travel tracking (separate from group appointments)

DB schema mapping:
  Journey ↔ Journey (journey_id, member_id, travel_mode, origin, destination,
                      current_location, goal_time, ETA, departure_alarm, last_train_flag,
                      repeat_days, journey_status, alarm_switch)

Buffer priority:
  1st: DB member settings 'buffer time'
  2nd: Computed value based on past lateness records in latency.py
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from gps_api.core import cache as _cache
from gps_api.core.db_client import DBClient
from gps_api.core.kakao_route import fetch_route
from gps_api.core.latency import recommended_buffer
from gps_api.core.optimizer import haversine, decayed_eta

DEFAULT_SPEED_MPS = 1.4
ARRIVAL_RADIUS_M = 100.0

_db: Optional[DBClient] = None


def init_db(client: DBClient) -> None:
    global _db
    _db = client


# ------------------------------------------------------------------
# Data model (1:1 mapping with DB schema)
# ------------------------------------------------------------------

@dataclass
class Journey:
    journey_id: str
    member_id: str
    title: str
    journey_type: str = "one_way"   # Journey type

    # Origin
    origin_lat: Optional[float] = None
    origin_lon: Optional[float] = None
    origin_name: Optional[str] = None
    origin_address: Optional[str] = None

    # Destination
    dest_lat: Optional[float] = None
    dest_lon: Optional[float] = None
    dest_name: Optional[str] = None
    dest_address: Optional[str] = None

    # Current location (updated on each GPS update)
    current_lat: Optional[float] = None
    current_lon: Optional[float] = None

    # Settings
    travel_mode: str = "transit"
    goal_time: Optional[datetime] = None
    last_train: bool = False          # Last train flag
    repeat_days: Optional[str] = None  # Repeat days (e.g. "MON,WED,FRI")
    planned_date: Optional[str] = None
    alarm_enabled: bool = True

    # Computed results (updated on ETA recalculation → DB write-back)
    eta_sec: Optional[float] = None
    distance_m: Optional[float] = None
    alarm_time: Optional[str] = None   # Departure alarm time (ISO 8601)
    status: str = "unknown"
    last_updated: Optional[str] = None


# ------------------------------------------------------------------
# Buffer calculation (member settings take priority, latency records as fallback)
# ------------------------------------------------------------------

def _get_buffer_minutes(member_id: str) -> float:
    """
    1st: Cached member settings 'buffer time' (value pushed via DB Webhook)
    2nd: DB member settings direct query (fallback on cache miss)
    3rd: Computed value based on past lateness records (recommended_buffer)
    """
    settings = _cache.get_member_settings(member_id)
    if settings is None and _db:
        settings = _db.get_member_settings(member_id)
    if settings and settings.get("buffer_minutes") is not None:
        return float(settings["buffer_minutes"])
    return recommended_buffer(member_id)


# ------------------------------------------------------------------
# ETA + alarm calculation
# ------------------------------------------------------------------

def _compute_status(eta_sec: float, distance_m: float, goal_time: Optional[datetime]) -> str:
    if distance_m <= ARRIVAL_RADIUS_M:
        return "arrived"
    if not goal_time:
        return "tracking"
    now = datetime.now()
    time_until_goal = (goal_time - now).total_seconds()
    if time_until_goal < 0:
        return "late"
    margin = time_until_goal - eta_sec
    if margin >= 600:
        return "on_time"
    elif margin >= 0:
        return "leave_soon"
    else:
        return "hurry"


def compute_eta(journey: Journey, kakao_api_key: str = "") -> Journey:
    """
    Compute ETA from current location to destination and update the Journey object.
    Falls back to Haversine straight-line distance if Kakao API fails.
    """
    if journey.current_lat is None or journey.dest_lat is None:
        return journey

    try:
        if not kakao_api_key:
            raise ValueError("no key")
        _, duration_sec, distance_m = fetch_route(
            journey.current_lat, journey.current_lon,
            journey.dest_lat, journey.dest_lon,
            kakao_api_key,
        )
        eta_sec = float(duration_sec)
    except Exception:
        distance_m = haversine(
            journey.current_lat, journey.current_lon,
            journey.dest_lat, journey.dest_lon,
        )
        eta_sec = distance_m / DEFAULT_SPEED_MPS

    buffer_sec = _get_buffer_minutes(journey.member_id) * 60

    journey.eta_sec = round(eta_sec, 1)
    journey.distance_m = round(distance_m, 1)
    journey.status = _compute_status(eta_sec, distance_m, journey.goal_time)
    journey.last_updated = datetime.now().isoformat()

    if journey.goal_time:
        alarm_dt = journey.goal_time - timedelta(seconds=eta_sec + buffer_sec)
        journey.alarm_time = alarm_dt.isoformat()

    return journey


# ------------------------------------------------------------------
# DB integration
# ------------------------------------------------------------------

def get_journey(journey_id: str) -> Optional[Journey]:
    raw = _cache.get_journey(journey_id)
    if raw is None and _db:
        raw = _db.get_journey(journey_id)
    return _from_db(raw) if raw else None


def get_member_journeys(member_id: str) -> list[Journey]:
    cached = _cache.get_member_journeys(member_id)
    if cached is not None:
        return [_from_db(r) for r in cached]
    if not _db:
        return []
    return [_from_db(r) for r in _db.get_member_journeys(member_id)]


def update_location(
    journey_id: str,
    lat: float,
    lon: float,
    kakao_api_key: str = "",
) -> Optional[dict]:
    """
    Update location and recalculate ETA.
    When connected to DB, writes the result back.
    """
    raw = _cache.get_journey(journey_id)
    if raw is None and _db:
        raw = _db.get_journey(journey_id)
    if not raw:
        return None

    journey = _from_db(raw)
    journey.current_lat = lat
    journey.current_lon = lon

    compute_eta(journey, kakao_api_key)

    if _db:
        _db.update_journey(
            journey_id,
            current_lat=lat,
            current_lon=lon,
            eta=journey.eta_sec,
            alarm_time=journey.alarm_time,
            status=journey.status,
        )

    return to_dict(journey)


# ------------------------------------------------------------------
# DB response → internal model conversion
# ------------------------------------------------------------------

def _from_db(raw: dict) -> Journey:
    goal_time = None
    if raw.get("goal_time"):
        goal_time = datetime.fromisoformat(raw["goal_time"])

    return Journey(
        journey_id=raw["journey_id"],
        member_id=raw["member_id"],
        title=raw.get("title", ""),
        journey_type=raw.get("journey_type", "one_way"),
        origin_lat=raw.get("origin_lat"),
        origin_lon=raw.get("origin_lon"),
        origin_name=raw.get("origin_name"),
        origin_address=raw.get("origin_address"),
        dest_lat=raw.get("dest_lat"),
        dest_lon=raw.get("dest_lon"),
        dest_name=raw.get("dest_name"),
        dest_address=raw.get("dest_address"),
        current_lat=raw.get("current_lat"),
        current_lon=raw.get("current_lon"),
        travel_mode=raw.get("travel_mode", "transit"),
        goal_time=goal_time,
        last_train=raw.get("last_train", False),
        repeat_days=raw.get("repeat_days"),
        planned_date=raw.get("planned_date"),
        alarm_enabled=raw.get("alarm_enabled", True),
        eta_sec=raw.get("eta"),
        alarm_time=raw.get("alarm_time"),
        status=raw.get("status", "unknown"),
    )


# ------------------------------------------------------------------
# Serialization
# ------------------------------------------------------------------

def to_dict(j: Journey) -> dict:
    current_eta = decayed_eta(j.eta_sec, j.last_updated)
    current_status = (
        _compute_status(current_eta, j.distance_m or 0.0, j.goal_time)
        if current_eta is not None else j.status
    )
    return {
        "journey_id": j.journey_id,
        "member_id": j.member_id,
        "title": j.title,
        "journey_type": j.journey_type,
        "origin": {
            "lat": j.origin_lat, "lon": j.origin_lon,
            "name": j.origin_name, "address": j.origin_address,
        },
        "destination": {
            "lat": j.dest_lat, "lon": j.dest_lon,
            "name": j.dest_name, "address": j.dest_address,
        },
        "travel_mode": j.travel_mode,
        "goal_time": j.goal_time.isoformat() if j.goal_time else None,
        "last_train": j.last_train,
        "repeat_days": j.repeat_days,
        "planned_date": j.planned_date,
        "alarm_enabled": j.alarm_enabled,
        "has_location": j.current_lat is not None,
        "eta_sec": current_eta,
        "eta_min": round(current_eta / 60, 1) if current_eta is not None else None,
        "distance_m": j.distance_m,
        "alarm_time": j.alarm_time,
        "status": current_status,
        "last_updated": j.last_updated,
    }
