# CounterClock — GPS Engine Technical Documentation

CounterClock is a backend engine that tracks the real-time location of each participant in group appointment and personal journey (commute home) scenarios, provides an Adaptive GPS update interval that maximizes battery efficiency, and delivers WebSocket-based real-time ETA broadcasts.

---

## Table of Contents

1. [GPS Update Interval Optimization (Cosine Blend Interval)](#1-gps-update-interval-optimization-cosine-blend-interval)
2. [ETA Calculation Principles](#2-eta-calculation-principles)
3. [ETA Time-Decay Approximation](#3-eta-time-decay-approximation)
4. [Asynchronous Processing](#4-asynchronous-processing)
5. [Overall Data Flow](#5-overall-data-flow)
6. [DB Server Integration (Webhook + Cache)](#6-db-server-integration-webhook--cache)
7. [GPS Trigger — Server-Driven Update Interval](#7-gps-trigger--server-driven-update-interval)
8. [Departure Alarm API (Spring Integration)](#8-departure-alarm-api-spring-integration)
9. [API Endpoints](#9-api-endpoints)

---

## 1. GPS Update Interval Optimization (Cosine Blend Interval)

### Core Idea

> Keeping GPS on all the time drains the battery quickly. How often to check location depends on both **"how close are we to the destination"** and **"how tight is the appointment time"**.

The system automatically calculates "when should GPS be turned on next?" each time. It blends **distance urgency + time urgency** using a Cosine curve to determine the final update interval.

---

### GPS Call Frequency Control Principles

#### Step 1 — Calculate Two Urgency Signals

**Distance urgency (u_dist)**: Approaches 1 as the destination gets closer.

```
u_dist = 1 - clamp(distance_to_destination, 0, 3000m) / 3000m

Example) Distance 150m  → u_dist = 0.95  (very urgent)
         Distance 1500m → u_dist = 0.50  (moderate)
         Distance 3000m+ → u_dist = 0.00  (relaxed)
```

**Time urgency (u_time)**: Approaches 1 as ETA grows large relative to the time remaining until the appointment.

```
u_time = clamp(ETA(sec) / time_remaining_until_appointment(sec), 0, 1)

Example) ETA 20 min, 2 hours until appointment → u_time = 0.17  (relaxed)
         ETA 20 min, 22 min until appointment  → u_time = 0.91  (tight)
```

> If time information is unavailable, `u_time = u_dist` is used as a substitute.

---

#### Step 2 — Blend the Two Signals Using a Cosine Curve

```
urgency = 0.5 × u_dist + 0.5 × u_time        (default: equal blend)

next_interval_base = I_min + 0.5 × (I_max - I_min) × (1 + cos(π × urgency))
                   (I_min = 3s,  I_max = 300s)
```

| urgency | cos(π·urgency) | Calculation                        | Base interval |
|---------|----------------|------------------------------------|--------------|
| 0.0     | +1.0           | 3 + 148.5 × 2.0 = **300s**        | Power saving |
| 0.25    | +0.71          | 3 + 148.5 × 1.71 ≈ **257s**       | Low frequency |
| 0.50    | 0.0            | 3 + 148.5 × 1.0 ≈ **152s**        | Medium |
| 0.75    | −0.71          | 3 + 148.5 × 0.29 ≈ **46s**        | High frequency |
| 1.0     | −1.0           | 3 + 148.5 × 0.0 = **3s**          | Precise tracking |

Unlike the traditional discrete step function (3s/10s/30s/60s), the interval changes **smoothly and continuously** as conditions change.

---

#### Step 3 — Apply Activity Recognition Multiplier

The average speed is estimated from recent location history and multiplied as a scaling factor.

The old discrete step approach (where the multiplier abruptly changes at 0.5 / 8 m/s boundaries) incorrectly applied the walking multiplier to ambiguous speeds like 20 km/h (≈5.6 m/s) such as bicycles or electric scooters. To resolve this, a **continuous function combining two sigmoids** is used to calculate the multiplier.

```
mult(v) = 3.0 − 2.0 × σ(10 · (v − 0.5)) − 0.7 × σ(2 · (v − 8.0))

σ(x) = 1 / (1 + e^−x)   (standard sigmoid)
```

- **First sigmoid** (center 0.5 m/s, steepness 10): Stationary → walking transition. Sharp boundary quickly distinguishes "stopped vs moving".
- **Second sigmoid** (center 8.0 m/s, steepness 2): Walking → vehicle transition. Gradual spread naturally handles the bicycle/scooter range.

| Average speed | Representative mode | Multiplier (old) | Multiplier (sigmoid) |
|--------------|---------------------|-----------------|----------------------|
| 0 m/s        | Stationary          | 3.0             | ≈ 2.99               |
| 1.4 m/s (5 km/h)  | Walking        | 1.0             | ≈ 1.00               |
| 5.6 m/s (20 km/h) | Bicycle        | 1.0 (walking bucket) | ≈ 0.99          |
| 8.0 m/s (boundary)| Fast vehicle entry | 0.3 (abrupt) | ≈ 0.65 (midpoint)  |
| 10 m/s (36 km/h)  | Vehicle        | 0.3             | ≈ 0.31               |
| 15+ m/s           | High-speed vehicle | 0.3         | ≈ 0.30               |

Activity classification labels (stationary / walking / vehicle) are used only for logging and status display; the actual multiplier calculation is handled by this sigmoid function.

---

#### Step 4 — Apply Significant Location Change (SLC) Multiplier

| Condition                          | SLC multiplier | Effect                         |
|------------------------------------|---------------|--------------------------------|
| Moved 500m or more (significant change) | × 1.0   | Continue active tracking       |
| Moved less than 500m + stationary  | × 2.0         | Battery saving (longer interval) |

---

### Final Calculation Formula

```
Next update interval = min(next_interval_base × activity_multiplier × slc_multiplier, 300s)
```

**Calculation Examples:**

| Situation | u_dist | u_time | urgency | base | sigmoid multiplier | Result |
|-----------|--------|--------|---------|------|--------------------|--------|
| Distance 150m, ETA≈appointment time, vehicle (15 m/s) | 0.95 | 0.95 | 0.95 | ~4s | × 0.30 | **1s** |
| Distance 1500m, ETA 20min/appointment 22min, bicycle (5.6 m/s) | 0.50 | 0.91 | 0.71 | ~63s | × 0.99 | **62s** |
| Distance 1500m, ETA 20min/appointment 2 hours, stationary (0 m/s) | 0.50 | 0.17 | 0.33 | ~213s | × 5.97 | **300s** (capped) |

---

### Debug Information in Response

Each signal can be verified in the `debug` field of the `POST /api/optimizer/interval` response.

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

## 2. ETA Calculation Principles

### Distance Calculation — Haversine Formula

Since the Earth is spherical, the actual distance between two coordinates is calculated using **spherical trigonometry** rather than simple Euclidean distance.

```
R = 6,371,000m  (Earth's radius)
a = sin²(Δlat/2) + cos(lat1) × cos(lat2) × sin²(Δlon/2)
distance = R × 2 × atan2(√a, √(1-a))
```

If a Kakao Routes API key is available, ETA is calculated based on the actual road route; otherwise, ETA is estimated using the Haversine straight-line distance and a default travel speed (1.4 m/s).

---

### Movement Detection — Gradient + Momentum

```
gradient = current_position→destination distance - previous_position→destination distance
```

| gradient value | Meaning                                   |
|----------------|-------------------------------------------|
| Negative (-)   | Approaching destination → ETA decreasing |
| Positive (+)   | Moving away from destination → recalculate route |
| Near 0         | Maintain current ETA                      |

Since real GPS signals have noise, **momentum** is used to smooth it out:

```
new_velocity = 0.9 × old_velocity + 0.1 × gradient
```

→ 90% of the previous velocity + 10% of new data smooths sudden changes.

---

### Adaptive Threshold

The ETA recalculation baseline is set dynamically based on the situation.

```
threshold = max(20m, remaining_distance × 5% + |velocity| × 1.5)
```

- The closer the remaining distance, the more sensitive the response.
- The faster the movement, the larger the threshold to prevent unnecessary recalculations.
- A minimum of 20m is guaranteed to prevent over-sensitivity near the destination.

---

### Departure Alarm Time Calculation

```
alarm_time = appointment_time - ETA - personal_buffer
```

The **personal buffer** is statistically derived from the user's past lateness records:

```
buffer = mean of past lateness values + z-score × standard_deviation
(default confidence 80% → z = 0.84)
```

→ Users who are frequently late automatically receive a longer buffer. If no records exist, a default of 10 minutes is used.

---

### Automatic Lateness Record Accumulation

Since the external DB does not provide lateness pattern data, **Flask saves records directly at arrival processing (`POST /api/group/<id>/arrive`)**.

```
POST /api/group/<id>/arrive
    ↓
mark_arrived() runs
    ├─ scheduled_time  = appointment time
    └─ actual_arrival  = current time
         ↓
    lateness = actual_arrival − scheduled_time
    (positive = late, negative = early arrival)
         ↓
    Accumulated in data/latency_{member_id}.json (max 50 records)
```

The more appointments used, the more personalized the buffer becomes.

| Accumulated records | Buffer calculation method |
|---------------------|--------------------------|
| 0 records           | Use default value of 10 minutes |
| 1 record            | Larger of that lateness value or 10 minutes |
| 2+ records          | Mean + 0.84 × standard deviation (80% confidence) |

Buffer priority:

```
Priority 1: buffer_minutes in DB member settings (when user specifies directly in app)
Priority 2: Statistics based on automatically accumulated lateness records
```

---

### Participant Status Classification

| Status     | Condition                                        |
|------------|--------------------------------------------------|
| `arrived`  | Within 100m of destination                       |
| `on_time`  | (appointment_time - current_time) - ETA ≥ 10 min |
| `leave_soon` | Time buffer 0 ~ 10 min                         |
| `hurry`    | ETA exceeds remaining time (may be late)         |
| `late`     | Appointment time has already passed              |

---

## 3. ETA Time-Decay Approximation

### Background — Limitations of the Previous Structure

Previously, ETA was only recalculated when the app sent GPS. If the app was in the background or not sending GPS, the ETA held by the server would remain frozen at the last calculation point.

```
17:00  App sends GPS → ETA = 30 min calculated
17:10  App goes to background, no GPS sent
17:10  GET /api/group/{id} query → still ETA = 30 min (value from 10 minutes ago)  ← problem
```

### Solution — Subtract Elapsed Time at Read Time

Even when no new GPS arrives, **at the moment of serializing the response**, the elapsed time is subtracted from the last calculated ETA to return the current approximate value.

```
decayed_eta = max(0, last_eta_sec − (now − last_updated).total_seconds())
```

```
17:00  App sends GPS → eta_sec = 1800 (30 min), last_updated = "17:00:00"
17:10  GET /api/group/{id} query
       decayed_eta = 1800 − 600 = 1200 (20 min)  ← approximated without API call
       status = _compute_status(1200, ...)         ← re-evaluated based on current time
```

### Design Principles

- **Stored values are not modified.** The `eta_sec` and `last_updated` fields retain the last actual calculated values.
- **Applied only at read time.** Decay is calculated only during `get_group_summary()` and `to_dict()` serialization.
- **Overwritten with accurate values when the next GPS arrives.** Actual calculation results always take precedence.

### Limitations

Traffic changes (congestion, accidents) or route deviations are not reflected. This approach is a first-level correction to **"prevent ETA from freezing at 0 without GPS"**; precise recalculation is performed when the app sends GPS.

---

## 4. Asynchronous Processing

### Problem Scenario

When there are 5 people in a group and one updates their location, the server must **request ETA from the Kakao API for all 5 members**.

```
Sequential processing: 5 members × Kakao API response ~1s = 5s wait → slow user experience
```

---

### Solution 1 — ThreadPoolExecutor (Parallel ETA Calculation)

```python
with ThreadPoolExecutor(max_workers=5) as executor:
    futures = [executor.submit(_fetch_eta, p, ...) for p in participants]
    for f in as_completed(futures):
        f.result()
```

5 threads simultaneously call the Kakao API.

```
Sequential: ─A──B──C──D──E─  (approx. 5 seconds)
Parallel:   ─A─               (approx. 1 second)
            ─B─
            ─C─
            ─D─
            ─E─
```

→ All ETA calculations complete in **about 1 second** regardless of group size.

---

### Solution 2 — WebSocket Real-time Broadcast (Flask-SocketIO)

REST API requires the client to periodically poll the server. This system uses **WebSocket** for the server to push data first.

```
Client                  Server
    │                       │
    │── join_group ─────────▶│  Enter group Room
    │                       │
    │                       │  (someone updates location → parallel ETA recalculation)
    │◀── group_update ───────│  Push to entire group
    │◀── group_update ───────│
    │◀── group_update ───────│
```

```python
# Send to entire group room at once after location update completes
socketio.emit("group_update", summary, room=group_id)
```

→ The moment a location changes, everyone in the group sees **real-time** updates.

---

### async_mode Configuration

```python
socketio.init_app(app, cors_allowed_origins="*", async_mode="threading")
```

| Environment  | Setting                       | Features                                   |
|-------------|-------------------------------|--------------------------------------------|
| Development | `async_mode="threading"`      | Ready to use without additional installation |
| Production  | `eventlet` worker (gunicorn)  | Supports thousands of simultaneous WebSocket connections |

---

## 5. Overall Data Flow

```
[DB Server]
    │
    │  POST /webhook/db-sync  (immediate Push when appointment/participant is created/changed)
    ▼
[Flask Server — In-memory Cache]
    │  _appt_cache / _part_cache  (TTL 300s)
    │
    │  (DB server queried directly only on cache miss)

[Mobile App]
    │
    │  POST /api/group/{id}/location
    │  { user_id, current_loc, kakao_api_key }
    ▼
[Flask Server]
    │
    ├─ Load appointment/participant info from cache (no DB call)
    │
    ├─ ThreadPoolExecutor (parallel)
    │   ├─ Participant A → Kakao API → ETA calculation
    │   ├─ Participant B → Kakao API → ETA calculation
    │   ├─ Participant C → Kakao API → ETA calculation
    │   └─ ... (max 5 simultaneous)
    │
    ├─ Status classification (on_time / leave_soon / hurry / arrived)
    ├─ Alarm time calculation (appointment_time - ETA - personal_buffer)
    ├─ Per-participant next GPS update interval calculation (Adaptive Interval)
    │
    ├─ socketio.emit("group_update", room=group_id)   → Group-wide ETA status
    │
    └─ Per participant: socketio.emit("request_gps", room="member:{id}")
            │    { group_id, next_interval_sec, gps_mode }
            ▼
    [Each client — re-send GPS after next_interval_sec]
```

---

## 6. DB Server Integration (Webhook + Cache)

### Background

The previous structure sent HTTP requests to the DB server to pull appointment/participant information every time GPS was processed. This caused DB call latency to directly translate into GPS processing latency.

### New Structure — Push + Cache

The DB server pushes data first, and the GPS server reads from the cache.

```
Event occurs (appointment created, participant added, etc.)
    │
    DB server → POST /webhook/db-sync
    │
    └─ Saved to GPS server cache (TTL 300s)

GPS processing
    │
    └─ Cache hit → Immediate processing without DB call
    └─ Cache miss → Direct DB server query (fallback)
```

### Webhook Payload Format

```json
// Appointment upsert
{ "type": "appointment", "data": { "appointment_id": "...", ... } }

// Single participant upsert
{ "type": "participant", "data": { "participant_id": "...", "appointment_id": "...", ... } }

// Replace entire participant list for appointment (initial loading)
{ "type": "participants", "appointment_id": "...", "data": [ {...}, {...} ] }

// Geofence exit (departure point 500m+ movement detected)
{
  "type": "geofence_exit",
  "appointment_id": "...",
  "member_id": "...",
  "current_lat": 37.4979,
  "current_lon": 127.0276
}
```

### Geofence Exit Processing Flow

When the frontend sends GPS, the DB server also receives the location. If a participant moves more than 500m from their departure point, the DB server pushes a `geofence_exit` event to the GPS server.

```
[Frontend] GPS sent
    ├─▶ [GPS Server] POST /api/group/<id>/location  (ETA calculation)
    └─▶ [DB Server]  Location saved + 500m geofence check
              │
              When exit detected
              │
              └─▶ [GPS Server] POST /webhook/db-sync { type: "geofence_exit", ... }
                        │
                        ├─ Full group ETA recalculation
                        ├─ socketio.emit("group_update", room=appointment_id)
                        │    → "This participant has departed!" notification to whole group
                        │
                        └─ socketio.emit("request_gps", { next_interval_sec: 10 }, ...)
                             → Immediately switch departed participant to BALANCED (10s) interval
                             → Remaining participants maintain their respective Adaptive Intervals
```

**Two-Phase Tracking Transition Summary**

| Phase | Condition | GPS interval | Description |
|-------|-----------|--------------|-------------|
| Phase 1 | Within 500m of departure point | Up to 300s (LOW) | No movement — battery saving |
| Phase 2 | 500m geofence exit | 10s (BALANCED) → Adaptive | Movement started — full tracking |

---

## 7. GPS Trigger — Server-Driven Update Interval

### Limitations of the Previous Approach

If the frontend sends GPS at a fixed interval or calculates the interval itself, it may not be in sync with the **Adaptive Interval** calculated by the server.

### New Approach — WebSocket `request_gps`

After completing GPS processing, the server emits a `request_gps` event to each participant's personal room. The frontend receives this event and sends GPS after the specified interval (next_interval_sec).

```
Client                                  Server
    │                                    │
    │── join_member({member_id}) ────────▶│  Enter personal room
    │── join_group({group_id})  ─────────▶│  Enter group room
    │                                    │
    │  (someone updates location)        │
    │                                    │── Parallel ETA calculation
    │                                    │── Adaptive Interval calculation
    │                                    │
    │◀── group_update ────────────────────│  Group-wide ETA
    │◀── request_gps ─────────────────────│  { next_interval_sec: 18, gps_mode: "BALANCED" }
    │                                    │
    │   setTimeout(sendGPS, 18_000)      │
    │── POST /api/group/<id>/location ───▶│  GPS sent after 18 seconds
```

### Frontend Implementation Example (JavaScript)

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

### GPS Mode Behavior

`next_interval_sec` is a continuous value calculated by the Cosine Blend algorithm; modes are classified by range.

| gps_mode   | next_interval_sec range | Meaning                                             |
|------------|-------------------------|-----------------------------------------------------|
| `HIGH`     | 1 ~ 9s                  | High urgency — precise tracking (near destination or tight on time) |
| `BALANCED` | 10 ~ 29s                | Medium urgency — in transit                         |
| `LOW`      | 30 ~ 300s               | Low urgency — stationary or plenty of time          |

Clients can use `next_interval_sec` as-is. Since the server automatically adjusts the value based on conditions, no additional client-side logic is needed.

---

## 8. Departure Alarm API (Spring Integration)

When the Spring server sends the target arrival time and current location, Flask calculates the travel time using the route API and returns the **departure alarm time** and **estimated arrival time**.

### Calculation Formula

```
departure_time       = target_time − duration_sec          (must depart at this time to arrive)
departure_alarm_time = departure_time − preparation_time   (alarm is set this much earlier for preparation)
estimated_arrival    = departure_time + duration_sec       (= target_time)
```

### Route API by transport_type

| transport_type | API used | Environment variable |
|---------------|----------|---------------------|
| `DRIVING`     | Kakao Mobility | `KAKAO_API_KEY` |
| `TRANSIT`     | ODsay Public Transit | `ODSAY_API_KEY` |

### Endpoint Comparison

| Endpoint | Purpose | `is_last_mode` |
|----------|---------|----------------|
| `POST /api/personal/departure` | App → Spring → Flask (commute home) | O |
| `POST /internal/alarm/journey` | Spring internal → Flask (personal journey) | O |
| `POST /internal/alarm/appointment` | Spring internal → Flask (group appointment) | None |

### Calculation Formula (Normal Mode)

```
latency_buffer       = recommended_buffer(member_id)   // lateness pattern-based, 10 min at cold-start
total_buffer         = preparation_time + latency_buffer
departure_time       = target_time - duration_sec
departure_alarm_time = departure_time - total_buffer
estimated_arrival    = departure_time + duration_sec
```

`preparation_time` (user preparation time) and `latency_buffer` (lateness pattern correction) are **summed separately**. As lateness records accumulate, `latency_buffer` becomes more personalized.

---

### Last Train Mode (`is_last_mode: "yes"`)

Can only be used when `transport_type` is `TRANSIT`. Automatically searches for the last public transit departure time on that date, calculating the alarm time so the last train is not missed.

#### Last Train Search Principle

Using ODsay API's `SearchDate` + `SearchTime` parameters, a binary search is performed over the **23:00 ~ 01:00 the following day** range. About 8~10 API calls find the last valid departure time with 1-minute precision.

```
23:00 ─────────────────────── Next day 01:00
        Binary search (8~10 calls)
          └─ Last valid departure time determined
```

Times past midnight (00:00~01:00) are automatically handled as the next day's date, so the correct `SearchDate` is passed to ODsay.

#### Alarm Time Decision Rules

The alarm is set based on the **earlier** of the last train departure time and the departure time based on `target_time`.

| Situation | Reference departure time | Reason |
|-----------|--------------------------|--------|
| Last train departure ≤ target_time-based departure | Last train departure time | Cannot return home without catching the last train |
| Last train departure > target_time-based departure | target_time-based departure | Target time is more pressing |

```
departure_alarm_time = min(last_train_departure, normal_departure) − total_buffer
```

#### Error Cases

| Condition | Response |
|-----------|---------|
| `transport_type: "DRIVING"` but `is_last_mode: "yes"` | `400` — Last train is TRANSIT only |
| Last train has already passed | `404` — No valid last train route found |

---

### Request / Response

```json
// Request (Normal mode)
{
  "current_lat": 37.49796,
  "current_lng": 127.02759,
  "dest_lat": 37.51234,
  "dest_lng": 127.05678,
  "transport_type": "TRANSIT",
  "target_time": "2026-05-25T18:00:00",
  "preparation_time": 10,
  "member_id": "user_001"    // optional — lateness pattern personalization
}

// Response (Normal mode)
{
  "departure_alarm_time": "2026-05-25T17:20:00",
  "estimated_arrival": "2026-05-25T18:00:00",
  "latency_buffer_min": 5.2
}

// Request (Last train mode)
{
  "current_lat": 37.49796,
  "current_lng": 127.02759,
  "dest_lat": 37.51234,
  "dest_lng": 127.05678,
  "transport_type": "TRANSIT",
  "is_last_mode": true,
  "target_time": "2026-05-25T23:00:00",  // used as date reference
  "preparation_time": 10,
  "member_id": "user_001"    // optional
}

// Response (Last train mode — last_train_departure field added)
{
  "departure_alarm_time": "2026-05-25T22:15:00",
  "estimated_arrival":    "2026-05-25T23:47:00",
  "latency_buffer_min":   10.0,
  "last_train_departure": "2026-05-25T22:25:00"  // found last train departure time (reference)
}
```

`latency_buffer_min` = 10.0 (cold-start default) when `member_id` is not provided.

---

## 9. API Endpoints

### REST API

**Group Appointments**

| Method  | Path                           | Description                                  |
|---------|-------------------------------|----------------------------------------------|
| `POST`  | `/api/group/create`            | Create group (appointment)                   |
| `POST`  | `/api/group/<id>/join`         | Join group                                   |
| `POST`  | `/api/group/<id>/location`     | Location update + full ETA recalculation     |
| `GET`   | `/api/group/<id>`              | Get group status (with Time-Decay ETA)        |
| `POST`  | `/api/group/<id>/arrive`       | Mark arrival                                 |
| `DELETE`| `/api/group/<id>/leave`        | Leave group                                  |

**Personal Journeys**

| Method  | Path                              | Description                                   |
|---------|----------------------------------|-----------------------------------------------|
| `GET`   | `/api/journey/<id>`              | Get journey (with Time-Decay ETA)              |
| `GET`   | `/api/journey/member/<id>`       | Get member's journey list                     |
| `POST`  | `/api/journey/<id>/location`     | Location update + ETA recalculation           |
| `POST`  | `/api/journey/eta`               | Temporary ETA calculation (no DB save)         |

**Departure Alarms (Spring Integration)**

| Method  | Path                              | Description                                        |
|---------|----------------------------------|----------------------------------------------------|
| `POST`  | `/api/personal/departure`        | Commute home departure alarm (includes is_last_mode) |
| `POST`  | `/internal/alarm/journey`        | Personal journey departure alarm (Spring internal call) |
| `POST`  | `/internal/alarm/appointment`    | Group appointment departure alarm (Spring internal call) |

**Other**

| Method  | Path                           | Description                                |
|---------|-------------------------------|--------------------------------------------|
| `POST`  | `/api/optimizer/interval`      | GPS update interval calculation             |
| `POST`  | `/api/eta/calculate`           | Single ETA calculation                     |
| `POST`  | `/api/latency/record`          | Save lateness record                       |
| `GET`   | `/api/latency/buffer`          | Get personal buffer (departure margin time) |
| `GET`   | `/health`                      | Server health check                        |

### WebSocket Events

| Direction               | Event name       | Data                                               | Description                           |
|------------------------|------------------|----------------------------------------------------|---------------------------------------|
| Client → Server        | `join_group`     | `{ group_id }`                                     | Enter group Room                      |
| Server → Client        | `group_update`   | Group-wide ETA status JSON                         | Auto-push on location change          |
| Client → Server        | `join_member`    | `{ member_id }`                                    | Enter personal Room (for GPS trigger) |
| Server → Client        | `request_gps`    | `{ group_id, next_interval_sec, gps_mode }`        | GPS signal send request               |
| Client → Server        | `join_journey`   | `{ journey_id }`                                   | Enter personal journey Room           |
| Server → Client        | `journey_update` | Personal ETA JSON                                  | Real-time personal journey ETA update |

### Webhook

| Method  | Path                  | Description                                                                           |
|--------|-----------------------|---------------------------------------------------------------------------------------|
| `POST` | `/webhook/db-sync`    | Receive DB server push (`appointment` / `participant` / `participants` / `geofence_exit`) |

---

## Running the Server

### Development Server

```bash
python -m gps_api.wsgi
```

### Production (eventlet)

```bash
gunicorn "gps_api.wsgi:app" --worker-class eventlet --workers 1 --bind 0.0.0.0:5000
```

### Environment Variables

| Variable          | Description                                              | Default |
|-----------------|----------------------------------------------------------|---------|
| `KAKAO_API_KEY` | Kakao Mobility REST API key (DRIVING route)              | None    |
| `ODSAY_API_KEY` | ODsay public transit API key (TRANSIT route)             | None    |
| `DB_BASE_URL`   | External DB server URL (in-memory if not set)            | None    |
| `PORT`          | Server port                                              | 5000    |
| `DEBUG`         | Debug mode                                               | False   |
