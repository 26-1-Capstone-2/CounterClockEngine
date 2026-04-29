"""
CounterClock Engine — usage example (no real DB or Maps API needed).
"""

from datetime import datetime, timedelta

from engine import CounterClockEngine
from event_manager import Event
from geofencing import Coordinate, GeofenceZone

# ── Setup ──────────────────────────────────────────────────────────────
engine = CounterClockEngine(user_id="user_001")

# Simulate user's current GPS position (서울 강남구)
engine.update_position(lat=37.4979, lng=127.0276)

# Register geofence zones (이벤트 목적지마다 등록)
engine.geofencing.register_zone(GeofenceZone(
    location_id="loc_school",
    name="학교",
    center=Coordinate(lat=37.5665, lng=126.9780),  # 서울시청 근처
    radius_meters=150,
))
engine.geofencing.register_zone(GeofenceZone(
    location_id="loc_meeting",
    name="회의장소",
    center=Coordinate(lat=37.5172, lng=127.0473),  # 삼성동 근처
    radius_meters=100,
))

# ── Add events ─────────────────────────────────────────────────────────
now = datetime.now()

engine.add_event(Event(
    event_id="ev_001",
    title="수업 시작",
    scheduled_time=now + timedelta(hours=1, minutes=30),
    destination=Coordinate(lat=37.5665, lng=126.9780),
    location_id="loc_school",
    travel_mode="transit",
))

engine.add_event(Event(
    event_id="ev_002",
    title="팀 미팅",
    scheduled_time=now + timedelta(hours=3),
    destination=Coordinate(lat=37.5172, lng=127.0473),
    location_id="loc_meeting",
    travel_mode="transit",
))

# ── Print status ───────────────────────────────────────────────────────
print("=== CounterClock Engine 상태 ===")
engine.print_status(now=now)

print("\n=== 상세 정보 ===")
for status in engine.calculate_all(now=now):
    print(f"\n이벤트  : {status.event.title}")
    print(f"예정 시간: {status.event.scheduled_time.strftime('%H:%M')}")
    print(f"권장 출발: {status.recommended_departure.strftime('%H:%M')}")
    print(f"이동 시간: {status.travel_time_minutes:.1f}분")
    print(f"지각 버퍼: {status.lateness_buffer_minutes:.1f}분")
    print(f"출발까지 : {status.minutes_until_departure:.1f}분 남음")
