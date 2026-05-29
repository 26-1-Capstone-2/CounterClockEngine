"""
In-memory cache for data pushed from the DB server.

Cache priority:
  1. This cache (latest data pushed by the DB server via Webhook)
  2. DBClient direct query (fallback on cache miss)
  3. In-memory store (when DB_BASE_URL is not set)

TTL: default 300 seconds
"""

import time
from threading import Lock
from typing import Optional

_TTL_SEC = 300
_lock = Lock()

# ------------------------------------------------------------------
# Internal storage
# ------------------------------------------------------------------

_appt_cache: dict[str, tuple[dict, float]] = {}        # appointment_id → (data, exp)
_invite_index: dict[str, str] = {}                     # invite_code → appointment_id
_part_cache: dict[str, list[tuple[dict, float]]] = {}  # appointment_id → [(data, exp)]
_settings_cache: dict[str, tuple[dict, float]] = {}    # member_id → (data, exp)
_journey_cache: dict[str, tuple[dict, float]] = {}     # journey_id → (data, exp)
_member_journeys_cache: dict[str, tuple[list, float]] = {}  # member_id → (list, exp)


# ------------------------------------------------------------------
# Appointment
# ------------------------------------------------------------------

def put_appointment(data: dict) -> None:
    appt_id = data.get("appointment_id")
    if not appt_id:
        return
    expires = time.monotonic() + _TTL_SEC
    with _lock:
        _appt_cache[appt_id] = (data, expires)
        invite_code = data.get("invite_code")
        if invite_code:
            _invite_index[invite_code] = appt_id


def get_appointment(appointment_id: str) -> Optional[dict]:
    with _lock:
        entry = _appt_cache.get(appointment_id)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def get_appointment_by_invite(invite_code: str) -> Optional[dict]:
    with _lock:
        appt_id = _invite_index.get(invite_code)
    if not appt_id:
        return None
    return get_appointment(appt_id)


def invalidate_appointment(appointment_id: str) -> None:
    with _lock:
        data, _ = _appt_cache.pop(appointment_id, (None, None))
        if data and data.get("invite_code"):
            _invite_index.pop(data["invite_code"], None)
        _part_cache.pop(appointment_id, None)


# ------------------------------------------------------------------
# Participants
# ------------------------------------------------------------------

def put_participants(appointment_id: str, participants: list[dict]) -> None:
    expires = time.monotonic() + _TTL_SEC
    with _lock:
        _part_cache[appointment_id] = [(p, expires) for p in participants]


def put_participant(appointment_id: str, participant: dict) -> None:
    """Single participant upsert (for create/update Webhook)."""
    pid = participant.get("participant_id")
    if not pid:
        return
    with _lock:
        entries = _part_cache.get(appointment_id, [])
        expires = time.monotonic() + _TTL_SEC
        updated = [(p, e) for p, e in entries if p.get("participant_id") != pid]
        updated.append((participant, expires))
        _part_cache[appointment_id] = updated


def get_participants(appointment_id: str) -> Optional[list[dict]]:
    now = time.monotonic()
    with _lock:
        entries = _part_cache.get(appointment_id)
    if entries is None:
        return None
    valid = [p for p, exp in entries if exp > now]
    return valid if valid else None


# ------------------------------------------------------------------
# Member Settings (buffer, etc.)
# ------------------------------------------------------------------

def put_member_settings(member_id: str, data: dict) -> None:
    with _lock:
        _settings_cache[member_id] = (data, time.monotonic() + _TTL_SEC)


def get_member_settings(member_id: str) -> Optional[dict]:
    with _lock:
        entry = _settings_cache.get(member_id)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


# ------------------------------------------------------------------
# Journey (personal journey)
# ------------------------------------------------------------------

def put_journey(data: dict) -> None:
    journey_id = data.get("journey_id")
    if not journey_id:
        return
    with _lock:
        _journey_cache[journey_id] = (data, time.monotonic() + _TTL_SEC)


def get_journey(journey_id: str) -> Optional[dict]:
    with _lock:
        entry = _journey_cache.get(journey_id)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def put_member_journeys(member_id: str, journeys: list[dict]) -> None:
    """Save the full journey list for a member to cache. Also registers each journey in the individual cache."""
    with _lock:
        _member_journeys_cache[member_id] = (journeys, time.monotonic() + _TTL_SEC)
        expires = time.monotonic() + _TTL_SEC
        for j in journeys:
            jid = j.get("journey_id")
            if jid:
                _journey_cache[jid] = (j, expires)


def get_member_journeys(member_id: str) -> Optional[list[dict]]:
    with _lock:
        entry = _member_journeys_cache.get(member_id)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None
