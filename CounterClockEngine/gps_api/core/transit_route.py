"""
ODSAY public transit directions API integration
- Origin/destination coordinates → mixed subway+bus route
- API key: https://lab.odsay.com (free registration required)
- Environment variable: ODSAY_API_KEY
"""

import requests
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Last train search result cache: key → (result, expires_at)
# key: (origin_lat_r, origin_lon_r, dest_lat_r, dest_lon_r, date)
_last_train_cache: dict = {}


def _next_4am(now: datetime) -> datetime:
    """Returns the next 04:00 relative to the current time (tomorrow's 04:00 if today's 04:00 has passed)."""
    candidate = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    return candidate


def _cache_key(origin_lat: float, origin_lon: float,
               dest_lat: float, dest_lon: float,
               base_dt: datetime) -> tuple:
    """Generate cache key rounded to 2 decimal places (~1.1km granularity)."""
    return (
        round(origin_lat, 2), round(origin_lon, 2),
        round(dest_lat, 2),   round(dest_lon, 2),
        base_dt.date(),
    )


def _cache_get(key: tuple):
    entry = _last_train_cache.get(key)
    if entry is None:
        return None
    result, expires_at = entry
    if datetime.now() >= expires_at:
        del _last_train_cache[key]
        return None
    return result


def _cache_set(key: tuple, result) -> None:
    _last_train_cache[key] = (result, _next_4am(datetime.now()))

ODSAY_BASE_URL = "https://api.odsay.com/v1/api"

# ODSAY trafficType codes
TRAFFIC_TYPE = {1: "SUBWAY", 2: "BUS", 3: "WALK"}
TRAFFIC_LABEL = {"SUBWAY": "지하철", "BUS": "버스", "WALK": "도보"}


@dataclass
class TransitLeg:
    mode: str           # SUBWAY / BUS / WALK
    duration_sec: int
    distance_m: int
    coords: list[tuple[float, float]] = field(default_factory=list)


# transport_mode → ODSAY SearchType
SEARCH_TYPE_MAP = {"ALL": 0, "SUBWAY": 1, "BUS": 2}

# priority_type → ODSAY OPT
PRIORITY_OPT_MAP = {"MIN_TIME": 0, "MIN_TRANSFER": 1, "MIN_WALK": 2}


