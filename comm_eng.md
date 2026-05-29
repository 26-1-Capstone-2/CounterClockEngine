# Communication Architecture

## Role Assignment

| Server | Responsibility | Technology |
|--------|---------------|------------|
| **App** | GPS measurement and transmission, receiving alarm/UI | iOS / Android |
| **Spring** | App ↔ server communication relay, WebSocket/FCM management, DB storage | Spring Boot |
| **Engine (CounterClockEngine)** | ETA calculation, GPS interval computation, lateness pattern analysis | Flask (Python) |

---

## Overall Flow

```
App                    Spring                     Engine (CounterClockEngine)
   │                        │                              │
   │── Send GPS location ──→│                              │
   │                        │── POST /api/group/.../location ──→│
   │                        │                       ETA + interval calculation
   │                        │←── { summary, gps_intervals } ───│
   │←── WebSocket/FCM ──────│                              │
   │    (interval, ETA status)  │                          │
   │                        │                              │
   │ (send GPS on next interval) │                         │
```

**Core Principles:**
- Engine is a **pure calculation API** — handles only REST request/response, no direct client communication
- Spring exclusively manages **WebSocket/FCM** connections with the app
- Spring receives `gps_intervals` from Engine's response and pushes to each member

---

## API Specification

### 1. Group Appointment — Location Update

Spring receives GPS from the App, forwards it to Engine, and broadcasts the result to the entire group.

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
      { "member_id": "user_001", "name": "Kim Cheolsu", "eta_sec": 540, "eta_min": 9.0, "status": "on_time" },
      { "member_id": "user_002", "name": "Lee Younghee", "eta_sec": 1200, "eta_min": 20.0, "status": "late" }
    ]
  },
  "gps_intervals": {
    "user_001": { "next_interval_sec": 10, "gps_mode": "HIGH" },
    "user_002": { "next_interval_sec": 30, "gps_mode": "BALANCED" }
  }
}
```

Spring's responsibilities:
- `summary` → WebSocket broadcast to entire group (`group_update`)
- `gps_intervals` → Push to each member individually (`request_gps`)

---

### 2. Group Appointment — Arrival Processing

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

Spring's responsibility: `summary` → broadcast to entire group

---

### 3. Group Appointment — Leave

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

Spring's responsibility: `summary` → broadcast to entire group

---

### 4. Personal Journey — Location Update

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

Spring's responsibility: Push only to that individual member (`journey_update`)

---

### 5. Geofence Exit (Webhook)

Spring notifies Engine when a 500m departure from the origin is detected in the Spring DB.

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

Spring's responsibilities:
- `summary` → broadcast to entire group
- `gps_intervals` → Push to each member individually

---

### 6. Departure Alarm Calculation

Spring calculates the departure alarm time based on the target arrival time.

**Spring → Engine (Personal Journey)**
```
POST /internal/alarm/journey
```

**Spring → Engine (Group Appointment)**
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

Spring's responsibility: Push alarm to App at `departure_alarm_time`

---

### 7. Last Train Search Result Caching

#### Background

For `is_last_mode: true` requests, Engine uses **binary search** to find the last train time.  
Searching the 23:00~01:00 range with 1-minute precision requires `log2(120) ≈ 7~8 calls` to the ODsay API.  
When multiple users request the same range, calls increase exponentially, so the search results are cached.

#### Cache Design

| Item | Details |
|------|---------|
| **Storage** | In-process `dict` (single EC2 instance) |
| **Cache key** | `(origin_lat, origin_lon, dest_lat, dest_lon, date)` — coordinates rounded to 2 decimal places (~1.1km granularity) |
| **Stored value** | `(last_train_departure_time, duration_sec, walk_min)` or `None` (no last train) |
| **Expiry time** | **04:00** the following day (minimum commuter traffic, no last train time period) |
| **Expiry handling** | Lazy check at query time — no separate scheduler |

#### Reason for Coordinate Rounding

Using coordinates as-is as keys would cause cache misses for differences of tens of centimeters like `37.12345` vs `37.12346`.  
Rounding to 2 decimal places ensures **users in the same neighborhood share the same cache**.

#### Reason for Caching `None`

Ranges with no last train (`None` return) are also cached. To avoid re-running binary search for repeated requests on the same range, **the fact that there is none is also stored**.

#### Overall Flow

```
Request received (is_last_mode: true)
    ↓
Calculate cache key (coordinate rounding + date)
    ↓
Cache hit? ──Yes──→ Before 04:00? ──Yes──→ Return cached value (0 API calls)
    │                    │
    No                   No (expired)
    ↓                    ↓
Binary search (ODsay API ~7~8 calls)
    ↓
Save result (expires_at = next 04:00)
    ↓
Return result
```

#### Last Train Mode Response (Additional Fields)

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

### 8. Cache Synchronization (Webhook)

Synchronizes the Engine in-memory cache when data changes in the Spring DB.

**Spring → Engine**
```
POST /webhook/db-sync
```

| type | Description |
|------|-------------|
| `appointment` | Appointment info upsert |
| `participant` | Single participant upsert |
| `participants` | Replace all participants for an appointment |
| `member_settings` | Member settings (buffer etc.) upsert |
| `journey` | Single personal journey upsert |
| `member_journeys` | Replace all journeys for a member |

---

### 9. Lateness Pattern Learning

**Spring → Engine (record)**
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

**Spring → Engine (buffer query)**
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

## gps_mode Criteria

| Mode | next_interval_sec | Situation |
|------|-------------------|-----------|
| `HIGH` | 10s | Near destination / last train approaching |
| `BALANCED` | 30s | In normal transit |
| `LOW` | 60s | Not yet departed / plenty of time |

---

## Data Flow Summary

```
[App]
  │  Send GPS (to Spring)
  ↓
[Spring]
  │  Relay location (to Engine)         │  Save to DB
  ↓                                     ↓
[Engine (CounterClockEngine)]    [Spring DB]
  │  ETA + interval calculation         │  webhook on geofence detection
  │  → summary, gps_intervals           └──────────────────────────→ [Engine]
  ↓
[Spring]
  │  WebSocket/FCM Push
  ├── group_update  → entire group
  └── request_gps  → each member individually
        ↓
     [App]
```
