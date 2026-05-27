# CounterClock — GPS 엔진 기술 문서

CounterClock은 그룹 약속 및 개인 여정(귀가) 상황에서 각 참가자의 실시간 위치를 추적하고, 배터리 효율을 극대화하는 Adaptive GPS 갱신 주기와 WebSocket 기반 실시간 ETA 브로드캐스트를 제공하는 백엔드 엔진입니다.

---

## 목차

1. [GPS 갱신 주기 최적화 (Cosine Blend Interval)](#1-gps-갱신-주기-최적화-cosine-blend-interval)
2. [ETA 계산 원리](#2-eta-계산-원리)
3. [ETA 시간 차감 근사 (Time-Decay)](#3-eta-시간-차감-근사-time-decay)
4. [비동기 처리](#4-비동기-처리)
5. [전체 데이터 흐름](#5-전체-데이터-흐름)
6. [DB 서버 연동 (Webhook + 캐시)](#6-db-서버-연동-webhook--캐시)
7. [GPS 트리거 — 서버 주도 갱신 주기](#7-gps-트리거--서버-주도-갱신-주기)
8. [출발 알람 API (스프링 연동)](#8-출발-알람-api-스프링-연동)
9. [API 엔드포인트](#9-api-엔드포인트)

---

## 1. GPS 갱신 주기 최적화 (Cosine Blend Interval)

### 핵심 아이디어

> GPS를 항상 켜두면 배터리가 빨리 닳는다. 얼마나 자주 위치를 확인해야 하는지는 **"목적지까지 얼마나 가까운가"** 와 **"약속 시간이 얼마나 촉박한가"** 두 가지 모두에 달려 있다.

시스템은 매번 "다음에 언제 GPS를 다시 켤까?"를 자동으로 계산합니다. **거리 긴급도 + 시간 긴급도**를 Cosine 곡선으로 혼합해 최종 갱신 주기를 결정합니다.

---

### GPS 호출 빈도 조절 원리

#### 1단계 — 두 가지 긴급도 신호 계산

**거리 긴급도 (u_dist)**: 목적지에 가까울수록 1에 가까워집니다.

```
u_dist = 1 - clamp(목적지까지_거리, 0, 3000m) / 3000m

예) 거리 150m  → u_dist = 0.95  (매우 긴급)
    거리 1500m → u_dist = 0.50  (보통)
    거리 3000m+ → u_dist = 0.00  (여유)
```

**시간 긴급도 (u_time)**: ETA가 약속까지 남은 시간에 비해 클수록 1에 가까워집니다.

```
u_time = clamp(ETA(초) / 약속까지_남은_시간(초), 0, 1)

예) ETA 20분, 약속까지 2시간 → u_time = 0.17  (여유)
    ETA 20분, 약속까지 22분   → u_time = 0.91  (촉박)
```

> 시간 정보가 없을 경우 `u_time = u_dist`로 대체됩니다.

---

#### 2단계 — 두 신호를 Cosine 곡선으로 혼합

```
urgency = 0.5 × u_dist + 0.5 × u_time        (기본: 균등 혼합)

next_interval_base = I_min + 0.5 × (I_max - I_min) × (1 + cos(π × urgency))
                   (I_min = 3초,  I_max = 300초)
```

| urgency | cos(π·urgency) | 계산                        | 기본 주기 |
|---------|----------------|-----------------------------|----------|
| 0.0     | +1.0           | 3 + 148.5 × 2.0 = **300초** | 절전      |
| 0.25    | +0.71          | 3 + 148.5 × 1.71 ≈ **257초** | 저빈도   |
| 0.50    | 0.0            | 3 + 148.5 × 1.0 ≈ **152초** | 중간     |
| 0.75    | −0.71          | 3 + 148.5 × 0.29 ≈ **46초** | 고빈도   |
| 1.0     | −1.0           | 3 + 148.5 × 0.0 = **3초**   | 정밀추적 |

기존 이산 계단 함수(3s/10s/30s/60s)와 달리, 상황이 변함에 따라 주기가 **부드럽게 연속적으로** 변화합니다.

---

#### 3단계 — 활동 인식 (Activity Recognition) 배율 적용

최근 위치 이력에서 평균 속도를 추정해 배율을 곱합니다.

기존 이산 계단 방식(0.5 / 8 m/s 경계에서 배율이 뚝 바뀜)은 시속 20 km(≈5.6 m/s) 같은 애매한 속도(자전거·전동킥보드 등)에서 도보 배율이 그대로 적용되는 문제가 있었습니다. 이를 해결하기 위해 **두 개의 sigmoid를 조합한 연속 함수**로 배율을 계산합니다.

```
mult(v) = 3.0 − 2.0 × σ(10 · (v − 0.5)) − 0.7 × σ(2 · (v − 8.0))

σ(x) = 1 / (1 + e^−x)   (표준 sigmoid)
```

- **첫 번째 sigmoid** (중심 0.5 m/s, steepness 10): 정지 → 도보 전환. 경계가 뚜렷해 "멈춤 vs 이동"을 빠르게 구분합니다.
- **두 번째 sigmoid** (중심 8.0 m/s, steepness 2): 도보 → 차량 전환. 완만하게 퍼져 자전거·킥보드 구간이 자연스럽게 처리됩니다.

| 평균 속도 | 대표 이동 수단 | 배율 (이전) | 배율 (sigmoid) |
|----------|--------------|------------|----------------|
| 0 m/s     | 정지          | 3.0        | ≈ 2.99         |
| 1.4 m/s (5 km/h)  | 도보    | 1.0        | ≈ 1.00         |
| 5.6 m/s (20 km/h) | 자전거  | 1.0 (도보 버킷) | ≈ 0.99    |
| 8.0 m/s (경계)    | 빠른 차량 진입 | 0.3 (급변) | ≈ 0.65 (중간값) |
| 10 m/s (36 km/h)  | 차량    | 0.3        | ≈ 0.31         |
| 15+ m/s            | 고속 차량 | 0.3      | ≈ 0.30         |

활동 분류 레이블(stationary / walking / vehicle)은 로깅·상태 표시에만 사용되며, 실제 배율 계산은 이 sigmoid 함수가 담당합니다.

---

#### 4단계 — 유의미한 위치 변화 감지 (Significant Location Change) 배율 적용

| 조건                         | SLC 배율 | 효과                      |
|-----------------------------|---------|---------------------------|
| 500m 이상 이동 (유의미한 변화) | × 1.0   | 계속 적극적으로 추적        |
| 500m 미만 이동 + 정지 상태    | × 2.0   | 배터리 절약 (주기 증가)     |

---

### 최종 계산 공식

```
다음 갱신 주기 = min(next_interval_base × 활동배율 × SLC배율, 300초)
```

**계산 예시:**

| 상황 | u_dist | u_time | urgency | base | sigmoid 배율 | 결과    |
|------|--------|--------|---------|------|-------------|--------|
| 거리 150m, ETA≈약속시간, 차량(15 m/s)  | 0.95 | 0.95 | 0.95 | ~4초  | × 0.30 | **1초** |
| 거리 1500m, ETA 20분/약속 22분, 자전거(5.6 m/s) | 0.50 | 0.91 | 0.71 | ~63초 | × 0.99 | **62초** |
| 거리 1500m, ETA 20분/약속 2시간, 정지(0 m/s) | 0.50 | 0.17 | 0.33 | ~213초 | × 5.97 | **300초** (캡) |

---

### 응답에 포함되는 디버그 정보

`POST /api/optimizer/interval` 응답의 `debug` 필드에서 각 신호를 확인할 수 있습니다.

```json
{
  "next_interval": 12,
  "gps_mode": "BALANCED",
  "debug": {
    "u_dist": 0.834,
    "u_time": 0.712,
    "urgency": 0.773,
    "activity_multiplier": 0.3,
    "slc_multiplier": 1.0
  }
}

---

## 2. ETA 계산 원리

### 거리 계산 — Haversine 공식

지구는 구형이므로 단순 유클리드 거리가 아닌 **구면 삼각법**으로 두 좌표 사이의 실제 거리를 계산합니다.

```
R = 6,371,000m  (지구 반지름)
a = sin²(Δlat/2) + cos(lat1) × cos(lat2) × sin²(Δlon/2)
distance = R × 2 × atan2(√a, √(1-a))
```

카카오 경로 API 키가 있으면 실제 도로 경로 기반 ETA를, 없으면 Haversine 직선 거리와 기본 이동 속도(1.4 m/s)로 ETA를 추정합니다.

---

### 움직임 감지 — Gradient + Momentum

```
gradient = 현재 위치→목적지 거리 - 이전 위치→목적지 거리
```

| gradient 값 | 의미                                |
|-------------|-------------------------------------|
| 음수 (-)    | 목적지에 가까워지는 중 → ETA 단축    |
| 양수 (+)    | 목적지에서 멀어지는 중 → 경로 재계산 |
| 0에 가까움  | 현재 ETA 유지                        |

실제 GPS 신호는 노이즈가 있으므로 **모멘텀**으로 완화합니다:

```
new_velocity = 0.9 × old_velocity + 0.1 × gradient
```

→ 직전 속도 90% + 새 데이터 10%로 급격한 변화를 부드럽게 처리합니다.

---

### Adaptive Threshold (동적 임계값)

ETA 재계산 기준선을 상황에 따라 동적으로 설정합니다.

```
threshold = max(20m, 남은거리 × 5% + |속도| × 1.5)
```

- 남은 거리가 짧을수록 더 민감하게 반응합니다.
- 빠르게 이동할수록 임계값이 커져 불필요한 재계산을 방지합니다.
- 최소 20m를 보장해 목적지 근처에서 과민 반응을 막습니다.

---

### 출발 알람 시각 계산

```
알람 시각 = 약속 시간 - ETA - 개인 버퍼
```

**개인 버퍼**는 사용자의 과거 지각 기록에서 통계적으로 산출합니다:

```
버퍼 = 과거 지각 값의 평균 + z-score × 표준편차
(신뢰도 80% 기본값 → z = 0.84)
```

→ 자주 늦는 사람에게는 자동으로 더 긴 버퍼를 부여합니다. 기록이 없으면 기본 10분을 사용합니다.

---

### 지각 기록 자동 누적

외부 DB는 지각 패턴 데이터를 제공하지 않으므로, **도착 처리(`POST /api/group/<id>/arrive`) 시점에 Flask가 직접 기록을 저장**합니다.

```
POST /api/group/<id>/arrive
    ↓
mark_arrived() 실행
    ├─ scheduled_time  = 약속 시간
    └─ actual_arrival  = 현재 시각
         ↓
    lateness = actual_arrival − scheduled_time
    (양수 = 지각, 음수 = 일찍 도착)
         ↓
    data/latency_{member_id}.json 에 누적 (최대 50건 유지)
```

약속을 사용할수록 버퍼가 점점 개인화됩니다.

| 누적 기록 수 | 버퍼 계산 방식 |
|------------|--------------|
| 0건        | 기본값 10분 사용 |
| 1건        | 해당 지각값과 10분 중 큰 값 |
| 2건 이상    | 평균 + 0.84 × 표준편차 (신뢰도 80%) |

버퍼 우선순위:

```
1순위: DB 멤버 설정의 buffer_minutes (사용자가 앱에서 직접 지정한 경우)
2순위: 자동 누적된 지각 기록 기반 통계값
```

---

### 참가자 상태 분류

| 상태        | 조건                                        |
|------------|---------------------------------------------|
| `arrived`  | 목적지 100m 이내                             |
| `on_time`  | (약속 시간 - 현재 시간) - ETA ≥ 10분        |
| `leave_soon` | 여유 시간 0 ~ 10분                         |
| `hurry`    | ETA가 남은 시간 초과 (늦을 가능성 있음)      |
| `late`     | 약속 시간이 이미 지남                         |

---

## 3. ETA 시간 차감 근사 (Time-Decay)

### 배경 — 기존 구조의 한계

기존에는 앱이 GPS를 전송할 때만 ETA를 재계산했습니다. 앱이 백그라운드 상태이거나 GPS를 보내지 않으면 서버가 보유한 ETA는 마지막 계산 시점에 고정된 채로 오래됩니다.

```
17:00  앱이 GPS 전송 → ETA = 30분 계산
17:10  앱 백그라운드 전환, GPS 전송 없음
17:10  GET /api/group/{id} 조회 → 여전히 ETA = 30분 (10분 전 값)  ← 문제
```

### 해결 — 읽을 때 경과 시간 차감

GPS가 새로 오지 않아도 **응답을 직렬화하는 시점에** 마지막 계산된 ETA에서 경과 시간을 빼 현재 근사값을 반환합니다.

```
decayed_eta = max(0, last_eta_sec − (now − last_updated).total_seconds())
```

```
17:00  앱이 GPS 전송 → eta_sec = 1800 (30분), last_updated = "17:00:00"
17:10  GET /api/group/{id} 조회
       decayed_eta = 1800 − 600 = 1200 (20분)  ← API 호출 없이 근사
       status = _compute_status(1200, ...)       ← 현재 시각 기준 재판정
```

### 설계 원칙

- **저장된 값은 변경하지 않습니다.** `eta_sec`와 `last_updated` 필드는 마지막 실제 계산값 그대로 유지됩니다.
- **읽을 때만 적용됩니다.** `get_group_summary()` 및 `to_dict()` 직렬화 시점에만 decay를 계산합니다.
- **다음 GPS가 오면 정확한 값으로 덮어씁니다.** 실제 계산 결과가 항상 우선합니다.

### 한계

교통 상황 변화(정체, 사고)나 경로 이탈은 반영하지 못합니다. 이 방식은 **"GPS 없이도 ETA가 0으로 굳는 것을 방지"** 하는 1단계 보정이며, 정밀한 재계산은 앱이 GPS를 전송할 때 수행됩니다.

---

## 4. 비동기 처리

### 문제 상황

그룹에 5명이 있을 때 한 명이 위치를 업데이트하면, 서버는 **5명 모두의 ETA를 카카오 API에 요청**해야 합니다.

```
순차 처리: 5명 × 카카오 API 응답 ~1초 = 5초 대기 → 사용자 체감 느림
```

---

### 해결 1 — ThreadPoolExecutor (병렬 ETA 계산)

```python
with ThreadPoolExecutor(max_workers=5) as executor:
    futures = [executor.submit(_fetch_eta, p, ...) for p in participants]
    for f in as_completed(futures):
        f.result()
```

5개의 스레드가 동시에 카카오 API를 호출합니다.

```
순차: ─A──B──C──D──E─  (약 5초)
병렬: ─A─               (약 1초)
      ─B─
      ─C─
      ─D─
      ─E─
```

→ 인원 수와 무관하게 **약 1초** 안에 전체 ETA 계산이 완료됩니다.

---

### 해결 2 — WebSocket 실시간 브로드캐스트 (Flask-SocketIO)

REST API는 요청-응답 방식이라 클라이언트가 주기적으로 서버에 물어봐야 합니다. 이 시스템은 **WebSocket**으로 서버가 먼저 데이터를 밀어 보냅니다.

```
클라이언트                서버
    │                       │
    │── join_group ─────────▶│  그룹 Room 입장
    │                       │
    │                       │  (누군가 위치 업데이트 → 병렬 ETA 재계산)
    │◀── group_update ───────│  그룹 전체에 Push
    │◀── group_update ───────│
    │◀── group_update ───────│
```

```python
# 위치 업데이트 완료 후 그룹 방 전체에 한 번에 전송
socketio.emit("group_update", summary, room=group_id)
```

→ 위치가 바뀌는 순간, 그룹 모두의 화면이 **실시간**으로 갱신됩니다.

---

### async_mode 설정

```python
socketio.init_app(app, cors_allowed_origins="*", async_mode="threading")
```

| 환경         | 설정                          | 특징                              |
|-------------|-------------------------------|----------------------------------|
| 개발         | `async_mode="threading"`      | 별도 설치 없이 바로 사용 가능       |
| 프로덕션     | `eventlet` 워커 (gunicorn)    | 수천 개의 동시 WebSocket 연결 지원  |

---

## 5. 전체 데이터 흐름

```
[DB 서버]
    │
    │  POST /webhook/db-sync  (약속·참가자 생성·변경 시 즉시 Push)
    ▼
[Flask 서버 — 인메모리 캐시]
    │  _appt_cache / _part_cache  (TTL 300초)
    │
    │  (캐시 미스 시만 DB 서버 직접 조회)

[모바일 앱]
    │
    │  POST /api/group/{id}/location
    │  { user_id, current_loc, kakao_api_key }
    ▼
[Flask 서버]
    │
    ├─ 캐시에서 약속·참가자 정보 로드 (DB 호출 없음)
    │
    ├─ ThreadPoolExecutor (병렬)
    │   ├─ 참가자 A → 카카오 API → ETA 계산
    │   ├─ 참가자 B → 카카오 API → ETA 계산
    │   ├─ 참가자 C → 카카오 API → ETA 계산
    │   └─ ... (최대 5개 동시)
    │
    ├─ 상태 분류 (on_time / leave_soon / hurry / arrived)
    ├─ 알람 시각 계산 (약속시간 - ETA - 개인버퍼)
    ├─ 참가자별 다음 GPS 갱신 주기 계산 (Adaptive Interval)
    │
    ├─ socketio.emit("group_update", room=group_id)   → 그룹 전체 ETA 현황
    │
    └─ 참가자별: socketio.emit("request_gps", room="member:{id}")
            │    { group_id, next_interval_sec, gps_mode }
            ▼
    [각 클라이언트 — next_interval_sec 후 GPS 재전송]
```

---

## 6. DB 서버 연동 (Webhook + 캐시)

### 배경

기존 구조는 GPS 처리 시마다 DB 서버에 HTTP 요청을 보내 약속·참가자 정보를 Pull하는 방식이었습니다. 이는 DB 호출 지연이 GPS 처리 지연으로 직결되는 문제가 있었습니다.

### 새 구조 — Push + 캐시

DB 서버가 데이터를 먼저 밀어넣고(Push), GPS 서버는 캐시에서 꺼내 씁니다.

```
이벤트 발생 (약속 생성, 참가자 추가 등)
    │
    DB 서버 → POST /webhook/db-sync
    │
    └─ GPS 서버 캐시에 저장 (TTL 300초)

GPS 처리
    │
    └─ 캐시 히트 → DB 호출 없이 즉시 처리
    └─ 캐시 미스 → DB 서버 직접 조회 (fallback)
```

### Webhook Payload 형식

```json
// 약속 upsert
{ "type": "appointment", "data": { "appointment_id": "...", ... } }

// 참가자 단건 upsert
{ "type": "participant", "data": { "participant_id": "...", "appointment_id": "...", ... } }

// 약속 전체 참가자 목록 교체 (초기 로딩)
{ "type": "participants", "appointment_id": "...", "data": [ {...}, {...} ] }

// 지오펜스 이탈 (출발지 500m 초과 이동 감지)
{
  "type": "geofence_exit",
  "appointment_id": "...",
  "member_id": "...",
  "current_lat": 37.4979,
  "current_lon": 127.0276
}
```

### 지오펜스 이탈 처리 흐름

프런트엔드가 GPS를 전송하면 DB 서버도 해당 위치를 수신합니다. DB 서버는 참가자의 출발지로부터 반경 500m를 벗어나면 `geofence_exit` 이벤트를 GPS 서버로 Push합니다.

```
[프런트엔드] GPS 전송
    ├─▶ [GPS 서버] POST /api/group/<id>/location  (ETA 계산)
    └─▶ [DB 서버]  위치 저장 + 500m 지오펜스 체크
              │
              이탈 감지 시
              │
              └─▶ [GPS 서버] POST /webhook/db-sync { type: "geofence_exit", ... }
                        │
                        ├─ 전체 그룹 ETA 재계산
                        ├─ socketio.emit("group_update", room=appointment_id)
                        │    → 그룹 전체에 "이 참가자 출발!" 알림
                        │
                        └─ socketio.emit("request_gps", { next_interval_sec: 10 }, ...)
                             → 이탈 참가자에게 BALANCED(10초) 주기로 즉시 전환
                             → 나머지 참가자는 각자 Adaptive Interval 유지
```

**2단계 추적 전환 요약**

| 단계 | 조건 | GPS 주기 | 설명 |
|------|------|----------|------|
| Phase 1 | 출발지 500m 이내 | 최대 300초 (LOW) | 이동 없음 — 배터리 절약 |
| Phase 2 | 500m 지오펜스 이탈 | 10초 (BALANCED) → Adaptive | 이동 시작 — 본격 추적 |

---

## 7. GPS 트리거 — 서버 주도 갱신 주기

### 기존 방식의 한계

프런트엔드가 고정된 주기로 GPS를 전송하거나 스스로 주기를 계산하면, 서버가 계산한 **Adaptive Interval**과 싱크가 맞지 않을 수 있습니다.

### 새 방식 — WebSocket `request_gps`

서버가 GPS 처리를 마친 후 각 참가자 개인 room에 `request_gps` 이벤트를 emit합니다. 프런트엔드는 이 이벤트를 받아 지정된 간격(next_interval_sec) 뒤에 GPS를 전송합니다.

```
클라이언트                              서버
    │                                    │
    │── join_member({member_id}) ────────▶│  개인 room 입장
    │── join_group({group_id})  ─────────▶│  그룹 room 입장
    │                                    │
    │  (누군가 위치 업데이트)             │
    │                                    │── 병렬 ETA 계산
    │                                    │── Adaptive Interval 계산
    │                                    │
    │◀── group_update ────────────────────│  그룹 전체 ETA
    │◀── request_gps ─────────────────────│  { next_interval_sec: 18, gps_mode: "BALANCED" }
    │                                    │
    │   setTimeout(sendGPS, 18_000)      │
    │── POST /api/group/<id>/location ───▶│  18초 후 GPS 전송
```

### 프런트엔드 구현 예시 (JavaScript)

```javascript
socket.emit("join_group",  { group_id: "..." });
socket.emit("join_member", { member_id: "..." });

socket.on("request_gps", ({ group_id, next_interval_sec, gps_mode }) => {
    setTimeout(() => {
        navigator.geolocation.getCurrentPosition(pos => {
            fetch(`/api/group/${group_id}/location`, {
                method: "POST",
                body: JSON.stringify({
                    user_id: MY_USER_ID,
                    current_loc: [pos.coords.latitude, pos.coords.longitude],
                }),
            });
        });
    }, next_interval_sec * 1000);
});
```

### GPS 모드별 동작

`next_interval_sec`는 Cosine Blend 알고리즘이 계산한 연속적인 값이며, 모드는 구간으로 분류됩니다.

| gps_mode   | next_interval_sec 범위 | 의미                               |
|------------|------------------------|------------------------------------|
| `HIGH`     | 1 ~ 9초                | urgency 높음 — 정밀 추적 (목적지 근접 또는 시간 촉박) |
| `BALANCED` | 10 ~ 29초              | urgency 중간 — 일반 이동 중         |
| `LOW`      | 30 ~ 300초             | urgency 낮음 — 정지 중 또는 시간 여유 |

클라이언트는 `next_interval_sec`를 그대로 사용하면 됩니다. 서버가 상황에 따라 값을 자동으로 조정하므로 클라이언트 측 별도 로직은 필요 없습니다.

---

## 8. 출발 알람 API (스프링 연동)

스프링 서버가 목표 도착 시간과 현재 위치를 전달하면, 플라스크가 경로 API로 소요 시간을 계산해 **출발 알람 시각**과 **예상 도착 시각**을 반환합니다.

### 계산 공식

```
departure_time       = target_time − duration_sec          (이 시각에 출발해야 도착)
departure_alarm_time = departure_time − preparation_time   (알람은 준비 시간만큼 앞당김)
estimated_arrival    = departure_time + duration_sec       (= target_time)
```

### transport_type별 경로 API

| transport_type | 사용 API | 환경 변수 |
|---------------|----------|-----------|
| `DRIVING`     | 카카오 모빌리티 | `KAKAO_API_KEY` |
| `TRANSIT`     | ODsay 대중교통  | `ODSAY_API_KEY` |

### 엔드포인트 비교

| 엔드포인트 | 용도 | `is_last_mode` |
|-----------|------|----------------|
| `POST /api/personal/departure` | 앱 → 스프링 → 플라스크 (귀가) | O |
| `POST /internal/alarm/journey` | 스프링 내부 → 플라스크 (개인 여정) | O |
| `POST /internal/alarm/appointment` | 스프링 내부 → 플라스크 (그룹 약속) | 없음 |

### 계산 공식 (일반 모드)

```
latency_buffer       = recommended_buffer(member_id)   // 지각 패턴 기반, cold-start 시 10분
total_buffer         = preparation_time + latency_buffer
departure_time       = target_time - duration_sec
departure_alarm_time = departure_time - total_buffer
estimated_arrival    = departure_time + duration_sec
```

`preparation_time`(사용자 준비 시간)과 `latency_buffer`(지각 패턴 보정)를 **분리해서 합산**합니다. 지각 기록이 쌓일수록 `latency_buffer`가 개인화됩니다.

---

### 막차 모드 (`is_last_mode: "yes"`)

`transport_type`이 `TRANSIT`일 때만 사용할 수 있습니다. 해당 날짜의 마지막 대중교통 출발 시각을 자동으로 탐색해, 막차를 놓치지 않도록 알람 시각을 계산합니다.

#### 막차 탐색 원리

ODsay API의 `SearchDate` + `SearchTime` 파라미터를 이용해 **당일 23:00 ~ 익일 01:00** 구간을 이진 탐색합니다. 약 8~10회의 API 호출로 1분 단위 정밀도로 마지막 유효 출발 시각을 찾습니다.

```
23:00 ─────────────────────── 익일 01:00
        이진 탐색 (8~10회)
          └─ 마지막 유효 출발 시각 확정
```

자정을 넘기는 시각(00:00~01:00)은 익일 날짜로 자동 처리되어 ODsay에 올바른 `SearchDate`가 전달됩니다.

#### 알람 시각 결정 규칙

막차 출발 시각과 `target_time` 기준 출발 시각 중 **더 이른 것**을 기준으로 알람을 설정합니다.

| 상황 | 기준 출발 시각 | 이유 |
|------|--------------|------|
| 막차 출발 ≤ target_time 기준 출발 | 막차 출발 시각 | 막차를 타지 않으면 귀가 불가 |
| 막차 출발 > target_time 기준 출발 | target_time 기준 출발 시각 | 목표 시간이 더 촉박함 |

```
departure_alarm_time = min(last_train_departure, normal_departure) − total_buffer
```

#### 에러 케이스

| 조건 | 응답 |
|------|------|
| `transport_type: "DRIVING"`인데 `is_last_mode: "yes"` | `400` — 막차는 TRANSIT 전용 |
| 이미 막차가 지난 경우 | `404` — 유효한 막차 경로 없음 |

---

### Request / Response

```json
// Request (일반 모드)
{
  "current_lat": 37.49796,
  "current_lng": 127.02759,
  "dest_lat": 37.51234,
  "dest_lng": 127.05678,
  "transport_type": "TRANSIT",
  "target_time": "2026-05-25T18:00:00",
  "preparation_time": 10,
  "member_id": "user_001"    // optional — 지각 패턴 개인화
}

// Response (일반 모드)
{
  "departure_alarm_time": "2026-05-25T17:20:00",
  "estimated_arrival": "2026-05-25T18:00:00",
  "latency_buffer_min": 5.2
}

// Request (막차 모드)
{
  "current_lat": 37.49796,
  "current_lng": 127.02759,
  "dest_lat": 37.51234,
  "dest_lng": 127.05678,
  "transport_type": "TRANSIT",
  "is_last_mode": true,
  "target_time": "2026-05-25T23:00:00",  // 날짜 기준으로 활용
  "preparation_time": 10,
  "member_id": "user_001"    // optional
}

// Response (막차 모드 — last_train_departure 필드 추가)
{
  "departure_alarm_time": "2026-05-25T22:15:00",
  "estimated_arrival":    "2026-05-25T23:47:00",
  "latency_buffer_min":   10.0,
  "last_train_departure": "2026-05-25T22:25:00"  // 탐색된 막차 출발 시각 (참고용)
}
```

`member_id` 미전달 시 `latency_buffer_min` = 10.0 (cold-start 기본값).

---

## 9. API 엔드포인트

### REST API

**그룹 약속**

| 메서드   | 경로                           | 설명                                |
|---------|-------------------------------|-------------------------------------|
| `POST`  | `/api/group/create`            | 그룹(약속) 생성                      |
| `POST`  | `/api/group/<id>/join`         | 그룹 참가                            |
| `POST`  | `/api/group/<id>/location`     | 위치 업데이트 + 전체 ETA 재계산      |
| `GET`   | `/api/group/<id>`              | 그룹 현황 조회 (Time-Decay ETA 반영) |
| `POST`  | `/api/group/<id>/arrive`       | 도착 처리                            |
| `DELETE`| `/api/group/<id>/leave`        | 그룹 탈퇴                            |

**개인 여정**

| 메서드   | 경로                              | 설명                                     |
|---------|----------------------------------|------------------------------------------|
| `GET`   | `/api/journey/<id>`              | 여정 조회 (Time-Decay ETA 반영)           |
| `GET`   | `/api/journey/member/<id>`       | 멤버의 여정 목록 조회                     |
| `POST`  | `/api/journey/<id>/location`     | 위치 업데이트 + ETA 재계산                |
| `POST`  | `/api/journey/eta`               | 임시 ETA 계산 (DB 저장 없음)              |

**출발 알람 (스프링 연동)**

| 메서드   | 경로                              | 설명                                     |
|---------|----------------------------------|------------------------------------------|
| `POST`  | `/api/personal/departure`        | 귀가 출발 알람 계산 (is_last_mode 포함)   |
| `POST`  | `/internal/alarm/journey`        | 개인 여정 출발 알람 (스프링 내부 호출)    |
| `POST`  | `/internal/alarm/appointment`    | 그룹 약속 출발 알람 (스프링 내부 호출)    |

**기타**

| 메서드   | 경로                           | 설명                                |
|---------|-------------------------------|-------------------------------------|
| `POST`  | `/api/optimizer/interval`      | GPS 갱신 주기 계산                   |
| `POST`  | `/api/eta/calculate`           | 단일 ETA 계산                        |
| `POST`  | `/api/latency/record`          | 지각 기록 저장                       |
| `GET`   | `/api/latency/buffer`          | 개인 버퍼(출발 여유 시간) 조회        |
| `GET`   | `/health`                      | 서버 상태 확인                       |

### WebSocket 이벤트

| 방향              | 이벤트 이름      | 데이터                                              | 설명                              |
|------------------|-----------------|----------------------------------------------------|------------------------------------|
| 클라이언트 → 서버 | `join_group`    | `{ group_id }`                                     | 그룹 Room 입장                     |
| 서버 → 클라이언트 | `group_update`  | 그룹 전체 ETA 현황 JSON                             | 위치 변경 시 자동 Push             |
| 클라이언트 → 서버 | `join_member`   | `{ member_id }`                                    | 개인 Room 입장 (GPS 트리거 수신용) |
| 서버 → 클라이언트 | `request_gps`   | `{ group_id, next_interval_sec, gps_mode }`        | GPS 신호 전송 요청                 |
| 클라이언트 → 서버 | `join_journey`  | `{ journey_id }`                                   | 개인 여정 Room 입장                |
| 서버 → 클라이언트 | `journey_update`| 개인 ETA JSON                                      | 개인 여정 ETA 실시간 갱신          |

### Webhook

| 메서드  | 경로                  | 설명                                                                         |
|--------|-----------------------|------------------------------------------------------------------------------|
| `POST` | `/webhook/db-sync`    | DB 서버 Push 수신 (`appointment` / `participant` / `participants` / `geofence_exit`) |

---

## 실행 방법

### 개발 서버

```bash
python -m gps_api.wsgi
```

### 프로덕션 (eventlet)

```bash
gunicorn "gps_api.wsgi:app" --worker-class eventlet --workers 1 --bind 0.0.0.0:5000
```

### 환경 변수

| 변수명            | 설명                                      | 기본값  |
|-----------------|------------------------------------------|--------|
| `KAKAO_API_KEY` | 카카오 모빌리티 REST API 키 (DRIVING 경로) | 없음   |
| `ODSAY_API_KEY` | ODsay 대중교통 API 키 (TRANSIT 경로)      | 없음   |
| `DB_BASE_URL`   | 외부 DB 서버 URL (미설정 시 인메모리)      | 없음   |
| `PORT`          | 서버 포트                                 | 5000   |
| `DEBUG`         | 디버그 모드                               | False  |
