# CounterClock GPS Server — API Specification

> **Base URL** `http://<host>:5000`
>
> All REST request/response Content-Type: `application/json`
> Common error response: `{ "error": "<message>" }` (400 / 404 / 500)

---

## Table of Contents

- [Common Conventions](#common-conventions)
- [A. Frontend Integration API](#a-frontend-integration-api)
  - [A1. Group Appointments](#a1-group-appointments)
  - [A2. Personal Journeys](#a2-personal-journeys)
  - [A3. Lateness Buffer](#a3-lateness-buffer)
  - [A4. GPS Update Interval Calculation (Standalone)](#a4-gps-update-interval-calculation-standalone)
  - [A5. WebSocket Events](#a5-websocket-events)
- [B. DB Server Integration API (Webhook)](#b-db-server-integration-api-webhook)
  - [B1. Data Cache Push](#b1-data-cache-push)
  - [B2. Geofence Exit Notification](#b2-geofence-exit-notification)
- [C. Reference — Status Value Definitions](#c-reference--status-value-definitions)

---

## Common Conventions

### Coordinate Representation

```json
[latitude(lat), longitude(lon)]
// Example: [37.5088, 127.0632]
```

### Time Representation

ISO 8601 format.

```
"2026-05-23T19:00:00"
```

### Server Health Check

```
GET /health
```

```json
{ "status": "ok" }
```

---

## A. Frontend Integration API

---

### A1. Group Appointments

#### `POST /api/group/create` — Create Group

> Called when creating a new appointment.

**Request**

```json
{
  "name": "Friday evening appointment",
  "destination": [37.5088, 127.0632],
  "appointment_time": "2026-05-23T19:00:00"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | ✅ | Appointment name |
| `destination` | [lat, lon] | ✅ | Destination coordinates |
| `appointment_time` | ISO 8601 | ✅ | Appointment time |

**Response** `201`

```json
{
  "group_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "Friday evening appointment",
  "destination": [37.5088, 127.0632],
  "appointment_time": "2026-05-23T19:00:00"
}
```

---

#### `POST /api/group/join-by-invite` — Join by Invite Code

> Called when a user who received an invite code joins an appointment.

**Request**

```json
{
  "invite_code": "ABC12345",
  "member_id": "user_002",
  "name": "Lee Younghee",
  "travel_mode": "transit",
  "origin": [37.4979, 127.0276],
  "origin_name": "Home",
  "origin_address": "Seoul Gangnam-gu ..."
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `invite_code` | string | ✅ | 8-digit invite code |
| `member_id` | string | ✅ | User ID |
| `name` | string | ✅ | Display name |
| `travel_mode` | string | | `walking` / `transit` / `vehicle` (default: `transit`) |
| `origin` | [lat, lon] | | Departure coordinates |
| `origin_name` | string | | Departure location name |
| `origin_address` | string | | Departure address |

**Response** `200`

```json
{
  "group_id": "550e8400-...",
  "title": "Friday evening appointment",
  "participant_id": "part-001",
  "member_id": "user_002",
  "name": "Lee Younghee",
  "status": "unknown"
}
```

---

#### `POST /api/group/<group_id>/join` — Direct Group Join

**Request**

```json
{
  "user_id": "user_001",
  "name": "Kim Cheolsu",
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
  "name": "Kim Cheolsu",
  "is_host": true,
  "travel_mode": "transit",
  "status": "unknown"
}
```

---

#### `POST /api/group/<group_id>/location` — Location Update

> Called every time a GPS signal is received. The server recalculates the full ETA and broadcasts to the group.
>
> **Note**: After calling this endpoint, the next send time is specified via the WebSocket `request_gps` event.

**Request**

```json
{
  "user_id": "user_001",
  "current_loc": [37.4979, 127.0276],
  "kakao_api_key": "kakao-rest-api-key"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | User ID |
| `current_loc` | [lat, lon] | ✅ | Current GPS coordinates |
| `kakao_api_key` | string | | Kakao REST API key (uses straight-line ETA if absent) |

**Response** `200` — [Group Status Object](#group-status-object)

> Simultaneously with the response, WebSocket `group_update` (group room) and `request_gps` (personal room) events are emitted.

---

#### `GET /api/group/<group_id>` — Get Group Status

**Response** `200` — [Group Status Object](#group-status-object)

---

#### `POST /api/group/<group_id>/arrive` — Mark Arrival

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

> WebSocket `group_update` is emitted to the entire group.

---

#### `DELETE /api/group/<group_id>/leave` — Leave Group

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

#### Group Status Object

Common response format for `GET /api/group/<id>`, `POST /api/group/<id>/location`, etc.

```json
{
  "group_id": "550e8400-...",
  "title": "Friday evening appointment",
  "destination": [37.5088, 127.0632],
  "destination_name": "Gangnam Station",
  "appointment_time": "2026-05-23T19:00:00",
  "status": "active",
  "invite_code": "ABC12345",
  "participants": [
    {
      "participant_id": "part-001",
      "member_id": "user_001",
      "name": "Kim Cheolsu",
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

| Field | Description |
|-------|-------------|
| `eta_sec` | Estimated travel time to destination (seconds). `null` if no GPS |
| `eta_min` | In minutes (1 decimal place) |
| `distance_m` | Distance to destination (meters) |
| `alarm_time` | Time to depart (`appointment_time - ETA - personal_buffer`) |
| `status` | [Participant status](#participant-status) |

---

### A2. Personal Journeys

#### `GET /api/journey/<journey_id>` — Get Journey

**Response** `200` — [Journey Object](#journey-object)

---

#### `GET /api/journey/member/<member_id>` — Member Journey List

**Response** `200`

```json
{
  "member_id": "user_001",
  "total": 2,
  "journeys": [ /* journey object array */ ]
}
```

---

#### `POST /api/journey/<journey_id>/location` — Journey Location Update

**Request**

```json
{
  "current_loc": [37.4979, 127.0276],
  "kakao_api_key": "kakao-rest-api-key"
}
```

**Response** `200` — [Journey Object](#journey-object)

> WebSocket `journey_update` is emitted to the user's own room.

---

#### `POST /api/journey/eta` — Temporary ETA Calculation (No Save)

> For testing/demo purposes. Not saved to DB.

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

**Response** `200` — [Journey Object](#journey-object)

---

#### Journey Object

```json
{
  "journey_id": "journey-001",
  "member_id": "user_001",
  "title": "Morning commute",
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

### A3. Lateness Buffer

#### `POST /api/latency/record` — Save Arrival Record

> Records the actual arrival time after an appointment ends. The more records accumulated, the more accurate the personal buffer becomes.

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

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | ✅ | |
| `scheduled_time` | ISO 8601 | ✅ | Scheduled appointment time |
| `actual_arrival_time` | ISO 8601 | ✅ | Actual arrival time |
| `location_id` | string | | Location identifier (default: `"default"`) |
| `event_id` | string | | Event identifier (default: auto-generated) |

**Response** `200`

```json
{
  "recorded": true,
  "lateness_minutes": 7.0,
  "total_records": 5
}
```

---

#### `GET /api/latency/buffer` — Get Personal Departure Buffer

**Query Params**

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `user_id` | ✅ | | User ID |
| `confidence` | | `0.80` | Confidence level (0.70 ~ 0.99) |

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

#### `GET /api/latency/history` — Get Lateness History

**Query Params**: `user_id` (required)

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

### A4. GPS Update Interval Calculation (Standalone)

#### `POST /api/optimizer/interval`

> Used to calculate GPS interval separately without using the group/journey API.
> Normally, the interval is automatically delivered via the `request_gps` WebSocket event.

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

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `lat` | float | ✅ | Current latitude |
| `lon` | float | ✅ | Current longitude |
| `history` | array | | Recent location history (max 5 entries) |
| `geofences` | array | | Geofence list (`radius` unit: meters) |

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

| Field | Description |
|-------|-------------|
| `next_interval` | Wait time until next GPS send (seconds) |
| `activity` | `stationary` / `walking` / `vehicle` / `unknown` |
| `gps_mode` | `HIGH` / `BALANCED` / `LOW` |
| `entered_zones` | List of entered geofence IDs |

---

### A5. WebSocket Events

WebSocket connection: `ws://<host>:5000/socket.io/`  
Library: [Socket.IO](https://socket.io/) (client v4 recommended)

#### Connection Sequence

```javascript
const socket = io("http://<host>:5000");

// 1. Enter group room (for receiving group_update)
socket.emit("join_group", { group_id: "550e8400-..." });

// 2. Enter personal room (for receiving request_gps)
socket.emit("join_member", { member_id: "user_001" });
```

---

#### Client → Server

| Event | Payload | Description |
|-------|---------|-------------|
| `join_group` | `{ "group_id": "..." }` | Enter group room. Immediately receive current group status upon joining |
| `join_member` | `{ "member_id": "..." }` | Enter personal room. Can receive `request_gps` afterwards |
| `join_journey` | `{ "journey_id": "..." }` | Enter personal journey room. Can receive `journey_update` afterwards |

---

#### Server → Client

| Event | Receive condition | Payload |
|-------|-------------------|---------|
| `group_update` | Location change / arrival / departure / geofence exit by any group member | [Group Status Object](#group-status-object) |
| `request_gps` | Sent to personal room after location processing completes | `{ "group_id": "...", "next_interval_sec": 18, "gps_mode": "BALANCED" }` |
| `journey_update` | Journey location update complete | [Journey Object](#journey-object) |

---

#### `request_gps` Handling Example

```javascript
socket.on("request_gps", ({ group_id, next_interval_sec, gps_mode }) => {
  // Can configure device GPS accuracy based on gps_mode
  // HIGH: high precision, BALANCED: balanced, LOW: power saving

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

## B. DB Server Integration API (Webhook)

> Endpoints called by the DB server to the GPS server. Not called from the frontend.

**Base URL** `POST /webhook/db-sync`

All requests use the `"type"` field to determine processing method.

---

### B1. Data Cache Push

Called immediately whenever data is created or changed.
The GPS server stores received data in an in-memory cache (TTL 300s) so it **does not directly query the DB** during GPS processing.
The DB is queried as a fallback only when a cache miss occurs.

**All reads the GPS server handles from cache**

| Read purpose | Webhook type | Fallback |
|--------------|-------------|---------|
| Appointment info lookup | `appointment` | Direct DB query |
| Appointment lookup by invite code | `appointment` (auto-indexed when invite_code is included) | Direct DB query |
| Participant list lookup | `participants` / `participant` | Direct DB query |
| Member settings (buffer etc.) lookup | `member_settings` | Direct DB query → latency record-based calculation |
| Personal journey lookup | `journey` / `member_journeys` | Direct DB query |

---

#### Appointment upsert

> If the `invite_code` field is included, the invite code index is automatically updated as well.

```json
{
  "type": "appointment",
  "data": {
    "appointment_id": "550e8400-...",
    "title": "Friday evening appointment",
    "destination_lat": 37.5088,
    "destination_lon": 127.0632,
    "destination_name": "Gangnam Station",
    "destination_address": "396 Underground, Gangnam-daero, Gangnam-gu, Seoul",
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

#### Single Participant upsert

> Called when a participant is added or their information changes.

```json
{
  "type": "participant",
  "data": {
    "participant_id": "part-001",
    "appointment_id": "550e8400-...",
    "member_id": "user_001",
    "name": "Kim Cheolsu",
    "is_host": true,
    "travel_mode": "transit",
    "origin_name": "Home",
    "origin_address": "Seocho-gu, Seoul ...",
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

#### Full Participant List Replacement (Initial Load)

> Used when creating an appointment or synchronizing the entire participant list at once.

```json
{
  "type": "participants",
  "appointment_id": "550e8400-...",
  "data": [
    { /* participant object */ },
    { /* participant object */ }
  ]
}
```

**Response** `200`

```json
{ "cached": "participants", "count": 3 }
```

---

#### Member Settings upsert

> Called when a member is created or their settings change.
> `buffer_minutes` is used in departure alarm time calculation (`appointment_time - ETA - buffer`).

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

#### Single Personal Journey upsert

> Called when a journey is created or changed.

```json
{
  "type": "journey",
  "data": {
    "journey_id": "journey-001",
    "member_id": "user_001",
    "title": "Morning commute",
    "journey_type": "one_way",
    "travel_mode": "transit",
    "origin_lat": 37.4979,
    "origin_lon": 127.0276,
    "origin_name": "Home",
    "origin_address": "Seocho-gu, Seoul ...",
    "dest_lat": 37.5088,
    "dest_lon": 127.0632,
    "dest_name": "Office",
    "dest_address": "Gangnam-gu, Seoul ...",
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

#### Full Member Journey List Replacement

> Used to synchronize a member's entire journey list at once.
> Each journey is also automatically registered in the individual journey cache.

```json
{
  "type": "member_journeys",
  "member_id": "user_001",
  "data": [
    { /* journey object */ },
    { /* journey object */ }
  ]
}
```

**Response** `200`

```json
{ "cached": "member_journeys", "member_id": "user_001", "count": 2 }
```

---

### B2. Geofence Exit Notification

> Called when a participant moves more than **500m** from their departure point.
> The GPS server receives this event, determines that **movement has started**, and begins full tracking.

```json
{
  "type": "geofence_exit",
  "appointment_id": "550e8400-...",
  "member_id": "user_001",
  "current_lat": 37.5010,
  "current_lon": 127.0350
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `appointment_id` | string | ✅ | Appointment ID |
| `member_id` | string | ✅ | Member ID of the departed participant |
| `current_lat` | float | ✅ | Latitude at the time of exit detection |
| `current_lon` | float | ✅ | Longitude at the time of exit detection |

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

**Actions automatically performed by the GPS server**

| Action | Description |
|--------|-------------|
| Full ETA recalculation | Recalculate group-wide ETA reflecting exit coordinates |
| `group_update` emit | Broadcast "this participant has departed" to the entire group room |
| `request_gps` emit (departed participant) | Send `next_interval_sec: 10` (BALANCED) to personal room immediately |
| `request_gps` emit (remaining participants) | Send Adaptive Interval calculated value to each participant |

**GPS interval comparison before/after geofence exit**

| Phase | Condition | GPS interval |
|-------|-----------|--------------|
| Phase 1 (before departure) | Within 500m of departure point | Up to 300s (LOW) |
| Phase 2 (in transit) | Immediately after 500m exit | 10s (BALANCED) → Adaptive afterwards |

---

## C. Reference — Status Value Definitions

### Participant status

| Value | Condition | Meaning |
|-------|-----------|---------|
| `unknown` | No GPS | Location unconfirmed |
| `on_time` | `(appointment_time - current_time) - ETA ≥ 10 min` | Plenty of time |
| `leave_soon` | Buffer 0 ~ 10 min | Must depart soon |
| `hurry` | ETA > remaining time | May be late |
| `late` | Appointment time exceeded | Late |
| `arrived` | Within 100m of destination | Arrived |

### gps_mode

| Value | next_interval range | Battery mode |
|-------|---------------------|-------------|
| `HIGH` | 1 ~ 9s | High precision (within 200m of destination) |
| `BALANCED` | 10 ~ 29s | Balanced |
| `LOW` | 30 ~ 300s | Power saving |

### travel_mode

| Value | Description |
|-------|-------------|
| `walking` | On foot |
| `transit` | Public transit (default) |
| `vehicle` | Car |
