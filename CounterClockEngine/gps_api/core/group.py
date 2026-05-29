"""
Group appointment tracking module.

DB schema mapping:
  Group          ↔ Appointment  (appointment_id, title, destination, goal_time, status, invite_code)
  Participant    ↔ Participant   (participant_id, member_id, is_host, travel_mode, origin,
                                  current_location, departure_alarm_time, ETA, participant_status, alarm_switch)

Communicates with an external DB server when the DB_BASE_URL environment variable is set;
otherwise operates with an in-memory store (for demo/local testing).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from gps_api.core import cache as _cache
from gps_api.core.db_client import DBClient
from gps_api.core.kakao_route import fetch_route
from gps_api.core.latency import recommended_buffer
from gps_api.core.optimizer import haversine, calculate_next_interval, LocationPoint, decayed_eta

ARRIVAL_RADIUS_M = 100.0
DEFAULT_SPEED_MPS = 1.4
MAX_WORKERS = 5

# DB client (set via init_db())
_db: Optional[DBClient] = None

# In-memory fallback store
_store: dict[str, "Group"] = {}


def init_db(client: DBClient) -> None:
    global _db
    _db = client


# ------------------------------------------------------------------
# Data model (1:1 mapping with DB schema)
# ------------------------------------------------------------------

@dataclass
class Participant:
    participant_id: str
    member_id: str           # Member ID FK
    name: str
    is_host: bool = False    # Host flag
    travel_mode: str = "transit"  # Travel mode: walking | transit | vehicle

    # Origin
    origin_lat: Optional[float] = None
    origin_lon: Optional[float] = None
    origin_name: Optional[str] = None
    origin_address: Optional[str] = None

    # Current latitude/longitude (updated on each GPS update)
    current_lat: Optional[float] = None
    current_lon: Optional[float] = None

    # Computed results (updated on each ETA recalculation → written back to DB)
    eta_sec: Optional[float] = None
    distance_m: Optional[float] = None
    alarm_time: Optional[str] = None   # Departure alarm time (ISO 8601)
    alarm_enabled: bool = True         # Participant alarm switch
    status: str = "unknown"            # Participant status
    last_updated: Optional[str] = None


@dataclass
class Group:
    group_id: str                      # Appointment ID
    title: str                         # Appointment title
    destination: tuple[float, float]   # (destination latitude, destination longitude)
    appointment_time: datetime         # Goal time
    destination_name: Optional[str] = None
    destination_address: Optional[str] = None
    status: str = "active"             # Appointment status
    invite_code: Optional[str] = None  # Invite code
    participants: dict[str, Participant] = field(default_factory=dict)


# ------------------------------------------------------------------
# In-memory CRUD (used when DB is not connected)
# ------------------------------------------------------------------

def create_group(
    title: str,
    destination: tuple[float, float],
    appointment_time: datetime,
    destination_name: Optional[str] = None,
    destination_address: Optional[str] = None,
) -> Group:
    group = Group(
        group_id=str(uuid4()),
        title=title,
        destination=destination,
        appointment_time=appointment_time,
        destination_name=destination_name,
        destination_address=destination_address,
        invite_code=str(uuid4())[:8].upper(),  # 8-character invite code
    )
    _store[group.group_id] = group
    return group


def get_group(group_id: str) -> Optional[Group]:
    if _db:
        raw = _cache.get_appointment(group_id) or _db.get_appointment(group_id)
        return _appointment_to_group(raw) if raw else None
    return _store.get(group_id)


def get_group_by_invite(invite_code: str) -> Optional[Group]:
    if _db:
        raw = _cache.get_appointment_by_invite(invite_code) or _db.get_appointment_by_invite(invite_code)
        return _appointment_to_group(raw) if raw else None
    return next((g for g in _store.values() if g.invite_code == invite_code), None)


def join_group(
    group_id: str,
    member_id: str,
    name: str,
    travel_mode: str = "transit",
    origin_lat: Optional[float] = None,
    origin_lon: Optional[float] = None,
    origin_name: Optional[str] = None,
    origin_address: Optional[str] = None,
) -> Optional[Participant]:
    group = _store.get(group_id) if not _db else get_group(group_id)
    if not group:
        return None

    participant = Participant(
        participant_id=str(uuid4()),
        member_id=member_id,
        name=name,
        travel_mode=travel_mode,
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        origin_name=origin_name,
        origin_address=origin_address,
        # Origin is also the initial current location
        current_lat=origin_lat,
        current_lon=origin_lon,
    )

    if not _db:
        group.participants[member_id] = participant

    return participant


def leave_group(group_id: str, member_id: str) -> bool:
    if _db:
        return False  # Handled on the DB server side
    group = _store.get(group_id)
    if not group or member_id not in group.participants:
        return False
    del group.participants[member_id]
    return True


# ------------------------------------------------------------------
# ETA calculation
# ------------------------------------------------------------------

def _compute_status(eta_sec: float, distance_m: float, appointment_time: datetime) -> str:
    if distance_m <= ARRIVAL_RADIUS_M:
        return "arrived"
    now = datetime.now()
    time_until_appt = (appointment_time - now).total_seconds()
    if time_until_appt < 0:
        return "late"
    margin = time_until_appt - eta_sec
    if margin >= 600:
        return "on_time"
    elif margin >= 0:
        return "leave_soon"
    else:
        return "hurry"


def _get_buffer_minutes(member_id: str) -> float:
    """
    Buffer priority:
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


