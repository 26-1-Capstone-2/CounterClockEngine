"""
CounterClock Engine — core time reverse-calculation engine.

Flow:
  recommended_departure = event_time - travel_time - lateness_buffer
  time_until_departure   = recommended_departure - now
"""

from datetime import datetime, timedelta
from typing import Optional

from event_manager import Event, EventManager, EventStatus
from geofencing import GeofencingEngine
from latency_tracker import LatencyTracker


class CounterClockEngine:
    """
    Orchestrates latency tracking, geofencing, and multi-event management
    to reverse-calculate when a user must leave for each event.
    """

    URGENT_THRESHOLD_MINUTES = 10

    def __init__(
        self,
        user_id: str,
        db_connector=None,
        maps_api=None,
        lateness_confidence: float = 0.80,
    ):
        self.user_id = user_id
        self.lateness_confidence = lateness_confidence

        self.tracker = LatencyTracker(user_id, db_connector)
        self.geofencing = GeofencingEngine(maps_api)
        self.events = EventManager()

        # Load historical lateness on startup
        self.tracker.load_records()

    # ------------------------------------------------------------------
    # Position updates (called by GPS/WebSocket layer)
    # ------------------------------------------------------------------

    def update_position(self, lat: float, lng: float):
        self.geofencing.update_position(lat, lng)
        self._check_geofence_arrivals()

    def _check_geofence_arrivals(self):
        arrived_ids = self.geofencing.check_arrivals()
        for event in self.events.all_events():
            if event.location_id in arrived_ids:
                self._handle_arrival(event)

    def _handle_arrival(self, event: Event):
        """Record actual arrival time to improve future lateness predictions."""
        from latency_tracker import LatenessRecord
        record = LatenessRecord(
            event_id=event.event_id,
            scheduled_time=event.scheduled_time,
            actual_arrival_time=datetime.now(),
            location_id=event.location_id,
        )
        self.tracker.add_record(record)

    # ------------------------------------------------------------------
    # Core reverse-calculation
    # ------------------------------------------------------------------

    def calculate_departure(
        self,
        event: Event,
        now: Optional[datetime] = None,
    ) -> EventStatus:
        """
        Reverse-calculates the recommended departure time for a single event.
        """
        now = now or datetime.now()

        travel_minutes = self.geofencing.get_travel_time_minutes(
            destination=event.destination,
            mode=event.travel_mode,
        )
        buffer_minutes = self.tracker.recommended_buffer(self.lateness_confidence)

        recommended_departure = event.scheduled_time - timedelta(
            minutes=travel_minutes + buffer_minutes
        )
        minutes_until_departure = (recommended_departure - now).total_seconds() / 60

        return EventStatus(
            event=event,
            recommended_departure=recommended_departure,
            minutes_until_departure=minutes_until_departure,
            travel_time_minutes=travel_minutes,
            lateness_buffer_minutes=buffer_minutes,
            is_urgent=0 < minutes_until_departure <= self.URGENT_THRESHOLD_MINUTES,
            is_overdue=minutes_until_departure < 0,
            arrived=self.geofencing.is_inside_zone(event.location_id),
        )

    def calculate_all(
        self,
        now: Optional[datetime] = None,
    ) -> list[EventStatus]:
        """
        Reverse-calculates departure times for all upcoming events,
        sorted by urgency (most urgent first).
        """
        now = now or datetime.now()
        statuses = []
        for event in self.events.upcoming_events(from_time=now):
            status = self.calculate_departure(event, now=now)
            statuses.append(status)

        # Sort: overdue first, then by minutes_until_departure ascending
        return sorted(statuses, key=lambda s: s.minutes_until_departure)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def add_event(self, event: Event):
        self.events.add_event(event)

    def remove_event(self, event_id: str):
        self.events.remove_event(event_id)

    def print_status(self, now: Optional[datetime] = None):
        statuses = self.calculate_all(now=now)
        if not statuses:
            print("예정된 이벤트가 없습니다.")
            return
        for s in statuses:
            print(s.summary())
