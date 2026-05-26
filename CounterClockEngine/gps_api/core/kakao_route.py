"""
카카오 모빌리티 길찾기 API 연동
- 출발지/목적지 좌표 → 실제 경로 좌표 + 소요 시간
- 경로를 시뮬레이션용 고정 간격 스텝으로 리샘플링

주의: 카카오 API는 좌표를 경도,위도(lon,lat) 순서로 받음
"""

from datetime import datetime, timedelta

import requests

KAKAO_DIRECTION_URL = "https://apis-navi.kakaomobility.com/v1/directions"



def fetch_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    api_key: str,
    priority: str = "RECOMMEND",
) -> tuple[list[tuple[float, float]], int, int]:
    """
    카카오 모빌리티 길찾기 API 호출

    Returns:
        coords      : [(lat, lon), ...] 경로 좌표 목록
        duration_sec: 총 소요 시간 (초)
        distance_m  : 총 거리 (미터)
    """
    headers = {"Authorization": f"KakaoAK {api_key}"}
    params = {
        "origin":      f"{origin_lon},{origin_lat}",   # 카카오는 경도,위도 순서
        "destination": f"{dest_lon},{dest_lat}",
        "priority":    priority,
    }

    resp = requests.get(KAKAO_DIRECTION_URL, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("routes"):
        raise ValueError("경로 응답이 비어 있습니다.")

    route = data["routes"][0]
    result_code = route.get("result_code", 0)
    if result_code != 0:
        raise ValueError(f"경로 탐색 실패 (code={result_code}): {route.get('result_msg', '알 수 없음')}")

    summary      = route["summary"]
    duration_sec = summary["duration"]
    distance_m   = summary["distance"]

    # vertexes: [lon0, lat0, lon1, lat1, ...] 연속 배열
    coords: list[tuple[float, float]] = []
    for section in route["sections"]:
        for road in section["roads"]:
            verts = road["vertexes"]
            for i in range(0, len(verts) - 1, 2):
                lon_v = verts[i]
                lat_v = verts[i + 1]
                if not coords or coords[-1] != (lat_v, lon_v):
                    coords.append((lat_v, lon_v))

    if not coords:
        raise ValueError("경로 좌표를 추출할 수 없습니다.")

    return coords, duration_sec, distance_m


def _cumulative_distances(coords: list[tuple[float, float]]) -> list[float]:
    """각 좌표까지의 누적 거리(m) 계산"""
    import math
    cum = [0.0]
    for i in range(1, len(coords)):
        lat1, lon1 = coords[i - 1]
        lat2, lon2 = coords[i]
        R = 6_371_000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi    = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        d = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        cum.append(cum[-1] + d)
    return cum


def resample_route(
    coords: list[tuple[float, float]],
    duration_sec: int,
    step_sec: int = 10,
) -> list[tuple[float, float, str]]:
    """
    실제 경로 좌표를 step_sec 간격으로 리샘플링하여
    시뮬레이션용 (lat, lon, speed_phase) 리스트 반환.

    거리 비례 방식: 누적 거리를 기준으로 시간 스텝마다 위치를 결정하여
    카카오 API가 도심에서 좌표를 밀집 반환하는 편향을 보정한다.
    """
    if not coords or duration_sec <= 0:
        return []

    cum      = _cumulative_distances(coords)
    total_d  = cum[-1]
    if total_d == 0:
        return []

    total_steps = max(1, round(duration_sec / step_sec))
    result = []

    for step in range(total_steps):
        # 이 스텝에서 이동해야 할 누적 거리
        target_d = (step / total_steps) * total_d

        # 누적 거리 배열에서 target_d 위치 탐색 (이진 탐색)
        lo, hi = 0, len(cum) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if cum[mid] <= target_d:
                lo = mid
            else:
                hi = mid

        seg_len = cum[hi] - cum[lo]
        frac    = (target_d - cum[lo]) / seg_len if seg_len > 0 else 0.0

        lat = coords[lo][0] + frac * (coords[hi][0] - coords[lo][0])
        lon = coords[lo][1] + frac * (coords[hi][1] - coords[lo][1])
        result.append((lat, lon, "vehicle"))

    return result


def calculate_departure_time(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    api_key: str,
    arrival_time: datetime,
    priority: str = "RECOMMEND",
) -> tuple[datetime, int, int]:
    """
    목표 도착 시간으로부터 출발해야 할 시간을 계산.

    Returns:
        departure_time: 출발해야 하는 시각
        duration_sec  : 예상 소요 시간 (초)
        distance_m    : 총 거리 (미터)
    """
    _, duration_sec, distance_m = fetch_route(
        origin_lat, origin_lon, dest_lat, dest_lon, api_key, priority
    )
    departure_time = arrival_time - timedelta(seconds=duration_sec)
    return departure_time, duration_sec, distance_m


def print_departure_plan(
    origin: tuple[float, float],
    dest: tuple[float, float],
    arrival_time: datetime,
    departure_time: datetime,
    duration_sec: int,
    distance_m: int,
) -> None:
    h, rem = divmod(duration_sec, 3600)
    m, s   = divmod(rem, 60)
    time_str = f"{h}시간 {m}분 {s}초" if h else f"{m}분 {s}초"

    print("\n" + "=" * 50)
    print("  출발 시간 안내")
    print("=" * 50)
    print(f"  출발지   : {origin[0]:.4f}, {origin[1]:.4f}")
    print(f"  목적지   : {dest[0]:.4f}, {dest[1]:.4f}")
    print(f"  총 거리  : {distance_m / 1000:.2f} km")
    print(f"  소요 시간: {time_str}")
    print("-" * 50)
    print(f"  목표 도착 시각: {arrival_time.strftime('%Y-%m-%d %H:%M')}")
    print(f"  출발해야 할 시각: {departure_time.strftime('%Y-%m-%d %H:%M')}")

    now = datetime.now()
    if departure_time < now:
        late_sec = int((now - departure_time).total_seconds())
        lh, lrem = divmod(late_sec, 3600)
        lm, ls   = divmod(lrem, 60)
        late_str = f"{lh}시간 {lm}분 {ls}초" if lh else f"{lm}분 {ls}초"
        print(f"  ⚠️  이미 출발 시각이 {late_str} 지났습니다.")
    else:
        remain_sec = int((departure_time - now).total_seconds())
        rh, rrem = divmod(remain_sec, 3600)
        rm, rs   = divmod(rrem, 60)
        remain_str = f"{rh}시간 {rm}분 {rs}초" if rh else f"{rm}분 {rs}초"
        print(f"  출발까지 남은 시간: {remain_str}")
    print("=" * 50)


def fetch_and_resample(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    api_key: str,
    step_sec: int = 10,
    departure_time: datetime = None,
    priority: str = "RECOMMEND",
) -> tuple[list[tuple[float, float, str]], int, int, datetime]:
    """
    카카오 API 호출 + 리샘플링을 한 번에 수행.

    Returns:
        route         : [(lat, lon, speed_phase), ...] 리샘플링된 경로
        duration_sec  : 총 소요 시간 (초)
        distance_m    : 총 거리 (미터)
        departure_time: 실제 사용된 출발 시각 (지정하지 않으면 현재 시각)
    """
    coords, duration_sec, distance_m = fetch_route(
        origin_lat, origin_lon, dest_lat, dest_lon, api_key, priority
    )
    route = resample_route(coords, duration_sec, step_sec)
    actual_departure = departure_time if departure_time is not None else datetime.now()
    return route, duration_sec, distance_m, actual_departure


def print_route_summary(
    origin: tuple[float, float],
    dest: tuple[float, float],
    duration_sec: int,
    distance_m: int,
    num_coords: int,
) -> None:
    h, rem = divmod(duration_sec, 3600)
    m, s   = divmod(rem, 60)
    time_str = f"{h}시간 {m}분 {s}초" if h else f"{m}분 {s}초"

    print(f"  출발지  : {origin[0]:.4f}, {origin[1]:.4f}")
    print(f"  목적지  : {dest[0]:.4f}, {dest[1]:.4f}")
    print(f"  총 거리 : {distance_m / 1000:.2f} km")
    print(f"  소요 시간: {time_str}")
    print(f"  경로 좌표: {num_coords}개 포인트")