def _compute_alarm_time(eta_sec: float, appointment_time: datetime, member_id: str) -> str:
    """Departure alarm time = appointment time - ETA - personal buffer (member settings take priority)"""
    buffer_sec = _get_buffer_minutes(member_id) * 60
    alarm_dt = appointment_time - timedelta(seconds=eta_sec + buffer_sec)
    return alarm_dt.isoformat()


def _fetch_eta(
    participant: Participant,
    destination: tuple[float, float],
    appointment_time: datetime,
    kakao_api_key: str,
) -> None:
    """Compute ETA for one participant and update the participant object in-place."""
    lat, lon = participant.current_lat, participant.current_lon
    dest_lat, dest_lon = destination

    try:
        if not kakao_api_key:
            raise ValueError("no key")
        _, duration_sec, distance_m = fetch_route(
            lat, lon, dest_lat, dest_lon, kakao_api_key,
        )
        eta_sec = float(duration_sec)
    except Exception:
        distance_m = haversine(lat, lon, dest_lat, dest_lon)
        eta_sec = distance_m / DEFAULT_SPEED_MPS

    participant.eta_sec = round(eta_sec, 1)
    participant.distance_m = round(distance_m, 1)
    participant.status = _compute_status(eta_sec, distance_m, appointment_time)
    participant.alarm_time = _compute_alarm_time(eta_sec, appointment_time, participant.member_id)
    participant.last_updated = datetime.now().isoformat()


def compute_gps_interval(
    participant: Participant,
    destination: tuple[float, float],
    appointment_time: Optional[datetime] = None,
) -> dict:
    """Calculate the next GPS update interval and mode based on the participant's current location and ETA."""
    dest_lat, dest_lon = destination
    history = []
    if participant.current_lat is not None:
        history = [LocationPoint(lat=participant.current_lat, lon=participant.current_lon)]

    from gps_api.core.optimizer import Geofence
    geofences = [Geofence(id="destination", lat=dest_lat, lon=dest_lon, radius=100.0)]

    if participant.current_lat is None:
        return {"next_interval_sec": 60, "gps_mode": "BALANCED"}

    appointment_remaining_sec: Optional[float] = None
    if appointment_time is not None:
        appointment_remaining_sec = max((appointment_time - datetime.now()).total_seconds(), 0.0)

    current_eta = decayed_eta(participant.eta_sec, participant.last_updated)

    result = calculate_next_interval(
        user_lat=participant.current_lat,
        user_lon=participant.current_lon,
        history=history,
        geofences=geofences,
        eta_sec=current_eta,
        appointment_remaining_sec=appointment_remaining_sec,
    )
    return {
        "next_interval_sec": result.next_interval,
        "gps_mode": result.gps_mode,
    }


def _run_parallel_eta(
    participants: list[Participant],
    destination: tuple[float, float],
    appointment_time: datetime,
    kakao_api_key: str,
) -> None:
    targets = [p for p in participants if p.current_lat is not None]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=min(len(targets), MAX_WORKERS)) as executor:
        futures = [
            executor.submit(_fetch_eta, p, destination, appointment_time, kakao_api_key)
            for p in targets
        ]
        for f in as_completed(futures):
            f.result()