def fetch_transit_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    api_key: str,
    search_dt: datetime | None = None,
    search_type: int = 0,
    opt: int = 0,
) -> tuple[list[tuple[float, float]], int, int, list[TransitLeg]]:
    """
    ODSAY public transit route query.

    Args:
        search_dt  : Query based on departure time (defaults to current time if None)
        search_type: ODSAY SearchType — 0=subway+bus(ALL), 1=subway(SUBWAY), 2=bus(BUS)
        opt        : ODSAY OPT — 0=shortest time(MIN_TIME), 1=min transfers(MIN_TRANSFER), 2=min walking(MIN_WALK)

    Returns:
        coords      : [(lat, lon), ...] full route coordinates
        duration_sec: total travel time (seconds)
        distance_m  : total distance (meters)
        legs        : list of TransitLeg objects per segment
    """
    params = {
        "apiKey": api_key,
        "SX": origin_lon,
        "SY": origin_lat,
        "EX": dest_lon,
        "EY": dest_lat,
        "OPT": opt,
        "SearchType": search_type,
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
        raise ValueError(f"ODSAY error: {data['error'].get('message', data['error'])}")

    paths = (data.get("result") or {}).get("path", [])
    if not paths:
        raise ValueError("ODSAY route response is empty.")

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

        # Station/stop coordinate list
        for st in (sub.get("passStopList") or {}).get("stations", []):
            x = st.get("x") or st.get("lon")
            y = st.get("y") or st.get("lat")
            if x and y:
                try:
                    seg_coords.append((float(y), float(x)))
                except (TypeError, ValueError):
                    pass

        # passShape polyline (detailed geometry) — "lon,lat lon,lat ..." format
        if not seg_coords:
            shape = (sub.get("passShape") or {}).get("polyline", "")
            for pair in shape.split():
                parts = pair.split(",")
                if len(parts) == 2:
                    try:
                        seg_coords.append((float(parts[1]), float(parts[0])))
                    except ValueError:
                        pass

        # If no coordinates, use only departure/arrival station
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
        raise ValueError("Unable to extract ODSAY route coordinates.")

    return all_coords, int(duration_sec), int(total_dist), legs


def first_walk_min(legs: list[TransitLeg]) -> int:
    """Returns the travel time (minutes) of the first WALK segment in the subPath legs. Returns 0 if none."""
    for leg in legs:
        if leg.mode == "WALK":
            return round(leg.duration_sec / 60)
        break  # If the first segment is not WALK, no walking to station
    return 0


def total_walk_min(legs: list[TransitLeg]) -> int:
    """Returns the total walking time (minutes) summed across every WALK segment of the route."""
    total_sec = sum(leg.duration_sec for leg in legs if leg.mode == "WALK")
    return round(total_sec / 60)


# ── Short-distance walking fallback ──────────────────────────────────
# ODSAY returns no transit route when origin and destination are too close
# (~700 m). For these cases we estimate walking time directly from the
# straight-line distance, the same way map services show "도보 N분".
SHORT_DISTANCE_THRESHOLD_M = 700   # below this ODSAY returns no transit route
WALK_SPEED_M_PER_MIN       = 67.0  # ≈ 4 km/h average walking pace
WALK_DETOUR_FACTOR         = 1.3   # straight-line → street-following correction


def walk_fallback(origin_lat: float, origin_lon: float,
                  dest_lat: float, dest_lon: float) -> tuple[bool, int]:
    """
    Estimate walking time for a short trip from the straight-line distance.

    Returns (is_short, walk_minutes), where is_short indicates the points are
    closer than SHORT_DISTANCE_THRESHOLD_M (i.e. ODSAY would return no route).
    walk_minutes is always at least 1.
    """
    from gps_api.core.optimizer import haversine
    dist_m  = haversine(origin_lat, origin_lon, dest_lat, dest_lon)
    minutes = max(1, round((dist_m * WALK_DETOUR_FACTOR) / WALK_SPEED_M_PER_MIN))
    return dist_m < SHORT_DISTANCE_THRESHOLD_M, minutes


def find_last_train_departure(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    api_key: str,
    base_dt: datetime,
    search_type: int = 0,
    opt: int = 0,
) -> tuple[datetime, int, int, int] | None:
    """
    Finds the last valid public transit departure time using binary search.

    Search range: 23:00 on the same day to 01:00 the next day (midnight crossover handled automatically)
    Results are cached until 04:00 the next day.

    Args:
        search_type: ODSAY SearchType (0=ALL, 1=SUBWAY, 2=BUS)
        opt        : ODSAY OPT (0=MIN_TIME, 1=MIN_TRANSFER, 2=MIN_WALK)

    Returns:
        (last_departure_dt, duration_sec, walk_to_station_min, total_walk_min) or None (no last train)
    """
    key = _cache_key(origin_lat, origin_lon, dest_lat, dest_lon, base_dt)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    base_date = base_dt.date()
    lo = datetime(base_date.year, base_date.month, base_date.day, 23, 0)
    hi = lo + timedelta(hours=2)  # Next day 01:00

    # If no valid route exists at hi, there is no last train within the search range
    try:
        fetch_transit_route(origin_lat, origin_lon, dest_lat, dest_lon, api_key, hi,
                            search_type=search_type, opt=opt)
    except ValueError:
        try:
            fetch_transit_route(origin_lat, origin_lon, dest_lat, dest_lon, api_key, lo,
                                search_type=search_type, opt=opt)
        except ValueError:
            return None

    last_valid_dt: datetime | None = None
    last_valid_duration: int = 0
    last_valid_walk_min: int = 0
    last_valid_total_walk_min: int = 0

    while (hi - lo).total_seconds() > 60:  # 1-minute precision
        mid = lo + (hi - lo) / 2
        try:
            _, duration_sec, _, legs = fetch_transit_route(
                origin_lat, origin_lon, dest_lat, dest_lon, api_key, mid,
                search_type=search_type, opt=opt,
            )
            last_valid_dt             = mid
            last_valid_duration       = duration_sec
            last_valid_walk_min       = first_walk_min(legs)
            last_valid_total_walk_min = total_walk_min(legs)
            lo = mid + timedelta(minutes=1)
        except ValueError:
            hi = mid - timedelta(minutes=1)

    result = (
        last_valid_dt, last_valid_duration,
        last_valid_walk_min, last_valid_total_walk_min,
    ) if last_valid_dt else None
    _cache_set(key, result)
    return result


def resample_transit_route(
    legs: list[TransitLeg],
    duration_sec: int,
    step_sec: int = 5,
) -> list[tuple[float, float, str]]:
    """
    Resample per-segment coordinates at step_sec intervals.
    Tags each point with the segment mode: (lat, lon, mode)
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

        # Cumulative distance calculation
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
