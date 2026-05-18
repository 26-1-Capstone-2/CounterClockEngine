"""
외부 DB 서버 HTTP 클라이언트.

DB 서버가 아래 REST API를 제공한다고 가정합니다.
실제 DB 서버의 엔드포인트/필드명이 다를 경우 이 파일만 수정하면 됩니다.

────────────────────────────────────────
테이블별 예상 응답 형식
────────────────────────────────────────

[멤버]
  GET /members/{id}
  {
    "member_id": "...", "email": "...", "nickname": "...",
    "home_name": "집", "home_address": "...",
    "home_lat": 37.4979, "home_lon": 127.0276
  }

[멤버 설정]
  GET /members/{id}/settings
  {
    "setting_id": "...", "member_id": "...",
    "buffer_minutes": 10,          ← 여유 시간
    "preferred_transit": "transit",  ← 선호 대중교통
    "route_priority": "RECOMMEND"    ← 경로 우선순위
  }

[장소]
  GET /members/{id}/places
  [ { "place_id": "...", "member_id": "...", "place_type": "work",
      "place_name": "회사", "address": "...", "lat": ..., "lon": ... }, ... ]

[여정]
  GET  /journeys/{id}
  GET  /members/{id}/journeys
  POST /journeys
  PATCH /journeys/{id}
  {
    "journey_id": "...", "member_id": "...", "title": "...",
    "journey_type": "one_way",
    "last_train": false,             ← 막차 여부
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
    "repeat_days": "MON,WED,FRI",   ← 반복 요일 (없으면 null)
    "status": "unknown",
    "alarm_enabled": true
  }

[약속]
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

[참여자]
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
    # 멤버 (Member)
    # ------------------------------------------------------------------

    def get_member(self, member_id: str) -> Optional[dict]:
        """멤버 정보 조회 (집 위도/경도 포함)."""
        return self._get(f"/members/{member_id}")

    def get_member_settings(self, member_id: str) -> Optional[dict]:
        """멤버 설정 조회 (여유 시간, 선호 교통수단, 경로 우선순위)."""
        return self._get(f"/members/{member_id}/settings")

    # ------------------------------------------------------------------
    # 장소 (Place)
    # ------------------------------------------------------------------

    def get_places(self, member_id: str) -> list[dict]:
        """멤버의 저장된 장소 목록 조회."""
        result = self._get(f"/members/{member_id}/places")
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # 여정 (Journey)
    # ------------------------------------------------------------------

    def get_journey(self, journey_id: str) -> Optional[dict]:
        return self._get(f"/journeys/{journey_id}")

    def get_member_journeys(self, member_id: str) -> list[dict]:
        result = self._get(f"/members/{member_id}/journeys")
        return result if isinstance(result, list) else []

    def update_journey(self, journey_id: str, **fields) -> dict:
        """
        여정의 현재 위치·ETA·상태·알람 시각을 업데이트.

        지원 필드:
          current_lat, current_lon  — 현재 위도/경도
          eta                       — 도착 예정 시간(초)
          alarm_time                — 출발 알람 시각 (ISO 8601)
          status                    — 여정 상태
        """
        return self._patch(f"/journeys/{journey_id}", fields)

    # ------------------------------------------------------------------
    # 약속 (Appointment)
    # ------------------------------------------------------------------

    def get_appointment(self, appointment_id: str) -> Optional[dict]:
        return self._get(f"/appointments/{appointment_id}")

    def get_appointment_by_invite(self, invite_code: str) -> Optional[dict]:
        """초대 코드로 약속 조회."""
        return self._get(f"/appointments/invite/{invite_code}")

    # ------------------------------------------------------------------
    # 참여자 (Participant)
    # ------------------------------------------------------------------

    def get_participants(self, appointment_id: str) -> list[dict]:
        result = self._get(f"/appointments/{appointment_id}/participants")
        return result if isinstance(result, list) else []

    def update_participant(self, participant_id: str, **fields) -> dict:
        """
        참여자의 위치·ETA·상태·알람 시각을 DB에 write-back.

        지원 필드:
          current_lat, current_lon  — 현재 위도/경도
          eta                       — 도착 예정 시간(초)
          alarm_time                — 출발 알람 시각 (ISO 8601)
          status                    — 참여자 상태
        """
        return self._patch(f"/participants/{participant_id}", fields)
