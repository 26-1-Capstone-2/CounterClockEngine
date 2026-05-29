# 통신 아키텍처

## 역할 분담

| 서버 | 담당 | 기술 |
|------|------|------|
| **App** | GPS 측정 및 전송, 알람/UI 수신 | iOS / Android |
| **Spring** | 앱 ↔ 서버 통신 중계, WebSocket/FCM 관리, DB 저장 | Spring Boot |
| **Engine (CounterClockEngine)** | ETA 계산, GPS 주기 산출, 지각 패턴 분석 | Flask (Python) |

---

## 전체 흐름

```
App                    Spring                     Engine (CounterClockEngine)
   │                        │                              │
   │── GPS 위치 전송 ──────→│                              │
   │                        │── POST /api/group/.../location ──→│
   │                        │                       ETA + 주기 계산
   │                        │←── { summary, gps_intervals } ───│
   │←── WebSocket/FCM ──────│                              │
   │    (주기, ETA 현황)     │                              │
   │                        │                              │
   │ (다음 주기에 GPS 전송)  │                              │
```

**핵심 원칙:**
- Engine은 **순수 계산 API** — REST 요청/응답만 처리, 클라이언트 직접 통신 없음
- Spring이 앱과의 **WebSocket/FCM** 연결을 전담
- Engine의 응답에 포함된 `gps_intervals`를 Spring이 받아 각 멤버에게 Push

---

## API 명세

### 1. 그룹 약속 — 위치 업데이트

Spring이 App의 GPS를 받아 Engine에 전달하고, 결과를 그룹 전체에 broadcast합니다.

**Spring → Engine**
```
POST /api/group/{group_id}/location
```

Request:
```json
{
  "user_id": "user_001",
  "current_loc": [37.4979, 127.0276],
  "kakao_api_key": "..."
}
```

Response:
```json
{
  "summary": {
    "group_id": "abc-123",
    "participants": [
      { "member_id": "user_001", "name": "김철수", "eta_sec": 540, "eta_min": 9.0, "status": "on_time" },
      { "member_id": "user_002", "name": "이영희", "eta_sec": 1200, "eta_min": 20.0, "status": "late" }
    ]
  },
  "gps_intervals": {
    "user_001": { "next_interval_sec": 10, "gps_mode": "HIGH" },
    "user_002": { "next_interval_sec": 30, "gps_mode": "BALANCED" }
  }
}
```

Spring이 할 일:
- `summary` → 그룹 전체에 WebSocket broadcast (`group_update`)
- `gps_intervals` → 각 멤버 개인에게 Push (`request_gps`)

---

### 2. 그룹 약속 — 도착 처리

**Spring → Engine**
```
POST /api/group/{group_id}/arrive
```

Request:
```json
{ "user_id": "user_001" }
```

Response:
```json
{
  "group_id": "abc-123",
  "user_id": "user_001",
  "status": "arrived",
  "summary": { ... }
}
```

Spring이 할 일: `summary` → 그룹 전체 broadcast

---

### 3. 그룹 약속 — 탈퇴

**Spring → Engine**
```
DELETE /api/group/{group_id}/leave
```

Request:
```json
{ "user_id": "user_001" }
```

Response:
```json
{
  "group_id": "abc-123",
  "user_id": "user_001",
  "left": true,
  "summary": { ... }
}
```

Spring이 할 일: `summary` → 그룹 전체 broadcast

---

### 4. 개인 여정 — 위치 업데이트

**Spring → Engine**
```
POST /api/journey/{journey_id}/location
```

Request:
```json
{
  "current_loc": [37.4979, 127.0276],
  "kakao_api_key": "..."
}
```

Response:
```json
{
  "journey_id": "j-001",
  "eta_sec": 900,
  "eta_min": 15.0,
  "alarm_time": "2026-05-27T18:43:00",
  "status": "on_time",
  "next_interval_sec": 30,
  "gps_mode": "BALANCED"
}
```

Spring이 할 일: 해당 멤버 개인에게만 Push (`journey_update`)

---

### 5. 지오펜스 이탈 (Webhook)

Spring DB에서 출발지 500m 이탈 감지 시 Engine에 알립니다.

**Spring → Engine**
```
POST /webhook/db-sync
```

Request:
```json
{
  "type": "geofence_exit",
  "appointment_id": "appt-456",
  "member_id": "user_001",
  "current_lat": 37.4979,
  "current_lon": 127.0276
}
```

Response:
```json
{
  "triggered": "geofence_exit",
  "appointment_id": "appt-456",
  "member_id": "user_001",
  "summary": { ... },
  "gps_intervals": {
    "user_001": { "next_interval_sec": 10, "gps_mode": "HIGH" },
    "user_002": { "next_interval_sec": 60, "gps_mode": "LOW" }
  }
}
```

Spring이 할 일:
- `summary` → 그룹 전체 broadcast
- `gps_intervals` → 각 멤버 개인에게 Push

---

### 6. 출발 알람 계산

Spring이 목표 도착 시간 기준으로 출발 알람 시각을 계산합니다.

**Spring → Engine (개인 여정)**
```
POST /internal/alarm/journey
```

**Spring → Engine (그룹 약속)**
```
POST /internal/alarm/appointment
```

