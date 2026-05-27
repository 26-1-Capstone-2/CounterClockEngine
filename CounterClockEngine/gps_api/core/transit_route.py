"""
ODSAY 대중교통 길찾기 API 연동
- 출발지/목적지 좌표 → 지하철+버스 혼합 경로
- API 키: https://lab.odsay.com (무료 가입 후 발급)
- 환경변수: ODSAY_API_KEY
"""

import requests
from dataclasses import dataclass, field
from datetime import datetime, timedelta

ODSAY_BASE_URL = "https://api.odsay.com/v1/api"

# ODSAY trafficType 코드
TRAFFIC_TYPE = {1: "SUBWAY", 2: "BUS", 3: "WALK"}
TRAFFIC_LABEL = {"SUBWAY": "지하철", "BUS": "버스", "WALK": "도보"}


@dataclass
class TransitLeg:
    mode: str           # SUBWAY / BUS / WALK
    duration_sec: int
    distance_m: int
    coords: list[tuple[float, float]] = field(default_factory=list)


def fetch_transit_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    api_key: str,
    search_dt: datetime | None = None,
) -> tuple[list[tuple[float, float]], int, int, list[TransitLeg]]:
    """
    ODSAY 대중교통 경로 조회.

    Args:
        search_dt: 출발 시각 기준 조회 (None이면 현재 시각 기준)

    Returns:
        coords      : [(lat, lon), ...] 전체 경로 좌표
        duration_sec: 총 소요 시간 (초)
        distance_m  : 총 거리 (미터)
        legs        : 구간별 TransitLeg 목록
    """
    params = {
        "apiKey": api_key,
        "SX": origin_lon,
        "SY": origin_lat,
        "EX": dest_lon,
        "EY": dest_lat,
        "OPT": 0,          # 0=최단시간
        "SearchType": 0,   # 0=지하철+버스
        "lang": 0,
    }
    if search_dt is not None:
        params["SearchDate"] = search_dt.strftime("%Y%m%d")
        params["SearchTime"] = search_dt.strftime("%H%M")
    resp = requests.get(
        f"{ODSAY_BASE_URL}/searchPubTransPathT",
        params=params, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise ValueError(f"ODSAY 오류: {data['error'].get('message', data['error'])}")

    paths = (data.get("result") or {}).get("path", [])
    if not paths:
        raise ValueError("ODSAY 경로 응답이 비어 있습니다.")

    path = paths[0]
    info = path.get("info", {})
    total_min  = info.get("totalTime", 0)
    total_dist = info.get("totalDistance", 0)
    duration_sec = total_min * 60

    legs: list[TransitLeg] = []
    all_coords: list[tuple[float, float]] = []

    for sub in path.get("subPath", []):
        t_type   = sub.get("trafficType", 3)
        mode     = TRAFFIC_TYPE.get(t_type, "WALK")
        leg_dur  = sub.get("sectionTime", 0) * 60
        leg_dist = sub.get("distance", 0)

        seg_coords: list[tuple[float, float]] = []

        # 정류장/역 좌표 목록
        for st in (sub.get("passStopList") or {}).get("stations", []):
            x = st.get("x") or st.get("lon")
            y = st.get("y") or st.get("lat")
            if x and y:
                try:
                    seg_coords.append((float(y), float(x)))
                except (TypeError, ValueError):
                    pass

        # passShape polyline (상세 선형) — "lon,lat lon,lat ..." 형식
        if not seg_coords:
            shape = (sub.get("passShape") or {}).get("polyline", "")
            for pair in shape.split():
                parts = pair.split(",")
                if len(parts) == 2:
                    try:
                        seg_coords.append((float(parts[1]), float(parts[0])))
                    except ValueError:
                        pass

        # 좌표 없으면 출발·도착 역만
        if not seg_coords:
            sx = sub.get("startX") or sub.get("startStation", {}).get("x")
            sy = sub.get("startY") or sub.get("startStation", {}).get("y")
            ex = sub.get("endX") or sub.get("endStation", {}).get("x")
            ey = sub.get("endY") or sub.get("endStation", {}).get("y")
            if sx and sy:
                seg_coords.append((float(sy), float(sx)))
            if ex and ey:
                seg_coords.append((float(ey), float(ex)))

        legs.append(TransitLeg(mode=mode, duration_sec=int(leg_dur),
                               distance_m=int(leg_dist), coords=seg_coords))
        all_coords.extend(seg_coords)

    if not all_coords:
        raise ValueError("ODSAY 경로 좌표를 추출할 수 없습니다.")

    return all_coords, int(duration_sec), int(total_dist), legs


def find_last_train_departure(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    api_key: str,
    base_dt: datetime,
) -> tuple[datetime, int] | None:
    """
    이진 탐색으로 유효한 마지막 대중교통 출발 시각을 찾는다.

    탐색 범위: 당일 23:00 ~ 익일 01:00 (자정 넘김 자동 처리)

    Returns:
        (last_departure_dt, duration_sec) 또는 None (막차 없음)
    """
    base_date = base_dt.date()
    lo = datetime(base_date.year, base_date.month, base_date.day, 23, 0)
    hi = lo + timedelta(hours=2)  # 익일 01:00

    # hi에서 유효한 경로가 없으면 탐색 범위 내 막차가 없는 것
    try:
        fetch_transit_route(origin_lat, origin_lon, dest_lat, dest_lon, api_key, hi)
    except ValueError:
        # hi가 막히면 lo부터 시작해 유효 여부 확인 후 탐색
        try:
            fetch_transit_route(origin_lat, origin_lon, dest_lat, dest_lon, api_key, lo)
        except ValueError:
            return None

    last_valid_dt: datetime | None = None
    last_valid_duration: int = 0

    while (hi - lo).total_seconds() > 60:  # 1분 단위 정밀도
        mid = lo + (hi - lo) / 2
        try:
            _, duration_sec, _, _ = fetch_transit_route(
                origin_lat, origin_lon, dest_lat, dest_lon, api_key, mid
            )
            last_valid_dt = mid
            last_valid_duration = duration_sec
            lo = mid + timedelta(minutes=1)
        except ValueError:
            hi = mid - timedelta(minutes=1)

    return (last_valid_dt, last_valid_duration) if last_valid_dt else None


def resample_transit_route(
    legs: list[TransitLeg],
    duration_sec: int,
    step_sec: int = 5,
) -> list[tuple[float, float, str]]:
    """
    구간별 좌표를 step_sec 간격으로 리샘플링.
    각 포인트에 구간 모드를 태깅: (lat, lon, mode)
    """
    import math

    def _haversine(a, b):
        R = 6_371_000
        phi1, phi2 = math.radians(a[0]), math.radians(b[0])
        dphi    = math.radians(b[0] - a[0])
        dlambda = math.radians(b[1] - a[1])
        x = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
        return R * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))

    result: list[tuple[float, float, str]] = []
    for leg in legs:
        if not leg.coords or leg.duration_sec <= 0:
            continue
        if len(leg.coords) == 1:
            result.append((leg.coords[0][0], leg.coords[0][1], leg.mode))
            continue

        # 누적 거리 계산
        cum = [0.0]
        for i in range(1, len(leg.coords)):
            cum.append(cum[-1] + _haversine(leg.coords[i-1], leg.coords[i]))
        total_d = cum[-1]
        if total_d == 0:
            continue

        n_steps = max(1, round(leg.duration_sec / step_sec))
        for step in range(n_steps):
            target_d = (step / n_steps) * total_d
            lo, hi = 0, len(cum) - 1
            while lo + 1 < hi:
                mid = (lo + hi) // 2
                if cum[mid] <= target_d:
                    lo = mid
                else:
                    hi = mid
            seg_len = cum[hi] - cum[lo]
            frac = (target_d - cum[lo]) / seg_len if seg_len > 0 else 0.0
            lat = leg.coords[lo][0] + frac * (leg.coords[hi][0] - leg.coords[lo][0])
            lon = leg.coords[lo][1] + frac * (leg.coords[hi][1] - leg.coords[lo][1])
            result.append((lat, lon, leg.mode))

    return result
