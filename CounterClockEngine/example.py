"""
CounterClock Engine — usage example (no real DB or Maps API needed).
"""

from datetime import datetime, timedelta

from engine import CounterClockEngine
from event_manager import Event
from geofencing import Coordinate, GeofenceZone

# ── Setup ──────────────────────────────────────────────────────────────
engine = CounterClockEngine(user_id="user_001")

# Simulate user's current GPS position (Gangnam-gu, Seoul)
engine.update_position(lat=37.4979, lng=127.0276)

# Register geofence zones (register for each event destination)
engine.geofencing.register_zone(GeofenceZone(
    location_id="loc_school",
    name="School",
    center=Coordinate(lat=37.5665, lng=126.9780),  # Near Seoul City Hall
    radius_meters=150,
))
engine.geofencing.register_zone(GeofenceZone(
    location_id="loc_meeting",
    name="Meeting Place",
    center=Coordinate(lat=37.5172, lng=127.0473),  # Near Samseong-dong
    radius_meters=100,
))

# ── Add events ─────────────────────────────────────────────────────────
now = datetime.now()

engine.add_event(Event(
    event_id="ev_001",
    title="Class Start",
    scheduled_time=now + timedelta(hours=1, minutes=30),
    destination=Coordinate(lat=37.5665, lng=126.9780),
    location_id="loc_school",
    travel_mode="transit",
))

engine.add_event(Event(
    event_id="ev_002",
    title="Team Meeting",
    scheduled_time=now + timedelta(hours=3),
    destination=Coordinate(lat=37.5172, lng=127.0473),
    location_id="loc_meeting",
    travel_mode="transit",
))

# ── Print status ───────────────────────────────────────────────────────
print("=== CounterClock Engine Status ===")
engine.print_status(now=now)

print("\n=== Detailed Info ===")
for status in engine.calculate_all(now=now):
    print(f"\nEvent       : {status.event.title}")
    print(f"Scheduled   : {status.event.scheduled_time.strftime('%H:%M')}")
    print(f"Rec. Depart : {status.recommended_departure.strftime('%H:%M')}")
    print(f"Travel Time : {status.travel_time_minutes:.1f} min")
    print(f"Late Buffer : {status.lateness_buffer_minutes:.1f} min")
    print(f"Until Depart: {status.minutes_until_departure:.1f} min remaining")