def update_location(
    group_id: str,
    member_id: str,
    lat: float,
    lon: float,
    kakao_api_key: str = "",
) -> Optional[tuple[dict, dict[str, dict]]]:
    """
    Update a participant's current location and recalculate ETA for the entire group.
    When connected to DB, writes calculation results back to DB.

    Returns:
      (group_summary, gps_intervals)
        group_summary  — dict of overall group ETA status
        gps_intervals  — { member_id: {"next_interval_sec": int, "gps_mode": str}, ... }
    """
    if _db:
        return _update_location_db(group_id, member_id, lat, lon, kakao_api_key)
    return _update_location_memory(group_id, member_id, lat, lon, kakao_api_key)


def _update_location_memory(
    group_id: str, member_id: str, lat: float, lon: float, kakao_api_key: str
) -> Optional[tuple[dict, dict]]:
    group = _store.get(group_id)
    if not group or member_id not in group.participants:
        return None

    group.participants[member_id].current_lat = lat
    group.participants[member_id].current_lon = lon

    _run_parallel_eta(
        list(group.participants.values()),
        group.destination,
        group.appointment_time,
        kakao_api_key,
    )

    intervals = {
        mid: compute_gps_interval(p, group.destination, group.appointment_time)
        for mid, p in group.participants.items()
    }
    return get_group_summary(group), intervals


def _update_location_db(
    group_id: str, member_id: str, lat: float, lon: float, kakao_api_key: str
) -> Optional[tuple[dict, dict]]:
    raw_appt = _cache.get_appointment(group_id) or _db.get_appointment(group_id)
    if not raw_appt:
        return None

    cached_parts = _cache.get_participants(group_id)
    raw_participants = cached_parts if cached_parts is not None else _db.get_participants(group_id)
    participants = [_participant_from_db(p) for p in raw_participants]

    # Apply current participant location
    target = next((p for p in participants if p.member_id == member_id), None)
    if not target:
        return None
    target.current_lat = lat
    target.current_lon = lon

    group = _appointment_to_group(raw_appt)
    _run_parallel_eta(participants, group.destination, group.appointment_time, kakao_api_key)

    # Write calculation results back to DB
    for p in participants:
        if p.last_updated:  # Only participants whose ETA has been computed
            _db.update_participant(
                p.participant_id,
                current_lat=p.current_lat,
                current_lon=p.current_lon,
                eta=p.eta_sec,
                status=p.status,
                alarm_time=p.alarm_time,
            )

    group.participants = {p.member_id: p for p in participants}

    intervals = {
        p.member_id: compute_gps_interval(p, group.destination, group.appointment_time)
        for p in participants
    }
    return get_group_summary(group), intervals


def mark_arrived(group_id: str, member_id: str) -> Optional[dict]:
    now_iso = datetime.now().isoformat()

    if _db:
        raw_appt = _cache.get_appointment(group_id) or _db.get_appointment(group_id)
        if not raw_appt:
            return None
        cached_parts = _cache.get_participants(group_id)
        raw_participants = cached_parts if cached_parts is not None else _db.get_participants(group_id)
        participants = [_participant_from_db(p) for p in raw_participants]
        target = next((p for p in participants if p.member_id == member_id), None)
        if not target:
            return None
        target.status, target.eta_sec, target.distance_m, target.last_updated = (
            "arrived", 0.0, 0.0, now_iso
        )
        _db.update_participant(target.participant_id, status="arrived", eta=0)
        group = _appointment_to_group(raw_appt)
        group.participants = {p.member_id: p for p in participants}
        _record_arrival(member_id, group_id, group.appointment_time, now_iso)
        return get_group_summary(group)

    group = _store.get(group_id)
    if not group or member_id not in group.participants:
        return None
    p = group.participants[member_id]
    p.status, p.eta_sec, p.distance_m, p.last_updated = "arrived", 0.0, 0.0, now_iso
    _record_arrival(member_id, group_id, group.appointment_time, now_iso)
    return get_group_summary(group)


