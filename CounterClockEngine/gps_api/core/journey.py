"""
개인 여정(여정) 추적 모듈.

여정 = 1인 이동 추적 (그룹 약속과 별개)

DB 스키마 매핑:
  Journey ↔ 여정 (여정ID, 멤버ID, 이동수단, 출발지, 목적지,
                   현재위치, 목표시간, ETA, 출발알람, 막차여부,
                   반복요일, 여정상태, 알람스위치)

버퍼 우선순위:
  1순위: DB 멤버 설정의 '여유 시간'
  2순위: latency.py의 과거 지각 기록 기반 계산값
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
# 데이터 모델 (DB 스키마와 1:1 대응)
# ------------------------------------------------------------------

@dataclass
class Journey:
    journey_id: str
    member_id: str
    title: str
    journey_type: str = "one_way"   # 여정 타입

    # 출발지
    origin_lat: Optional[float] = None
    origin_lon: Optional[float] = None
    origin_name: Optional[str] = None
    origin_address: Optional[str] = None

    # 목적지
    dest_lat: Optional[float] = None
    dest_lon: Optional[float] = None
    dest_name: Optional[str] = None
    dest_address: Optional[str] = None

    # 현재 위치 (GPS 업데이트마다 갱신)
    current_lat: Optional[float] = None
    current_lon: Optional[float] = None

    # 설정
    travel_mode: str = "transit"
    goal_time: Optional[datetime] = None
    last_train: bool = False          # 막차 여부
    repeat_days: Optional[str] = None  # 반복 요일 (예: "MON,WED,FRI")
    planned_date: Optional[str] = None
    alarm_enabled: bool = True

    # 계산 결과 (ETA 재계산 시 갱신 → DB write-back)
    eta_sec: Optional[float] = None
    distance_m: Optional[float] = None
    alarm_time: Optional[str] = None   # 출발 알람 시각 (ISO 8601)
    status: str = "unknown"
    last_updated: Optional[str] = None


# ------------------------------------------------------------------
# 버퍼 계산 (멤버 설정 우선, latency 기록 fallback)
# ------------------------------------------------------------------

def _get_buffer_minutes(member_id: str) -> float:
    """
    1순위: 캐시된 멤버 설정의 '여유 시간' (DB Webhook으로 Push된 값)
    2순위: DB 멤버 설정 직접 조회 (캐시 미스 fallback)
    3순위: 과거 지각 기록 기반 계산값 (recommended_buffer)
    """
    settings = _cache.get_member_settings(member_id)
    if settings is None and _db:
        settings = _db.get_member_settings(member_id)
    if settings and settings.get("buffer_minutes") is not None:
        return float(settings["buffer_minutes"])
    return recommended_buffer(member_id)


# ------------------------------------------------------------------
# ETA + 알람 계산
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
    현재 위치 → 목적지 ETA를 계산하고 Journey 객체를 갱신한다.
    카카오 API 실패 시 Haversine 직선 거리로 fallback.
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
# DB 연동
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
    위치를 업데이트하고 ETA를 재계산한다.
    DB 연결 시 결과를 write-back한다.
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
# DB 응답 → 내부 모델 변환
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
# 직렬화
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
