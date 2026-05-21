# CounterClock GPS 서버 — API 명세서

> **Base URL** `http://<host>:5000`
>
> 모든 REST 요청/응답 Content-Type: `application/json`
> 공통 에러 응답: `{ "error": "<message>" }` (400 / 404 / 500)

---

## 목차

- [공통 규약](#공통-규약)
- [A. 프런트엔드 연동 API](#a-프런트엔드-연동-api)
  - [A1. 그룹 약속](#a1-그룹-약속)
  - [A2. 개인 여정](#a2-개인-여정)
  - [A3. 지각 버퍼](#a3-지각-버퍼)
  - [A4. GPS 갱신 주기 계산 (단독 호출용)](#a4-gps-갱신-주기-계산-단독-호출용)
  - [A5. WebSocket 이벤트](#a5-websocket-이벤트)
- [B. DB 서버 연동 API (Webhook)](#b-db-서버-연동-api-webhook)
  - [B1. 데이터 캐시 Push](#b1-데이터-캐시-push)
  - [B2. 지오펜스 이탈 알림](#b2-지오펜스-이탈-알림)
- [C. 참고 — 상태값 정의](#c-참고--상태값-정의)

---

## 공통 규약

### 좌표 표현

```json
[위도(lat), 경도(lon)]
// 예: [37.5088, 127.0632]
```

### 시각 표현

ISO 8601 형식 사용.

```
"2026-05-23T19:00:00"
```

### 서버 헬스 체크

```
GET /health
```

```json
{ "status": "ok" }
```

---

## A. 프런트엔드 연동 API

---

### A1. 그룹 약속

#### `POST /api/group/create` — 그룹 생성

> 약속을 새로 만들 때 호출합니다.

**Request**

```json
{
  "name": "금요일 저녁 약속",
  "destination": [37.5088, 127.0632],
  "appointment_time": "2026-05-23T19:00:00"
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `name` | string | ✅ | 약속 이름 |
| `destination` | [lat, lon] | ✅ | 목적지 좌표 |
| `appointment_time` | ISO 8601 | ✅ | 약속 시각 |

**Response** `201`

```json
{
  "group_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "금요일 저녁 약속",
  "destination": [37.5088, 127.0632],
  "appointment_time": "2026-05-23T19:00:00"
}
```

---

#### `POST /api/group/join-by-invite` — 초대 코드로 참가

> 초대 코드를 받은 사용자가 약속에 참가할 때 호출합니다.

**Request**

```json
{
  "invite_code": "ABC12345",
  "member_id": "user_002",
  "name": "이영희",
  "travel_mode": "transit",
  "origin": [37.4979, 127.0276],
  "origin_name": "집",
  "origin_address": "서울시 강남구 ..."
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `invite_code` | string | ✅ | 8자리 초대 코드 |
| `member_id` | string | ✅ | 사용자 ID |
| `name` | string | ✅ | 표시 이름 |
| `travel_mode` | string | | `walking` / `transit` / `vehicle` (기본: `transit`) |
| `origin` | [lat, lon] | | 출발지 좌표 |
| `origin_name` | string | | 출발지 이름 |
| `origin_address` | string | | 출발지 주소 |

**Response** `200`

```json
{
  "group_id": "550e8400-...",
  "title": "금요일 저녁 약속",
  "participant_id": "part-001",
  "member_id": "user_002",
  "name": "이영희",
  "status": "unknown"
}
```

---

#### `POST /api/group/<group_id>/join` — 그룹 직접 참가

**Request**

```json
{
  "user_id": "user_001",
  "name": "김철수",
  "travel_mode": "transit",
  "origin": [37.4979, 127.0276]
}
```

**Response** `200`

```json
{
  "group_id": "550e8400-...",
  "participant_id": "part-001",
  "member_id": "user_001",
  "name": "김철수",
  "is_host": true,
  "travel_mode": "transit",
  "status": "unknown"
}
```

---

#### `POST /api/group/<group_id>/location` — 위치 업데이트

> GPS 신호를 수신할 때마다 호출합니다. 서버가 전체 ETA를 재계산하고 그룹에 broadcast합니다.
>
> **주의**: 이 엔드포인트 호출 후 WebSocket의 `request_gps` 이벤트로 다음 전송 시각이 지정됩니다.

**Request**

```json
{
  "user_id": "user_001",
  "current_loc": [37.4979, 127.0276],
  "kakao_api_key": "kakao-rest-api-key"
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `user_id` | string | ✅ | 사용자 ID |
| `current_loc` | [lat, lon] | ✅ | 현재 GPS 좌표 |
| `kakao_api_key` | string | | 카카오 REST API 키 (없으면 직선거리 ETA) |

**Response** `200` — [그룹 현황 객체](#그룹-현황-객체)

> 응답과 동시에 WebSocket `group_update` (그룹 room), `request_gps` (개인 room) 이벤트가 emit됩니다.

---

#### `GET /api/group/<group_id>` — 그룹 현황 조회

**Response** `200` — [그룹 현황 객체](#그룹-현황-객체)

---

#### `POST /api/group/<group_id>/arrive` — 도착 처리


**Request**

```json
{ "user_id": "user_001" }
```

**Response** `200`

```json
{
  "group_id": "550e8400-...",
  "user_id": "user_001",
  "status": "arrived"
}
```

> WebSocket `group_update`가 그룹 전체에 emit됩니다.

---

#### `DELETE /api/group/<group_id>/leave` — 그룹 탈퇴

**Request**

```json
{ "user_id": "user_001" }
```

**Response** `200`

```json
{
  "group_id": "550e8400-...",
  "user_id": "user_001",
  "left": true
}
```

---

#### 그룹 현황 객체

`GET /api/group/<id>`, `POST /api/group/<id>/location` 등의 공통 응답 형식입니다.

```json
{
  "group_id": "550e8400-...",
  "title": "금요일 저녁 약속",
  "destination": [37.5088, 127.0632],
  "destination_name": "강남역",
  "appointment_time": "2026-05-23T19:00:00",
  "status": "active",
  "invite_code": "ABC12345",
  "participants": [
    {
      "participant_id": "part-001",
      "member_id": "user_001",
      "name": "김철수",
      "is_host": true,
      "travel_mode": "transit",
      "has_location": true,
      "eta_sec": 1080,
      "eta_min": 18.0,
      "distance_m": 1520.3,
      "alarm_time": "2026-05-23T18:29:00",
      "alarm_enabled": true,
      "status": "on_time",
      "last_updated": "2026-05-23T18:10:00"
    }
  ]
}
```

| 필드 | 설명 |
|------|------|
| `eta_sec` | 목적지까지 예상 소요 시간 (초). GPS 없으면 `null` |
| `eta_min` | 분 단위 (소수점 1자리) |
| `distance_m` | 목적지까지 거리 (미터) |
| `alarm_time` | 출발해야 할 시각 (`약속시간 - ETA - 개인버퍼`) |
| `status` | [참가자 상태](#참가자-status) |

---

### A2. 개인 여정

#### `GET /api/journey/<journey_id>` — 여정 조회

**Response** `200` — [여정 객체](#여정-객체)

---

#### `GET /api/journey/member/<member_id>` — 멤버 여정 목록

**Response** `200`

```json
{
  "member_id": "user_001",
  "total": 2,
  "journeys": [ /* 여정 객체 배열 */ ]
}
```

---

#### `POST /api/journey/<journey_id>/location` — 여정 위치 업데이트

**Request**

```json
{
  "current_loc": [37.4979, 127.0276],
  "kakao_api_key": "kakao-rest-api-key"
}
```

**Response** `200` — [여정 객체](#여정-객체)

> WebSocket `journey_update`가 본인 room에 emit됩니다.

---

#### `POST /api/journey/eta` — 임시 ETA 계산 (저장 없음)

> 테스트·데모용. DB에 저장되지 않습니다.

**Request**

```json
{
  "member_id": "user_001",
  "current_loc": [37.4979, 127.0276],
  "destination": [37.5088, 127.0632],
  "goal_time": "2026-05-23T19:00:00",
  "travel_mode": "transit",
  "kakao_api_key": "kakao-rest-api-key"
}
```

**Response** `200` — [여정 객체](#여정-객체)

---

#### 여정 객체

```json
{
  "journey_id": "journey-001",
  "member_id": "user_001",
  "title": "출근길",
  "current_lat": 37.4979,
  "current_lon": 127.0276,
  "dest_lat": 37.5088,
  "dest_lon": 127.0632,
  "goal_time": "2026-05-23T09:00:00",
  "eta_sec": 900,
  "eta_min": 15.0,
  "alarm_time": "2026-05-23T08:42:00",
  "status": "on_time",
  "travel_mode": "transit"
}
```

---

### A3. 지각 버퍼

#### `POST /api/latency/record` — 도착 기록 저장

> 약속 종료 후 실제 도착 시각을 기록합니다. 기록이 쌓일수록 개인 버퍼가 정확해집니다.

**Request**

```json
{
  "user_id": "user_001",
  "scheduled_time": "2026-05-14T15:00:00",
  "actual_arrival_time": "2026-05-14T15:07:00",
  "location_id": "loc_gangnam",
  "event_id": "ev_001"
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `user_id` | string | ✅ | |
| `scheduled_time` | ISO 8601 | ✅ | 약속 시각 |
| `actual_arrival_time` | ISO 8601 | ✅ | 실제 도착 시각 |
| `location_id` | string | | 장소 식별자 (기본: `"default"`) |
| `event_id` | string | | 이벤트 식별자 (기본: 자동 생성) |

**Response** `200`

```json
{
  "recorded": true,
  "lateness_minutes": 7.0,
  "total_records": 5
}
```

---

#### `GET /api/latency/buffer` — 개인 출발 버퍼 조회

**Query Params**

| 파라미터 | 필수 | 기본값 | 설명 |
|----------|------|--------|------|
| `user_id` | ✅ | | 사용자 ID |
| `confidence` | | `0.80` | 신뢰 수준 (0.70 ~ 0.99) |

**Response** `200`

```json
{
  "user_id": "user_001",
  "buffer_minutes": 8.5,
  "confidence": 0.8,
  "total_records": 12
}
```

---

#### `GET /api/latency/history` — 지각 기록 조회

**Query Params**: `user_id` (필수)

**Response** `200`

```json
{
  "user_id": "user_001",
  "total_records": 3,
  "records": [
    {
      "event_id": "ev_001",
      "scheduled_time": "2026-05-14T15:00:00",
      "actual_arrival_time": "2026-05-14T15:07:00",
      "location_id": "loc_gangnam",
      "lateness_minutes": 7.0
    }
  ]
}
```

---

### A4. GPS 갱신 주기 계산 (단독 호출용)

#### `POST /api/optimizer/interval`

> 그룹/여정 API를 사용하지 않고 GPS 주기만 별도로 계산할 때 사용합니다.
> 일반적으로는 `request_gps` WebSocket 이벤트를 통해 주기가 자동 전달됩니다.

**Request**

```json
{
  "lat": 37.5,
  "lon": 127.0,
  "history": [
    {"lat": 37.49, "lon": 127.01, "timestamp": "2026-05-14T10:00:00"},
    {"lat": 37.495, "lon": 127.005, "timestamp": "2026-05-14T10:00:30"}
  ],
  "geofences": [
    {"id": "destination", "lat": 37.5088, "lon": 127.0632, "radius": 200}
  ]
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `lat` | float | ✅ | 현재 위도 |
| `lon` | float | ✅ | 현재 경도 |
| `history` | array | | 최근 위치 이력 (최대 5개) |
| `geofences` | array | | 지오펜스 목록 (`radius` 단위: 미터) |

**Response** `200`

```json
{
  "next_interval": 18,
  "activity": "vehicle",
  "gps_mode": "BALANCED",
  "is_significant_change": true,
  "moved_distance": 320.5,
  "distance_to_nearest_fence": 450.2,
  "entered_zones": [],
  "debug": {
    "base_interval": 60,
    "activity_multiplier": 0.3,
    "slc_multiplier": 1.0
  }
}
```

| 필드 | 설명 |
|------|------|
| `next_interval` | 다음 GPS 전송까지 대기 시간 (초) |
| `activity` | `stationary` / `walking` / `vehicle` / `unknown` |
| `gps_mode` | `HIGH` / `BALANCED` / `LOW` |
| `entered_zones` | 진입한 지오펜스 ID 목록 |

---

### A5. WebSocket 이벤트

WebSocket 연결: `ws://<host>:5000/socket.io/`  
라이브러리: [Socket.IO](https://socket.io/) (클라이언트 v4 권장)

#### 연결 순서

```javascript
const socket = io("http://<host>:5000");

// 1. 그룹 room 입장 (group_update 수신용)
socket.emit("join_group", { group_id: "550e8400-..." });

// 2. 개인 room 입장 (request_gps 수신용)
socket.emit("join_member", { member_id: "user_001" });
```

---

#### 클라이언트 → 서버

| 이벤트 | Payload | 설명 |
|--------|---------|------|
| `join_group` | `{ "group_id": "..." }` | 그룹 room 입장. 입장 시 현재 그룹 현황 즉시 수신 |
| `join_member` | `{ "member_id": "..." }` | 개인 room 입장. 이후 `request_gps` 수신 가능 |
| `join_journey` | `{ "journey_id": "..." }` | 개인 여정 room 입장. 이후 `journey_update` 수신 가능 |

---

#### 서버 → 클라이언트

| 이벤트 | 수신 조건 | Payload |
|--------|-----------|---------|
| `group_update` | 그룹 누군가의 위치 변경 / 도착 / 탈퇴 / 지오펜스 이탈 | [그룹 현황 객체](#그룹-현황-객체) |
| `request_gps` | 위치 처리 완료 후 개인 room으로 전송 | `{ "group_id": "...", "next_interval_sec": 18, "gps_mode": "BALANCED" }` |
| `journey_update` | 여정 위치 업데이트 완료 | [여정 객체](#여정-객체) |

---

#### `request_gps` 처리 예시

```javascript
socket.on("request_gps", ({ group_id, next_interval_sec, gps_mode }) => {
  // gps_mode에 따라 디바이스 GPS 정확도 설정 가능
  // HIGH: 고정밀, BALANCED: 균형, LOW: 절전

  setTimeout(() => {
    navigator.geolocation.getCurrentPosition((pos) => {
      fetch(`/api/group/${group_id}/location`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: MY_USER_ID,
          current_loc: [pos.coords.latitude, pos.coords.longitude],
          kakao_api_key: KAKAO_KEY,
        }),
      });
    });
  }, next_interval_sec * 1000);
});
```

---

## B. DB 서버 연동 API (Webhook)

> DB 서버가 GPS 서버로 호출하는 엔드포인트입니다. 프런트엔드에서는 호출하지 않습니다.

**Base URL** `POST /webhook/db-sync`

모든 요청은 `"type"` 필드로 처리 방식을 구분합니다.

---

### B1. 데이터 캐시 Push

데이터가 생성되거나 변경될 때마다 즉시 호출합니다.
GPS 서버는 수신한 데이터를 인메모리 캐시(TTL 300초)에 저장해 GPS 처리 시 **DB를 직접 조회하지 않습니다.**
캐시 미스가 발생할 경우에만 DB를 fallback으로 조회합니다.

**GPS 서버가 캐시로 처리하는 모든 읽기 목록**

| 읽기 목적 | Webhook type | fallback |
|-----------|-------------|---------|
| 약속 정보 조회 | `appointment` | DB 직접 조회 |
| 초대 코드로 약속 조회 | `appointment` (invite_code 포함 시 자동 인덱싱) | DB 직접 조회 |
| 참가자 목록 조회 | `participants` / `participant` | DB 직접 조회 |
| 멤버 설정(버퍼 등) 조회 | `member_settings` | DB 직접 조회 → latency 기록 기반 계산 |
| 개인 여정 조회 | `journey` / `member_journeys` | DB 직접 조회 |

---

#### 약속(Appointment) upsert

> `invite_code` 필드가 포함되면 초대 코드 인덱스도 자동으로 갱신됩니다.

```json
{
  "type": "appointment",
  "data": {
    "appointment_id": "550e8400-...",
    "title": "금요일 저녁 약속",
    "destination_lat": 37.5088,
    "destination_lon": 127.0632,
    "destination_name": "강남역",
    "destination_address": "서울시 강남구 강남대로 지하 396",
    "appointment_time": "2026-05-23T19:00:00",
    "status": "active",
    "invite_code": "ABC12345"
  }
}
```

**Response** `200`

```json
{ "cached": "appointment", "id": "550e8400-..." }
```

---

#### 참가자(Participant) 단건 upsert

> 참가자 추가 또는 정보 변경 시 호출합니다.

```json
{
  "type": "participant",
  "data": {
    "participant_id": "part-001",
    "appointment_id": "550e8400-...",
    "member_id": "user_001",
    "name": "김철수",
    "is_host": true,
    "travel_mode": "transit",
    "origin_name": "집",
    "origin_address": "서울시 서초구 ...",
    "origin_lat": 37.4979,
    "origin_lon": 127.0276,
    "current_lat": null,
    "current_lon": null,
    "eta": null,
    "alarm_time": null,
    "alarm_enabled": true,
    "status": "unknown"
  }
}
```

**Response** `200`

```json
{ "cached": "participant", "id": "part-001" }
```

---

#### 참가자 전체 목록 교체 (초기 로딩)

> 약속 생성 시 또는 참가자 목록 전체를 한 번에 동기화할 때 사용합니다.

```json
{
  "type": "participants",
  "appointment_id": "550e8400-...",
  "data": [
    { /* participant 객체 */ },
    { /* participant 객체 */ }
  ]
}
```

**Response** `200`

```json
{ "cached": "participants", "count": 3 }
```

---

#### 멤버 설정(Member Settings) upsert

> 멤버 생성 또는 설정 변경 시 호출합니다.
> `buffer_minutes`는 출발 알람 시각 계산(`약속시간 - ETA - 버퍼`)에 사용됩니다.

```json
{
  "type": "member_settings",
  "data": {
    "member_id": "user_001",
    "buffer_minutes": 10,
    "preferred_transit": "transit",
    "route_priority": "RECOMMEND"
  }
}
```

**Response** `200`

```json
{ "cached": "member_settings", "member_id": "user_001" }
```

---

#### 개인 여정(Journey) 단건 upsert

> 여정 생성 또는 변경 시 호출합니다.

```json
{
  "type": "journey",
  "data": {
    "journey_id": "journey-001",
    "member_id": "user_001",
    "title": "출근길",
    "journey_type": "one_way",
    "travel_mode": "transit",
    "origin_lat": 37.4979,
    "origin_lon": 127.0276,
    "origin_name": "집",
    "origin_address": "서울시 서초구 ...",
    "dest_lat": 37.5088,
    "dest_lon": 127.0632,
    "dest_name": "회사",
    "dest_address": "서울시 강남구 ...",
    "goal_time": "2026-05-23T09:00:00",
    "last_train": false,
    "repeat_days": null,
    "planned_date": "2026-05-23",
    "alarm_enabled": true,
    "current_lat": null,
    "current_lon": null,
    "eta": null,
    "alarm_time": null,
    "status": "unknown"
  }
}
```

**Response** `200`

```json
{ "cached": "journey", "id": "journey-001" }
```

---

#### 멤버 여정 목록 전체 교체

> 멤버의 여정 목록을 한 번에 동기화할 때 사용합니다.
> 각 여정은 개별 journey 캐시에도 자동으로 등록됩니다.

```json
{
  "type": "member_journeys",
  "member_id": "user_001",
  "data": [
    { /* journey 객체 */ },
    { /* journey 객체 */ }
  ]
}
```

**Response** `200`

```json
{ "cached": "member_journeys", "member_id": "user_001", "count": 2 }
```

---

### B2. 지오펜스 이탈 알림

> 참가자가 출발지로부터 반경 **500m**를 벗어났을 때 호출합니다.
> GPS 서버는 이 이벤트를 받아 **이동 시작**으로 판단하고 추적을 본격화합니다.

```json
{
  "type": "geofence_exit",
  "appointment_id": "550e8400-...",
  "member_id": "user_001",
  "current_lat": 37.5010,
  "current_lon": 127.0350
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `appointment_id` | string | ✅ | 약속 ID |
| `member_id` | string | ✅ | 이탈한 참가자의 멤버 ID |
| `current_lat` | float | ✅ | 이탈 감지 시점의 위도 |
| `current_lon` | float | ✅ | 이탈 감지 시점의 경도 |

**Response** `200`

```json
{
  "triggered": "geofence_exit",
  "appointment_id": "550e8400-...",
  "member_id": "user_001",
  "gps_interval": {
    "next_interval_sec": 10,
    "gps_mode": "BALANCED"
  }
}
```

**GPS 서버가 자동으로 수행하는 동작**

| 동작 | 설명 |
|------|------|
| 전체 ETA 재계산 | 이탈 좌표를 반영해 그룹 전체 ETA 갱신 |
| `group_update` emit | 그룹 room 전체에 "이 참가자 출발" broadcast |
| `request_gps` emit (이탈자) | 개인 room에 `next_interval_sec: 10` (BALANCED) 즉시 전송 |
| `request_gps` emit (나머지) | 각 참가자에게 Adaptive Interval 계산값 전송 |

**지오펜스 이탈 전후 GPS 주기 비교**

| 단계 | 조건 | GPS 주기 |
|------|------|----------|
| Phase 1 (출발 전) | 출발지 500m 이내 | 최대 300초 (LOW) |
| Phase 2 (이동 중) | 500m 이탈 직후 | 10초 (BALANCED) → 이후 Adaptive |

---

## C. 참고 — 상태값 정의

### 참가자 status

| 값 | 조건 | 의미 |
|----|------|------|
| `unknown` | GPS 없음 | 위치 미확인 |
| `on_time` | `(약속시간 - 현재) - ETA ≥ 10분` | 여유 있음 |
| `leave_soon` | 여유 0 ~ 10분 | 곧 출발해야 함 |
| `hurry` | ETA > 남은 시간 | 늦을 가능성 있음 |
| `late` | 약속 시간 초과 | 지각 |
| `arrived` | 목적지 100m 이내 | 도착 |

### gps_mode

| 값 | next_interval 범위 | 배터리 모드 |
|----|--------------------|------------|
| `HIGH` | 1 ~ 9초 | 고정밀 (목적지 200m 이내) |
| `BALANCED` | 10 ~ 29초 | 균형 |
| `LOW` | 30 ~ 300초 | 절전 |

### travel_mode

| 값 | 설명 |
|----|------|
| `walking` | 도보 |
| `transit` | 대중교통 (기본값) |
| `vehicle` | 자동차 |
