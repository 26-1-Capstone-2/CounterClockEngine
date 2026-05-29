# Last Train Search System Design Document

---

## Table of Contents

1. [Overall Architecture Design](#1-overall-architecture-design)
2. [Recommended Algorithm](#2-recommended-algorithm--coarse-scan--local-refinement)
3. [Coarse Scan + Local Binary Search Design](#3-coarse-scan--local-binary-search-design)
4. [Valid Path Criteria](#4-valid-path-criteria)
5. [API Retry / Cache Strategy](#5-api-retry--cache-strategy)
6. [Datetime Handling Strategy](#6-datetime-handling-strategy)
7. [Python Production-Level Code](#7-python-production-level-code)
8. [Flask Structure Integration](#8-flask-structure-integration)
9. [Testing Strategy](#9-testing-strategy)
10. [Time Complexity and API Call Volume Analysis](#10-time-complexity-and-api-call-volume-analysis)

---

## 1. Overall Architecture Design

```
[alarm.py]
    │
    ├─ is_last_mode=False → existing simple route lookup
    │
    └─ is_last_mode=True
            │
            ▼
    [last_train.py]  ← separated as a new module
            │
            ├─ Stage 1: Coarse Scan (10-minute intervals, 13 calls)
            │         23:00 ~ 01:00
            │
            ├─ Stage 2: Local Refinement (binary search, ~4 calls)
            │         Refine last valid range to 1-minute precision
            │
            ├─ Valid Path Filter
            │         walk ratio / transfers / total time
            │
            ├─ API Retry (distinguish network error vs no route)
            │
            └─ Return PathInfo → alarm calculation in alarm.py
```

### Endpoint

Include `"is_last_mode": true` in a `POST /internal/alarm/journey` request to activate last train mode. No separate endpoint — the existing journey alarm endpoint is used as-is.

### Related Files

| File | Role |
|------|------|
| `gps_api/routes/alarm.py` | Request parsing and alarm time calculation |
| `gps_api/core/last_train.py` | Last train search core logic (new) |
| `gps_api/core/transit_route.py` | ODsay API integration (`search_dt` parameter added) |

---

## 2. Recommended Algorithm — Coarse Scan + Local Refinement

### Limitations of Pure Binary Search

Pure binary search relies on the **monotonicity assumption**.

> "If there is no route at 23:30, there won't be one after that either"

However, this assumption breaks down for public transit due to train scheduling intervals.

```
23:40 not available
23:45 available      ← binary search would judge "no route" at 23:40 and only search backwards → misses 23:45
23:50 not available
```

### Solution

```
Stage 1: Scan the entire range (23:00 ~ 01:00) at 10-minute intervals → obtain list of valid time slots
Stage 2: Binary search in the range after the last valid time to refine to 1-minute precision
```

Since the coarse scan covers all non-monotonic ranges, nothing is missed.

---

## 3. Coarse Scan + Local Binary Search Design

### Stage 1: Coarse Scan

```
23:00  23:10  23:20  23:30  23:40  23:50  00:00  00:10  ...  01:00
  O      O      O      O      X      O      O      X          X
                                           ↑
                              Last valid (00:00)
                              └─ Binary search in [00:00 ~ 00:10] range
```

- Iterate over 23:00 ~ next day 01:00 at 10-minute intervals (max 13 API calls)
- Collect list of times with valid routes (`O`)
- Determine last valid time (`last_valid_dt`)

### Stage 2: Local Refinement (Binary Search)

```
[00:00 ─────────────────── 00:10]  (one coarse interval = 10 minutes)
        ↓ binary search
  00:05 → O → best=00:05, lo=00:06
  00:08 → O → best=00:08, lo=00:09
  00:09 → X → hi=00:08
  range ≤ 1 minute → stop, last train = 00:08
```

- Binary search in `last_valid_dt` ~ `last_valid_dt + 10 minutes` range
- Determine last valid departure time with 1-minute precision (max 4 API calls)

---

## 4. Valid Path Criteria

Simply checking "did ODsay return a route?" is insufficient. A route must pass all the following criteria to be considered a valid last train route.

| Criterion | Threshold | Reason |
|-----------|-----------|--------|
| Walk ratio | 50% or less of total travel time | Exclude walking-heavy routes |
| Transfers | 4 or fewer | Exclude unrealistically complex routes |
| Total travel time | 180 minutes or less | Exclude abnormally long routes |
| Transit segment count | Minimum 1 | Exclude all-walking routes |

### Exception Classification

| Exception Class | Trigger Condition | Handling |
|----------------|-------------------|----------|
| `ODsayNoRouteError` | ODsay returns no-route code (`-98`, `-11`) | Return `None` without retry |
| `ODsayInvalidPathError` | Route exists but fails validation criteria | Return `None` without retry |
| `ODsayNetworkError` | 5xx, timeout, connection error | Retry with exponential backoff |

---

## 5. API Retry / Cache Strategy

### Retry — Handling by Error Type

```
ODsay response
  ├─ error.code == -98 / -11 / -8  → ODsayNoRouteError  → no retry
  ├─ error.code other              → ODsayNetworkError  → retry
  ├─ HTTP 5xx                      → ODsayNetworkError  → retry
  ├─ Timeout / ConnectionError     → ODsayNetworkError  → retry
  └─ normal response but fails validation criteria → ODsayInvalidPathError → no retry
```

**Exponential Backoff**: 1s → 2s → 4s, max 3 retries

Network failures return `None` with **warning log only** and are not treated as "no last train".

### Cache — Daily Caching

Since the last train time is the same for the same date, origin, and destination, caching is done per day.

```python
Cache key: (origin_lat_2dp, origin_lon_2dp, dest_lat_2dp, dest_lon_2dp, date)
```

Coordinates are rounded to 2 decimal places (allowing ~1km error) to achieve cache hits for requests from the same neighborhood.

---

## 6. Datetime Handling Strategy

All internal processing uses `datetime` objects, converting to strings only immediately before ODsay API calls.

```python
# Past midnight → datetime automatically handles next-day processing
search_dt = datetime(2026, 5, 25, 23, 0) + timedelta(hours=1, minutes=30)
# → 2026-05-26 00:30:00

params["SearchDate"] = search_dt.strftime("%Y%m%d")  # "20260526"
params["SearchTime"] = search_dt.strftime("%H%M")    # "0030"
```

No manual date rollover is needed, and no bugs occur at the midnight boundary.

---

## 7. Python Production-Level Code

**`gps_api/core/last_train.py`**

```python
import time
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional
import requests

logger = logging.getLogger(__name__)

ODSAY_BASE_URL  = "https://api.odsay.com/v1/api"
COARSE_STEP_MIN = 10
MAX_RETRIES     = 3
RETRY_BACKOFF   = 1.0

MAX_WALK_RATIO  = 0.5
MAX_TRANSFERS   = 4
MAX_TOTAL_MIN   = 180


class ODsayNetworkError(Exception):
    """Retryable error — timeout, 5xx, etc."""

class ODsayNoRouteError(Exception):
    """No route — no retry needed"""

class ODsayInvalidPathError(Exception):
    """Route exists but is not valid"""


@dataclass
class PathInfo:
    departure_dt: datetime
    arrival_dt:   datetime
    duration_sec: int
    walk_ratio:   float
    transfers:    int


# Per-day in-memory cache
_last_train_cache: dict[tuple, Optional[PathInfo]] = {}


def _cache_key(olat, olon, dlat, dlon, date) -> tuple:
    return (round(olat, 2), round(olon, 2), round(dlat, 2), round(dlon, 2), str(date))


def _call_odsay(olat, olon, dlat, dlon, api_key, search_dt: datetime) -> dict:
    """ODsay API call. Distinguishes network errors from no-route as exceptions."""
    params = {
        "apiKey": api_key,
        "SX": olon, "SY": olat,
        "EX": dlon, "EY": dlat,
        "OPT": 0, "SearchType": 0, "lang": 0,
        "SearchDate": search_dt.strftime("%Y%m%d"),
        "SearchTime": search_dt.strftime("%H%M"),
    }
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(
                f"{ODSAY_BASE_URL}/searchPubTransPathT",
                params=params, timeout=10,
            )
            if resp.status_code >= 500:
                raise ODsayNetworkError(f"5xx: {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                code = data["error"].get("code", -1)
                msg  = data["error"].get("message", "")
                if code in (-98, -11, -8):
                    raise ODsayNoRouteError(msg)
                raise ODsayNetworkError(f"ODsay {code}: {msg}")

            paths = (data.get("result") or {}).get("path", [])
            if not paths:
                raise ODsayNoRouteError("No paths in response")
            return paths[0]

        except ODsayNoRouteError:
            raise
        except (requests.Timeout, requests.ConnectionError, ODsayNetworkError) as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))

    raise ODsayNetworkError(f"Failed after {MAX_RETRIES} retries: {last_exc}")


def _validate(path: dict, search_dt: datetime) -> PathInfo:
    """Validation. Raises ODsayInvalidPathError if criteria not met."""
    info       = path.get("info", {})
    total_min  = info.get("totalTime", 0)
    sub_paths  = path.get("subPath", [])

    walk_sec     = sum(s.get("sectionTime", 0) * 60
                       for s in sub_paths if s.get("trafficType") == 3)
    total_sec    = total_min * 60
    transit_legs = [s for s in sub_paths if s.get("trafficType") in (1, 2)]
    transfers    = max(0, len(transit_legs) - 1)
    walk_ratio   = walk_sec / total_sec if total_sec > 0 else 1.0

    if total_min > MAX_TOTAL_MIN:
        raise ODsayInvalidPathError(f"Total time {total_min} min exceeded")
    if walk_ratio > MAX_WALK_RATIO:
        raise ODsayInvalidPathError(f"Walk ratio {walk_ratio:.0%} exceeded")
    if transfers > MAX_TRANSFERS:
        raise ODsayInvalidPathError(f"Transfers {transfers} exceeded")
    if not transit_legs:
        raise ODsayInvalidPathError("No transit segments")

    return PathInfo(
        departure_dt=search_dt,
        arrival_dt=search_dt + timedelta(seconds=total_sec),
        duration_sec=total_sec,
        walk_ratio=walk_ratio,
        transfers=transfers,
    )


def _probe(olat, olon, dlat, dlon, api_key, dt: datetime) -> Optional[PathInfo]:
    """Single time probe. Network failures return None + warning log."""
    try:
        path = _call_odsay(olat, olon, dlat, dlon, api_key, dt)
        return _validate(path, dt)
    except ODsayNoRouteError:
        return None
    except ODsayInvalidPathError as e:
        logger.debug("[last_train] Invalid path %s: %s", dt.strftime("%H:%M"), e)
        return None
    except ODsayNetworkError as e:
        logger.warning("[last_train] API failure %s: %s", dt.strftime("%H:%M"), e)
        return None  # Do not conclude failure as "no route"


def find_last_train(
    olat: float, olon: float,
    dlat: float, dlon: float,
    api_key: str,
    base_dt: datetime,
) -> Optional[PathInfo]:
    """
    Returns the last valid departure time between 23:00 and 01:00 the following day
    using Coarse scan (10-minute intervals) + Local refinement (binary search).
    Results are cached per day.
    """
    key = _cache_key(olat, olon, dlat, dlon, base_dt.date())
    if key in _last_train_cache:
        logger.debug("[last_train] Cache hit %s", key)
        return _last_train_cache[key]

    base_date  = base_dt.date()
    scan_start = datetime(base_date.year, base_date.month, base_date.day, 23, 0)
    scan_end   = scan_start + timedelta(hours=2)

    # Stage 1: Coarse scan
    coarse: list[tuple[datetime, Optional[PathInfo]]] = []
    t = scan_start
    while t <= scan_end:
        coarse.append((t, _probe(olat, olon, dlat, dlon, api_key, t)))
        t += timedelta(minutes=COARSE_STEP_MIN)

    valid_points = [(dt, r) for dt, r in coarse if r is not None]
    if not valid_points:
        _last_train_cache[key] = None
        return None

    last_valid_dt, best = valid_points[-1]

    # Stage 2: Local refinement
    lo = last_valid_dt
    hi = min(last_valid_dt + timedelta(minutes=COARSE_STEP_MIN), scan_end)

    while (hi - lo).total_seconds() > 60:
        mid    = lo + (hi - lo) / 2
        result = _probe(olat, olon, dlat, dlon, api_key, mid)
        if result is not None:
            best = result
            lo   = mid + timedelta(minutes=1)
        else:
            hi   = mid - timedelta(minutes=1)

    _last_train_cache[key] = best
    return best
```

---

## 8. Flask Structure Integration

In `alarm.py`'s `_compute_alarm()`, call as follows when `is_last_mode=True`.

```python
from gps_api.core.last_train import find_last_train

if is_last_mode:
    odsay_key = current_app.config.get("ODSAY_API_KEY", "")
    info = find_last_train(
        current_lat, current_lng, dest_lat, dest_lng,
        odsay_key, target_time,
    )
    if info is None:
        abort(404, description="No last train route found.")

    # Normal mode reference departure time
    try:
        normal_sec = _get_duration(
            current_lat, current_lng, dest_lat, dest_lng,
            transport_type, current_app.config,
        )
    except Exception:
        normal_sec = info.duration_sec
    normal_departure = target_time - timedelta(seconds=normal_sec)

    # Set alarm based on earlier departure
    if info.departure_dt <= normal_departure:
        eff_departure = info.departure_dt
        eff_arrival   = info.arrival_dt
    else:
        eff_departure = normal_departure
        eff_arrival   = target_time

    alarm_time = eff_departure - timedelta(minutes=total_buffer_min)
    return {
        "departure_alarm_time": alarm_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "estimated_arrival":    eff_arrival.strftime("%Y-%m-%dT%H:%M:%S"),
        "latency_buffer_min":   round(latency_buffer_min, 1),
        "last_train_departure": info.departure_dt.strftime("%Y-%m-%dT%H:%M:%S"),
    }
```

### Request / Response

```json
// Request
{
  "current_lat": 37.49796,
  "current_lng": 127.02759,
  "dest_lat": 37.51234,
  "dest_lng": 127.05678,
  "transport_type": "TRANSIT",
  "is_last_mode": true,
  "target_time": "2026-05-25T23:00:00",
  "preparation_time": 10,
  "member_id": "user_001"
}

// Response
{
  "departure_alarm_time": "2026-05-25T22:15:00",
  "estimated_arrival":    "2026-05-25T23:47:00",
  "latency_buffer_min":   10.0,
  "last_train_departure": "2026-05-25T22:25:00"
}
```

### Error Cases

| Condition | HTTP Code | Message |
|-----------|-----------|---------|
| `transport_type` is DRIVING but `is_last_mode: true` | 400 | Last train is TRANSIT only |
| No valid last train within search range | 404 | No last train route found |

---

## 9. Testing Strategy

### Unit Tests — `_validate`

```python
def test_validate_rejects_high_walk_ratio():
    path = make_path(total_min=60, walk_sec=35*60, transit_legs=1)
    with pytest.raises(ODsayInvalidPathError, match="walk ratio"):
        _validate(path, datetime.now())

def test_validate_rejects_excessive_transfers():
    path = make_path(total_min=90, walk_sec=10*60, transit_legs=6)
    with pytest.raises(ODsayInvalidPathError, match="transfers"):
        _validate(path, datetime.now())

def test_validate_rejects_no_transit_legs():
    path = make_path(total_min=30, walk_sec=20*60, transit_legs=0)
    with pytest.raises(ODsayInvalidPathError, match="No transit segments"):
        _validate(path, datetime.now())
```

### Unit Tests — `find_last_train`

```python
def test_nonmonotonic_route_not_missed(monkeypatch):
    # 23:50 available, 00:00 unavailable, 23:55 available → coarse scan must capture 23:50
    schedule = {
        "2330": True, "2340": False, "2350": True, "0000": False,
    }
    monkeypatch.setattr("gps_api.core.last_train._probe", mock_probe(schedule))
    result = find_last_train(...)
    assert result is not None
    assert result.departure_dt.strftime("%H%M") >= "2350"

def test_returns_none_when_no_valid_route(monkeypatch):
    monkeypatch.setattr("gps_api.core.last_train._probe", lambda *a, **k: None)
    assert find_last_train(...) is None

def test_network_error_does_not_raise(monkeypatch):
    # API failure should return None, not propagate exception
    monkeypatch.setattr("gps_api.core.last_train._call_odsay",
                        lambda *a, **k: (_ for _ in ()).throw(ODsayNetworkError("timeout")))
    result = find_last_train(...)
    assert result is None

def test_cache_hit_skips_api(monkeypatch):
    call_count = {"n": 0}
    original_probe = _probe
    def counting_probe(*args, **kwargs):
        call_count["n"] += 1
        return original_probe(*args, **kwargs)
    monkeypatch.setattr("gps_api.core.last_train._probe", counting_probe)

    find_last_train(...)
    first_count = call_count["n"]
    find_last_train(...)  # second call — cache hit
    assert call_count["n"] == first_count  # no additional API calls
```

### Integration Tests

Tests using the real ODsay API are marked with `@pytest.mark.integration` and run separately from CI.

```python
@pytest.mark.integration
def test_real_last_train_seoul():
    result = find_last_train(
        olat=37.4979, olon=127.0276,  # Gangnam Station
        dlat=37.5665, dlon=126.9780,  # Seoul Station
        api_key=os.environ["ODSAY_API_KEY"],
        base_dt=datetime.now(),
    )
    # Last train should exist (daytime test)
    assert result is not None
    assert result.duration_sec > 0
    assert result.walk_ratio <= MAX_WALK_RATIO
```

---

## 10. Time Complexity and API Call Volume Analysis

| Stage | Call count | Calculation basis |
|-------|-----------|-------------------|
| Coarse scan | 13 calls | 120 min ÷ 10 min + 1 |
| Local refinement | 4 calls | log₂(10 min) ≈ 3.3 → rounded up |
| **Total (maximum)** | **17 calls** | |
| On cache hit | **0 calls** | Same date/range re-request |
| Worst case with retries | **~51 calls** | 17 calls × max 3 retries |

### Comparison with Pure Binary Search

| Method | Call count | Non-monotonic range handling | Cache |
|--------|-----------|------------------------------|-------|
| Pure binary search | ~10 calls | Not possible (may miss routes) | None |
| Coarse + Refinement | ~17 calls | Possible | Yes |

Although call count increases by about 7, the **non-monotonic range miss prevention** and **effectively 0 calls due to cache** more than offset this trade-off.
