"""
Kakao Mobility directions API integration
- Origin/destination coordinates → actual route coordinates + travel time
- Resample route into fixed-interval steps for simulation

Note: Kakao API receives coordinates in longitude,latitude (lon,lat) order
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
    Kakao Mobility directions API call

    Returns:
        coords      : [(lat, lon), ...] list of route coordinates
        duration_sec: total travel time (seconds)
        distance_m  : total distance (meters)
    """
    headers = {"Authorization": f"KakaoAK {api_key}"}
    params = {
        "origin":      f"{origin_lon},{origin_lat}",   # Kakao uses longitude,latitude order
        "destination": f"{dest_lon},{dest_lat}",
        "priority":    priority,
    }

    resp = requests.get(KAKAO_DIRECTION_URL, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("routes"):
        raise ValueError("Route response is empty.")

    route = data["routes"][0]
    result_code = route.get("result_code", 0)
    if result_code != 0:
        raise ValueError(f"Route search failed (code={result_code}): {route.get('result_msg', 'Unknown')}")

    summary      = route["summary"]
    duration_sec = summary["duration"]
    distance_m   = summary["distance"]

    # vertexes: [lon0, lat0, lon1, lat1, ...] consecutive array
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
        raise ValueError("Unable to extract route coordinates.")

    return coords, duration_sec, distance_m


def _cumulative_distances(coords: list[tuple[float, float]]) -> list[float]:
    """Calculate cumulative distance (m) to each coordinate"""
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
    Resample actual route coordinates at step_sec intervals and return
    a simulation-ready (lat, lon, speed_phase) list.

    Distance-proportional method: determines position at each time step based on
    cumulative distance, correcting for the bias where Kakao API returns dense
    coordinates in urban areas.
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
        # Cumulative distance to travel at this step
        target_d = (step / total_steps) * total_d

        # Binary search for target_d in cumulative distance array
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
    Calculate the departure time needed to arrive by the target arrival time.

    Returns:
        departure_time: time when departure is needed
        duration_sec  : estimated travel time (seconds)
        distance_m    : total distance (meters)
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
    Perform Kakao API call + resampling in one step.

    Returns:
        route         : [(lat, lon, speed_phase), ...] resampled route
        duration_sec  : total travel time (seconds)
        distance_m    : total distance (meters)
        departure_time: actual departure time used (current time if not specified)
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
