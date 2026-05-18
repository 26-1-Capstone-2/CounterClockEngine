"""
그룹 약속 추적 모듈.

DB 스키마 매핑:
  Group          ↔ 약속  (약속ID, 제목, 목적지, 목표시간, 상태, 초대코드)
  Participant    ↔ 참여자 (참여자ID, 멤버ID, 방장여부, 이동수단, 출발지, 현재위치,
                           출발알람시각, ETA, 참여자상태, 알람스위치)

DB_BASE_URL 환경변수가 설정된 경우 외부 DB 서버와 통신하고,
설정되지 않은 경우 인메모리 store로 동작합니다 (데모/로컬 테스트용).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from gps_api.core.db_client import DBClient
from gps_api.core.kakao_route import fetch_route
from gps_api.core.latency import recommended_buffer
from gps_api.core.optimizer import haversine

ARRIVAL_RADIUS_M = 100.0
DEFAULT_SPEED_MPS = 1.4
MAX_WORKERS = 5

# DB 클라이언트 (init_db()로 설정)
_db: Optional[DBClient] = None

# 인메모리 fallback store
_store: dict[str, "Group"] = {}


def init_db(client: DBClient) -> None:
    global _db
    _db = client


# ------------------------------------------------------------------
# 데이터 모델 (DB 스키마와 1:1 대응)
# ------------------------------------------------------------------

@dataclass
class Participant:
    participant_id: str
    member_id: str           # 멤버 ID FK
    name: str
    is_host: bool = False    # 방장 여부
    travel_mode: str = "transit"  # 이동 수단: walking | transit | vehicle

    # 출발지
    origin_lat: Optional[float] = None
    origin_lon: Optional[float] = None
    origin_name: Optional[str] = None
    origin_address: Optional[str] = None

    # 현재 위도/경도 (GPS 업데이트마다 갱신)
    current_lat: Optional[float] = None
    current_lon: Optional[float] = None

    # 계산 결과 (ETA 재계산마다 갱신 → DB에 write-back)
    eta_sec: Optional[float] = None
    distance_m: Optional[float] = None
    alarm_time: Optional[str] = None   # 출발 알람 시각 (ISO 8601)
    alarm_enabled: bool = True         # 참가자 알람 스위치
    status: str = "unknown"            # 참여자 상태
    last_updated: Optional[str] = None


@dataclass
class Group:
    group_id: str                      # 약속 ID
    title: str                         # 약속 제목
    destination: tuple[float, float]   # (목적지 위도, 목적지 경도)
    appointment_time: datetime         # 목표 시간
    destination_name: Optional[str] = None
    destination_address: Optional[str] = None
    status: str = "active"             # 약속 상태
    invite_code: Optional[str] = None  # 초대 코드
    participants: dict[str, Participant] = field(default_factory=dict)


# ------------------------------------------------------------------
# 인메모리 CRUD (DB 미연결 시 사용)
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
        invite_code=str(uuid4())[:8].upper(),  # 8자리 초대 코드
    )
    _store[group.group_id] = group
    return group


def get_group(group_id: str) -> Optional[Group]:
    if _db:
        raw = _db.get_appointment(group_id)
        return _appointment_to_group(raw) if raw else None
    return _store.get(group_id)


def get_group_by_invite(invite_code: str) -> Optional[Group]:
    if _db:
        raw = _db.get_appointment_by_invite(invite_code)
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
        # 출발지가 곧 현재 위치의 초기값
        current_lat=origin_lat,
        current_lon=origin_lon,
    )

    if not _db:
        group.participants[member_id] = participant

    return participant


def leave_group(group_id: str, member_id: str) -> bool:
    if _db:
        return False  # DB 서버 측에서 처리
    group = _store.get(group_id)
    if not group or member_id not in group.participants:
        return False
    del group.participants[member_id]
    return True


# ------------------------------------------------------------------
# ETA 계산
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
    버퍼 우선순위:
      1순위: DB 멤버 설정의 '여유 시간'
      2순위: 과거 지각 기록 기반 계산값 (recommended_buffer)
    """
    if _db:
        settings = _db.get_member_settings(member_id)
        if settings and settings.get("buffer_minutes") is not None:
            return float(settings["buffer_minutes"])
    return recommended_buffer(member_id)


def _compute_alarm_time(eta_sec: float, appointment_time: datetime, member_id: str) -> str:
    """출발 알람 시각 = 약속 시간 - ETA - 개인 버퍼 (멤버 설정 우선)"""
    buffer_sec = _get_buffer_minutes(member_id) * 60
    alarm_dt = appointment_time - timedelta(seconds=eta_sec + buffer_sec)
    return alarm_dt.isoformat()


def _fetch_eta(
    participant: Participant,
    destination: tuple[float, float],
    appointment_time: datetime,
    kakao_api_key: str,
) -> None:
    """참여자 1명의 ETA를 계산해 participant 객체를 직접 갱신한다."""
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
) -> Optional[dict]:
    """
    참여자의 현재 위치를 업데이트하고 전체 그룹 ETA를 재계산한다.
    DB 연결 시 계산 결과를 DB에 write-back한다.
    """
    if _db:
        return _update_location_db(group_id, member_id, lat, lon, kakao_api_key)
    return _update_location_memory(group_id, member_id, lat, lon, kakao_api_key)


def _update_location_memory(
    group_id: str, member_id: str, lat: float, lon: float, kakao_api_key: str
) -> Optional[dict]:
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
    return get_group_summary(group)


def _update_location_db(
    group_id: str, member_id: str, lat: float, lon: float, kakao_api_key: str
) -> Optional[dict]:
    raw_appt = _db.get_appointment(group_id)
    if not raw_appt:
        return None

    raw_participants = _db.get_participants(group_id)
    participants = [_participant_from_db(p) for p in raw_participants]

    # 현재 참여자 위치 반영
    target = next((p for p in participants if p.member_id == member_id), None)
    if not target:
        return None
    target.current_lat = lat
    target.current_lon = lon

    group = _appointment_to_group(raw_appt)
    _run_parallel_eta(participants, group.destination, group.appointment_time, kakao_api_key)

    # 계산 결과를 DB에 write-back
    for p in participants:
        if p.last_updated:  # ETA가 계산된 참여자만
            _db.update_participant(
                p.participant_id,
                current_lat=p.current_lat,
                current_lon=p.current_lon,
                eta=p.eta_sec,
                status=p.status,
                alarm_time=p.alarm_time,
            )

    group.participants = {p.member_id: p for p in participants}
    return get_group_summary(group)


def mark_arrived(group_id: str, member_id: str) -> Optional[dict]:
    now_iso = datetime.now().isoformat()

    if _db:
        raw_appt = _db.get_appointment(group_id)
        if not raw_appt:
            return None
        raw_participants = _db.get_participants(group_id)
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
        return get_group_summary(group)

    group = _store.get(group_id)
    if not group or member_id not in group.participants:
        return None
    p = group.participants[member_id]
    p.status, p.eta_sec, p.distance_m, p.last_updated = "arrived", 0.0, 0.0, now_iso
    return get_group_summary(group)


# ------------------------------------------------------------------
# DB 응답 → 내부 모델 변환
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
# 직렬화
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
                "eta_sec": p.eta_sec,
                "eta_min": round(p.eta_sec / 60, 1) if p.eta_sec is not None else None,
                "distance_m": p.distance_m,
                "alarm_time": p.alarm_time,
                "alarm_enabled": p.alarm_enabled,
                "status": p.status,
                "last_updated": p.last_updated,
            }
            for p in group.participants.values()
        ],
    }