def _record_arrival(
    member_id: str,
    group_id: str,
    appointment_time: datetime,
    arrived_iso: str,
) -> None:
    """Automatically saves a lateness record when arrival is processed. Arrival processing continues even if this fails."""
    from gps_api.core.latency import save_record, ArrivalRecord
    try:
        save_record(ArrivalRecord(
            user_id=member_id,
            event_id=group_id,
            scheduled_time=appointment_time.isoformat(),
            actual_arrival_time=arrived_iso,
            location_id=group_id,
        ))
    except Exception:
        pass


# ------------------------------------------------------------------
# Geofence exit handling (DB server push → departure detection)
# ------------------------------------------------------------------

# Initial GPS update interval assigned immediately after geofence exit (BALANCED mode)
_GEOFENCE_EXIT_INTERVAL = {"next_interval_sec": 10, "gps_mode": "BALANCED"}


def handle_geofence_exit(
    appointment_id: str,
    member_id: str,
    lat: float,
    lon: float,
    kakao_api_key: str = "",
) -> Optional[tuple[dict, dict]]:
    """
    Called when the DB server detects a participant has exited the 500m origin geofence.

    Processing order:
      1. Update the participant's current location to the exit point coordinates
      2. Recalculate ETA for the entire group (including the exiting participant)
      3. Immediately set the exiting participant's GPS interval to BALANCED (10 seconds)
      4. DB write-back

    Returns:
      (group_summary, gps_intervals)
        gps_intervals[member_id] is always BALANCED (10s) — right after movement starts
        Other participants use Adaptive Interval calculated values
    """
    result = update_location(appointment_id, member_id, lat, lon, kakao_api_key)
    if result is None:
        return None

    summary, intervals = result
    # Force short interval for the exiting participant since movement just started
    intervals[member_id] = _GEOFENCE_EXIT_INTERVAL
    return summary, intervals


# ------------------------------------------------------------------
# DB response → internal model conversion
# ------------------------------------------------------------------

def _appointment_to_group(raw: dict) -> Group:
    return Group(
        group_id=raw["appointment_id"],
        title=raw.get("title", ""),
        destination=(float(raw["destination_lat"]), float(raw["destination_lon"])),
        appointment_time=datetime.fromisoformat(raw["appointment_time"]),
        destination_name=raw.get("destination_name"),
        destination_address=raw.get("destination_address"),
        status=raw.get("status", "active"),
        invite_code=raw.get("invite_code"),
    )


def _participant_from_db(raw: dict) -> Participant:
    return Participant(
        participant_id=raw["participant_id"],
        member_id=raw["member_id"],
        name=raw.get("name", raw["member_id"]),
        is_host=raw.get("is_host", False),
        travel_mode=raw.get("travel_mode", "transit"),
        origin_lat=raw.get("origin_lat"),
        origin_lon=raw.get("origin_lon"),
        origin_name=raw.get("origin_name"),
        origin_address=raw.get("origin_address"),
        current_lat=raw.get("current_lat"),
        current_lon=raw.get("current_lon"),
        eta_sec=raw.get("eta"),
        alarm_time=raw.get("alarm_time"),
        alarm_enabled=raw.get("alarm_enabled", True),
        status=raw.get("status", "unknown"),
    )


# ------------------------------------------------------------------
# Serialization
# ------------------------------------------------------------------

def get_group_summary(group: Group) -> dict:
    return {
        "group_id": group.group_id,
        "title": group.title,
        "destination": list(group.destination),
        "destination_name": group.destination_name,
        "appointment_time": group.appointment_time.isoformat(),
        "status": group.status,
        "invite_code": group.invite_code,
        "participants": [
            {
                "participant_id": p.participant_id,
                "member_id": p.member_id,
                "name": p.name,
                "is_host": p.is_host,
                "travel_mode": p.travel_mode,
                "has_location": p.current_lat is not None,
                "eta_sec": current_eta,
                "eta_min": round(current_eta / 60, 1) if current_eta is not None else None,
                "distance_m": p.distance_m,
                "alarm_time": p.alarm_time,
                "alarm_enabled": p.alarm_enabled,
                "status": _compute_status(current_eta, p.distance_m or 0.0, group.appointment_time)
                          if current_eta is not None else p.status,
                "last_updated": p.last_updated,
            }
            for p in group.participants.values()
            for current_eta in [decayed_eta(p.eta_sec, p.last_updated)]
        ],
    }
