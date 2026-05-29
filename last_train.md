# 막차 탐색 시스템 설계 문서

---

## 목차

1. [전체 아키텍처 설계](#1-전체-아키텍처-설계)
2. [추천 알고리즘](#2-추천-알고리즘--coarse-scan--local-refinement)
3. [Coarse Scan + Local Binary Search 설계](#3-coarse-scan--local-binary-search-설계)
4. [Valid Path 판정 기준](#4-valid-path-판정-기준)
5. [API Retry / Cache 전략](#5-api-retry--cache-전략)
6. [Datetime 처리 전략](#6-datetime-처리-전략)
7. [Python Production-Level 코드](#7-python-production-level-코드)
8. [Flask 구조 적용](#8-flask-구조-적용)
9. [테스트 전략](#9-테스트-전략)
10. [시간복잡도 및 API 호출량 분석](#10-시간복잡도-및-api-호출량-분석)

---

## 1. 전체 아키텍처 설계

```
[alarm.py]
    │
    ├─ is_last_mode=False → 기존 단순 경로 조회
    │
    └─ is_last_mode=True
            │
            ▼
    [last_train.py]  ← 신규 모듈로 분리
            │
            ├─ 1단계: Coarse Scan (10분 간격, 13회)
            │         23:00 ~ 01:00
            │
            ├─ 2단계: Local Refinement (이진 탐색, ~4회)
            │         마지막 valid 구간 1분 단위 정밀화
            │
            ├─ Valid Path Filter
            │         도보비율 / 환승 / 소요시간
            │
            ├─ API Retry (네트워크 오류 vs 경로 없음 구분)
            │
            └─ PathInfo 반환 → alarm.py에서 알람 계산
```

### 엔드포인트

`POST /internal/alarm/journey` 요청에 `"is_last_mode": true`를 포함하면 막차 모드로 동작합니다. 별도 엔드포인트 없이 기존 journey 알람 엔드포인트를 그대로 사용합니다.

### 관련 파일

| 파일 | 역할 |
|------|------|
| `gps_api/routes/alarm.py` | 요청 파싱 및 알람 시각 계산 |
| `gps_api/core/last_train.py` | 막차 탐색 핵심 로직 (신규) |
| `gps_api/core/transit_route.py` | ODsay API 연동 (`search_dt` 파라미터 추가) |

---

## 2. 추천 알고리즘 — Coarse Scan + Local Refinement

### 순수 이진 탐색의 한계

순수 이진 탐색은 **단조성(monotonicity) 가정**에 의존합니다.

> "23:30에 경로가 없으면 그 이후에도 없다"

그러나 대중교통은 열차 배차 간격 때문에 이 가정이 깨집니다.

```
23:40 불가능
23:45 가능      ← 이진 탐색이면 23:40에서 "없음"으로 판정 후 앞으로만 탐색 → 23:45 놓침
23:50 불가능
```

### 해결 방법

```
1단계: 10분 간격으로 전체 구간(23:00 ~ 01:00)을 스캔 → 유효한 구간 목록 확보
2단계: 목록의 마지막 유효 시각 이후 구간에서 이진 탐색으로 1분 단위 정밀화
```

비단조 구간도 coarse scan이 모두 커버하므로 누락이 없습니다.

---

## 3. Coarse Scan + Local Binary Search 설계

### 1단계: Coarse Scan

```
23:00  23:10  23:20  23:30  23:40  23:50  00:00  00:10  ...  01:00
  O      O      O      O      X      O      O      X          X
                                           ↑
                              마지막 valid (00:00)
                              └─ [00:00 ~ 00:10] 구간에서 이진 탐색
```

- 10분 간격으로 23:00 ~ 익일 01:00를 순회 (최대 13회 API 호출)
- 유효한 경로(`O`)가 있는 시각 목록 수집
- 마지막 유효 시각(`last_valid_dt`) 확정

### 2단계: Local Refinement (이진 탐색)

```
[00:00 ─────────────────── 00:10]  (coarse 한 구간 = 10분)
        ↓ 이진 탐색
  00:05 → O → best=00:05, lo=00:06
  00:08 → O → best=00:08, lo=00:09
  00:09 → X → hi=00:08
  범위 1분 이하 → 종료, 막차 = 00:08
```

- `last_valid_dt` ~ `last_valid_dt + 10분` 구간에서 이진 탐색
- 1분 단위 정밀도로 마지막 유효 출발 시각 확정 (최대 4회 API 호출)

---

## 4. Valid Path 판정 기준

단순히 "ODsay가 경로를 반환했는가"만으로는 부족합니다. 아래 기준을 모두 통과해야 유효한 막차 경로로 인정합니다.

| 기준 | 임계값 | 이유 |
|------|--------|------|
| 도보 비율 | 총 소요시간의 50% 이하 | 도보 중심 경로 제외 |
| 환승 횟수 | 4회 이하 | 비현실적인 복잡 경로 제외 |
| 총 소요시간 | 180분 이하 | 비정상적으로 긴 경로 제외 |
| 대중교통 구간 수 | 최소 1개 | 전부 도보인 경로 제외 |

### 예외 분류

| 예외 클래스 | 발생 조건 | 처리 방법 |
|------------|----------|----------|
| `ODsayNoRouteError` | ODsay가 경로 없음 코드 반환 (`-98`, `-11`) | 재시도 없이 `None` |
| `ODsayInvalidPathError` | 경로는 있으나 판정 기준 미달 | 재시도 없이 `None` |
| `ODsayNetworkError` | 5xx, 타임아웃, 연결 오류 | Exponential backoff 재시도 |

---

## 5. API Retry / Cache 전략

### Retry — 오류 유형별 처리

```
ODsay 응답
  ├─ error.code == -98 / -11 / -8  → ODsayNoRouteError  → 재시도 없음
  ├─ error.code 그 외              → ODsayNetworkError  → 재시도
  ├─ HTTP 5xx                      → ODsayNetworkError  → 재시도
  ├─ Timeout / ConnectionError     → ODsayNetworkError  → 재시도
  └─ 정상 응답이나 판정 기준 미달  → ODsayInvalidPathError → 재시도 없음
```

**Exponential Backoff**: 1초 → 2초 → 4초, 최대 3회

네트워크 장애는 `None`을 반환하되 **경고 로그만** 남기고, "막차 없음"으로 단정하지 않습니다.

### Cache — 날짜 단위 캐싱

막차 시각은 날짜·출발지·목적지가 같으면 동일하므로, 하루 단위로 캐싱합니다.

```python
캐시 키: (origin_lat_2dp, origin_lon_2dp, dest_lat_2dp, dest_lon_2dp, date)
```

좌표를 소수점 2자리로 반올림(약 1km 오차 허용)해 동일 생활권 요청을 캐시 히트시킵니다.

---

## 6. Datetime 처리 전략

모든 내부 처리는 `datetime` 객체로 수행하고, ODsay API 호출 직전에만 문자열로 변환합니다.

```python
# 자정 넘김 → datetime이 자동으로 익일 처리
search_dt = datetime(2026, 5, 25, 23, 0) + timedelta(hours=1, minutes=30)
# → 2026-05-26 00:30:00

params["SearchDate"] = search_dt.strftime("%Y%m%d")  # "20260526"
params["SearchTime"] = search_dt.strftime("%H%M")    # "0030"
```

수동으로 날짜를 올려주는 처리가 필요 없고, 자정 경계에서 버그가 발생하지 않습니다.

---

## 7. Python Production-Level 코드

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
    """타임아웃·5xx 등 재시도 가능한 오류"""

class ODsayNoRouteError(Exception):
    """경로 없음 — 재시도 불필요"""

class ODsayInvalidPathError(Exception):
    """경로는 있으나 유효하지 않음"""


@dataclass
class PathInfo:
    departure_dt: datetime
    arrival_dt:   datetime
    duration_sec: int
    walk_ratio:   float
    transfers:    int


# 날짜별 인메모리 캐시
_last_train_cache: dict[tuple, Optional[PathInfo]] = {}


def _cache_key(olat, olon, dlat, dlon, date) -> tuple:
    return (round(olat, 2), round(olon, 2), round(dlat, 2), round(dlon, 2), str(date))


def _call_odsay(olat, olon, dlat, dlon, api_key, search_dt: datetime) -> dict:
    """ODsay API 호출. 네트워크 오류와 경로 없음을 예외로 구분."""
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
                raise ODsayNoRouteError("응답 path 없음")
            return paths[0]

        except ODsayNoRouteError:
            raise
        except (requests.Timeout, requests.ConnectionError, ODsayNetworkError) as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))

    raise ODsayNetworkError(f"{MAX_RETRIES}회 실패: {last_exc}")


def _validate(path: dict, search_dt: datetime) -> PathInfo:
    """유효성 검증. 통과하지 못하면 ODsayInvalidPathError."""
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
        raise ODsayInvalidPathError(f"소요시간 {total_min}분 초과")
    if walk_ratio > MAX_WALK_RATIO:
        raise ODsayInvalidPathError(f"도보비율 {walk_ratio:.0%} 초과")
    if transfers > MAX_TRANSFERS:
        raise ODsayInvalidPathError(f"환승 {transfers}회 초과")
    if not transit_legs:
        raise ODsayInvalidPathError("대중교통 구간 없음")

    return PathInfo(
        departure_dt=search_dt,
        arrival_dt=search_dt + timedelta(seconds=total_sec),
        duration_sec=total_sec,
        walk_ratio=walk_ratio,
        transfers=transfers,
    )


def _probe(olat, olon, dlat, dlon, api_key, dt: datetime) -> Optional[PathInfo]:
    """단일 시각 탐색. 네트워크 장애는 None + 경고 로그."""
    try:
        path = _call_odsay(olat, olon, dlat, dlon, api_key, dt)
        return _validate(path, dt)
    except ODsayNoRouteError:
        return None
    except ODsayInvalidPathError as e:
        logger.debug("[last_train] 유효하지 않은 경로 %s: %s", dt.strftime("%H:%M"), e)
        return None
    except ODsayNetworkError as e:
        logger.warning("[last_train] API 장애 %s: %s", dt.strftime("%H:%M"), e)
        return None  # 장애를 "경로 없음"으로 단정하지 않음


def find_last_train(
    olat: float, olon: float,
    dlat: float, dlon: float,
    api_key: str,
    base_dt: datetime,
) -> Optional[PathInfo]:
    """
    Coarse scan (10분 간격) + Local refinement (이진 탐색) 으로
    당일 23:00 ~ 익일 01:00 사이의 마지막 유효 출발 시각을 반환.
    결과는 날짜 단위로 캐싱.
    """
    key = _cache_key(olat, olon, dlat, dlon, base_dt.date())
    if key in _last_train_cache:
        logger.debug("[last_train] 캐시 히트 %s", key)
        return _last_train_cache[key]

    base_date  = base_dt.date()
    scan_start = datetime(base_date.year, base_date.month, base_date.day, 23, 0)
    scan_end   = scan_start + timedelta(hours=2)

    # 1단계: Coarse scan
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

    # 2단계: Local refinement
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

## 8. Flask 구조 적용

`alarm.py`의 `_compute_alarm()`에서 `is_last_mode=True`일 때 아래와 같이 호출합니다.

```python
from gps_api.core.last_train import find_last_train

if is_last_mode:
    odsay_key = current_app.config.get("ODSAY_API_KEY", "")
    info = find_last_train(
        current_lat, current_lng, dest_lat, dest_lng,
        odsay_key, target_time,
    )
    if info is None:
        abort(404, description="막차 경로를 찾을 수 없습니다.")

    # 일반 모드 기준 출발 시각
    try:
        normal_sec = _get_duration(
            current_lat, current_lng, dest_lat, dest_lng,
            transport_type, current_app.config,
        )
    except Exception:
        normal_sec = info.duration_sec
    normal_departure = target_time - timedelta(seconds=normal_sec)

    # 더 이른 출발 기준으로 알람 설정
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

### 에러 케이스

| 조건 | HTTP 코드 | 메시지 |
|------|-----------|--------|
| `transport_type`이 DRIVING인데 `is_last_mode: true` | 400 | 막차는 TRANSIT 전용 |
| 탐색 범위 내 유효한 막차 없음 | 404 | 막차 경로를 찾을 수 없습니다 |

---

## 9. 테스트 전략

### 단위 테스트 — `_validate`

```python
def test_validate_rejects_high_walk_ratio():
    path = make_path(total_min=60, walk_sec=35*60, transit_legs=1)
    with pytest.raises(ODsayInvalidPathError, match="도보비율"):
        _validate(path, datetime.now())

def test_validate_rejects_excessive_transfers():
    path = make_path(total_min=90, walk_sec=10*60, transit_legs=6)
    with pytest.raises(ODsayInvalidPathError, match="환승"):
        _validate(path, datetime.now())

def test_validate_rejects_no_transit_legs():
    path = make_path(total_min=30, walk_sec=20*60, transit_legs=0)
    with pytest.raises(ODsayInvalidPathError, match="대중교통 구간 없음"):
        _validate(path, datetime.now())
```

### 단위 테스트 — `find_last_train`

```python
def test_nonmonotonic_route_not_missed(monkeypatch):
    # 23:50 가능, 00:00 불가, 23:55 가능 → coarse scan이 23:50을 포착해야 함
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
    # API 장애 시 None 반환, 예외 전파 안 함
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
    find_last_train(...)  # 두 번째 호출 — 캐시 히트
    assert call_count["n"] == first_count  # API 추가 호출 없음
```

### 통합 테스트

실제 ODsay API를 사용하는 테스트는 `@pytest.mark.integration`으로 마킹해 CI에서 분리 실행합니다.

```python
@pytest.mark.integration
def test_real_last_train_seoul():
    result = find_last_train(
        olat=37.4979, olon=127.0276,  # 강남역
        dlat=37.5665, dlon=126.9780,  # 서울역
        api_key=os.environ["ODSAY_API_KEY"],
        base_dt=datetime.now(),
    )
    # 실제 막차가 존재해야 함 (낮 시간대 테스트)
    assert result is not None
    assert result.duration_sec > 0
    assert result.walk_ratio <= MAX_WALK_RATIO
```

---

## 10. 시간복잡도 및 API 호출량 분석

| 단계 | 호출 수 | 계산 근거 |
|------|---------|----------|
| Coarse scan | 13회 | 120분 ÷ 10분 + 1 |
| Local refinement | 4회 | log₂(10분) ≈ 3.3 → 올림 |
| **합계 (최대)** | **17회** | |
| 캐시 히트 시 | **0회** | 동일 날짜·구간 재요청 |
| 재시도 포함 최악 | **~51회** | 17회 × 최대 3회 재시도 |

### 기존 순수 이진 탐색과 비교

| 방식 | 호출 수 | 비단조 구간 처리 | 캐시 |
|------|---------|----------------|------|
| 순수 이진 탐색 | ~10회 | 불가 (놓칠 수 있음) | 없음 |
| Coarse + Refinement | ~17회 | 가능 | 있음 |

호출 수가 약 7회 증가하지만, **비단조 구간 누락 방지**와 **캐시로 인한 실질적 0회 호출** 효과가 트레이드오프를 상쇄합니다.
