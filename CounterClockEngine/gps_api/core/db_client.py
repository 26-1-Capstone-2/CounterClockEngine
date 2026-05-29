"""
External DB server HTTP client.

Assumes the DB server provides the following REST API.
If the actual DB server's endpoints/field names differ, only this file needs to be modified.

────────────────────────────────────────
Expected response format per table
────────────────────────────────────────

[Member]
  GET /members/{id}
  {
    "member_id": "...", "email": "...", "nickname": "...",
    "home_name": "집", "home_address": "...",
    "home_lat": 37.4979, "home_lon": 127.0276
  }

[Member Settings]
  GET /members/{id}/settings
  {
    "setting_id": "...", "member_id": "...",
    "buffer_minutes": 10,          ← buffer time
    "preferred_transit": "transit",  ← preferred transit mode
    "route_priority": "RECOMMEND"    ← route priority
  }

[Place]
  GET /members/{id}/places
  [ { "place_id": "...", "member_id": "...", "place_type": "work",
      "place_name": "회사", "address": "...", "lat": ..., "lon": ... }, ... ]

[Journey]
  GET  /journeys/{id}
  GET  /members/{id}/journeys
  POST /journeys
  PATCH /journeys/{id}
  {
    "journey_id": "...", "member_id": "...", "title": "...",
    "journey_type": "one_way",
    "last_train": false,             ← last train flag
    "planned_date": "2026-05-23",
    "travel_mode": "transit",
    "origin_name": "집", "origin_address": "...",
    "origin_lat": 37.4979, "origin_lon": 127.0276,
    "dest_name": "회사", "dest_address": "...",
    "dest_lat": 37.5088, "dest_lon": 127.0632,
    "current_lat": null, "current_lon": null,
    "goal_time": "2026-05-23T09:00:00",
    "eta": null,
    "alarm_time": null,
    "repeat_days": "MON,WED,FRI",   ← repeat days (null if none)
    "status": "unknown",
    "alarm_enabled": true
  }

[Appointment]
  GET /appointments/{id}
  GET /appointments/invite/{code}
  {
    "appointment_id": "...", "title": "...",
    "planned_date": "2026-05-23",
    "dest_name": "강남역", "dest_address": "...",
    "dest_lat": 37.5088, "dest_lon": 127.0632,
    "goal_time": "2026-05-23T19:00:00",
    "status": "active",
    "invite_code": "ABC123"
  }

[Participant]
  GET  /appointments/{id}/participants
  POST /appointments/{id}/participants
  PATCH /participants/{id}
  {
    "participant_id": "...", "member_id": "...", "appointment_id": "...",
    "is_host": false,
    "travel_mode": "transit",
    "origin_name": "집", "origin_address": "...",
    "origin_lat": 37.4979, "origin_lon": 127.0276,
    "current_lat": null, "current_lon": null,
    "alarm_time": null,
    "eta": null,
    "status": "unknown",
    "alarm_enabled": true
  }
"""

from typing import Optional

import requests


class DBClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    def _get(self, path: str) -> Optional[dict | list]:
        resp = self._session.get(f"{self.base_url}{path}", timeout=5)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        resp = self._session.post(f"{self.base_url}{path}", json=data, timeout=5)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, data: dict) -> dict:
        resp = self._session.patch(f"{self.base_url}{path}", json=data, timeout=5)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Member
    # ------------------------------------------------------------------

    def get_member(self, member_id: str) -> Optional[dict]:
        """Retrieve member info (including home latitude/longitude)."""
        return self._get(f"/members/{member_id}")

    def get_member_settings(self, member_id: str) -> Optional[dict]:
        """Retrieve member settings (buffer time, preferred transit, route priority)."""
        return self._get(f"/members/{member_id}/settings")

    # ------------------------------------------------------------------
    # Place
    # ------------------------------------------------------------------

    def get_places(self, member_id: str) -> list[dict]:
        """Retrieve saved place list for a member."""
        result = self._get(f"/members/{member_id}/places")
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Journey
    # ------------------------------------------------------------------

    def get_journey(self, journey_id: str) -> Optional[dict]:
        return self._get(f"/journeys/{journey_id}")

    def get_member_journeys(self, member_id: str) -> list[dict]:
        result = self._get(f"/members/{member_id}/journeys")
        return result if isinstance(result, list) else []

    def update_journey(self, journey_id: str, **fields) -> dict:
        """
        Update the current location, ETA, status, and alarm time of a journey.

        Supported fields:
          current_lat, current_lon  — current latitude/longitude
          eta                       — estimated arrival time (seconds)
          alarm_time                — departure alarm time (ISO 8601)
          status                    — journey status
        """
        return self._patch(f"/journeys/{journey_id}", fields)

    # ------------------------------------------------------------------
    # Appointment
    # ------------------------------------------------------------------

    def get_appointment(self, appointment_id: str) -> Optional[dict]:
        return self._get(f"/appointments/{appointment_id}")

    def get_appointment_by_invite(self, invite_code: str) -> Optional[dict]:
        """Retrieve appointment by invite code."""
        return self._get(f"/appointments/invite/{invite_code}")

    # ------------------------------------------------------------------
    # Participant
    # ------------------------------------------------------------------

    def get_participants(self, appointment_id: str) -> list[dict]:
        result = self._get(f"/appointments/{appointment_id}/participants")
        return result if isinstance(result, list) else []

    def update_participant(self, participant_id: str, **fields) -> dict:
        """
        Write back participant location, ETA, status, and alarm time to DB.

        Supported fields:
          current_lat, current_lon  — current latitude/longitude
          eta                       — estimated arrival time (seconds)
          alarm_time                — departure alarm time (ISO 8601)
          status                    — participant status
        """
        return self._patch(f"/participants/{participant_id}", fields)