Request:
```json
{
  "current_lat": 37.49796,
  "current_lng": 127.02759,
  "dest_lat": 37.51234,
  "dest_lng": 127.05678,
  "transport_type": "TRANSIT",
  "target_time": "2026-05-27T19:00:00",
  "is_last_mode": false,
  "preparation_time": 10,
  "member_id": "user_001"
}
```

Response:
```json
{
  "departure_alarm_time": "2026-05-27T18:20:00",
  "estimated_arrival": "2026-05-27T19:00:00",
  "latency_buffer_min": 5.2,
  "walk_to_station_min": 3.0
}
```

Spring이 할 일: `departure_alarm_time`에 맞춰 App에 알람 Push

---

### 7. 막차 탐색 결과 캐싱

#### 배경

`is_last_mode: true` 요청 시 Engine은 **이진 탐색**으로 막차 시간을 찾아요.  
23:00~01:00 구간을 1분 정밀도로 탐색하면 `log2(120) ≈ 7~8번`의 ODsay API 호출이 발생해요.  
같은 구간을 여러 유저가 요청하면 호출이 기하급수적으로 늘기 때문에, 탐색 결과를 캐싱해요.

#### 캐시 설계

| 항목 | 내용 |
|------|------|
| **저장소** | 프로세스 내 `dict` (단일 EC2 인스턴스) |
| **캐시 키** | `(출발위도, 출발경도, 목적위도, 목적경도, 날짜)` — 좌표는 소수점 2자리 반올림(~1.1km 단위) |
| **저장 값** | `(막차_출발시각, 소요시간_초, 도보_분)` 또는 `None` (막차 없음) |
| **만료 시각** | 익일 **04:00** (유동인구 최소, 막차 없는 시간대) |
| **만료 처리** | 조회 시점에 lazy 체크 — 별도 스케줄러 없음 |

#### 좌표 반올림 이유

좌표를 그대로 키로 쓰면 `37.12345` vs `37.12346` 같이 수십 cm 차이도 다른 키로 인식돼 캐시 미스가 발생해요.  
소수점 2자리 반올림으로 **같은 생활권 유저들이 동일한 캐시를 공유**하게 해요.

#### `None`도 캐싱하는 이유

막차가 없는 구간(`None` 반환)도 캐싱해요. 동일 구간을 반복 요청할 때 이진 탐색을 다시 수행하지 않도록 **"없다는 사실 자체"도 저장**해요.

#### 전체 흐름

```
요청 수신 (is_last_mode: true)
    ↓
캐시 키 계산 (좌표 반올림 + 날짜)
    ↓
캐시 히트? ──Yes──→ 04:00 이전? ──Yes──→ 캐시 값 반환 (API 호출 0번)
    │                    │
    No                   No (만료)
    ↓                    ↓
이진 탐색 (ODsay API 7~8회 호출)
    ↓
결과 저장 (expires_at = 다음 04:00)
    ↓
결과 반환
```

#### 막차 모드 Response (추가 필드)

```json
{
  "departure_alarm_time": "2026-05-27T23:10:00",
  "estimated_arrival":    "2026-05-27T23:58:00",
  "latency_buffer_min":   5.2,
  "last_train_departure": "2026-05-27T23:20:00",
  "walk_to_station_min":  4.0
}
```

---

### 8. 캐시 동기화 (Webhook)

Spring DB에서 데이터 변경 시 Engine 인메모리 캐시를 동기화합니다.

**Spring → Engine**
```
POST /webhook/db-sync
```

| type | 설명 |
|------|------|
| `appointment` | 약속 정보 upsert |
| `participant` | 단일 참가자 upsert |
| `participants` | 약속의 참가자 전체 교체 |
| `member_settings` | 멤버 설정(버퍼 등) upsert |
| `journey` | 개인 여정 단건 upsert |
| `member_journeys` | 멤버의 여정 목록 전체 교체 |

---

### 9. 지각 패턴 학습

**Spring → Engine (기록)**
```
POST /api/latency/record
```

Request:
```json
{
  "user_id": "user_001",
  "scheduled_time": "2026-05-14T15:00:00",
  "actual_arrival_time": "2026-05-14T15:07:00"
}
```

Response:
```json
{
  "recorded": true,
  "lateness_minutes": 7.0,
  "total_records": 5
}
```

**Spring → Engine (버퍼 조회)**
```
GET /api/latency/buffer?user_id=user_001&confidence=0.80
```

Response:
```json
{
  "user_id": "user_001",
  "buffer_minutes": 8.5,
  "confidence": 0.8,
  "total_records": 12
}
```

---

## gps_mode 기준

| 모드 | next_interval_sec | 상황 |
|------|-------------------|------|
| `HIGH` | 10초 | 목적지 근처 / 막차 임박 |
| `BALANCED` | 30초 | 일반 이동 중 |
| `LOW` | 60초 | 아직 출발 전 / 여유 있음 |

---

## 데이터 흐름 요약

```
[App]
  │  GPS 전송 (Spring에게)
  ↓
[Spring]
  │  위치 중계 (Engine에게)         │  DB 저장
  ↓                                 ↓
[Engine (CounterClockEngine)]    [Spring DB]
  │  ETA + 주기 계산               │  지오펜스 감지 시 webhook
  │  → summary, gps_intervals      └──────────────────────────→ [Engine]
  ↓
[Spring]
  │  WebSocket/FCM Push
  ├── group_update  → 그룹 전체
  └── request_gps  → 각 멤버 개인
        ↓
     [App]
```
