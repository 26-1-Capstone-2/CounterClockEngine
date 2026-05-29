"""
Manages multiple concurrent events and their CounterClock states.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from geofencing import Coordinate


@dataclass
class Event:
    event_id: str
    title: str
    scheduled_time: datetime
    destination: Coordinate
    location_id: str
    travel_mode: str = "transit"


@dataclass
class EventStatus:
    event: Event
    recommended_departure: datetime
    minutes_until_departure: float
    travel_time_minutes: float
    lateness_buffer_minutes: float
    is_urgent: bool        # departure time is within 10 minutes
    is_overdue: bool       # recommended departure has already passed
    arrived: bool = False

    def summary(self) -> str:
        if self.arrived:
            return f"[{self.event.title}] Arrived"
        if self.is_overdue:
            return (
                f"[{self.event.title}] Departure overdue by {abs(self.minutes_until_departure):.0f} min — move quickly!"
            )
        if self.is_urgent:
            return (
                f"[{self.event.title}] Must depart in {self.minutes_until_departure:.0f} min (urgent)"
            )
        return (
            f"[{self.event.title}] Depart in {self.minutes_until_departure:.0f} min "
            f"(travel {self.travel_time_minutes:.0f} min + buffer {self.lateness_buffer_minutes:.0f} min)"
        )


class EventManager:
    """Stores and retrieves events sorted by urgency."""

    def __init__(self):
        self._events: dict[str, Event] = {}

    def add_event(self, event: Event):
        self._events[event.event_id] = event

    def remove_event(self, event_id: str):
        self._events.pop(event_id, None)

    def get_event(self, event_id: str) -> Optional[Event]:
        return self._events.get(event_id)

    def all_events(self) -> list[Event]:
        return sorted(self._events.values(), key=lambda e: e.scheduled_time)

    def upcoming_events(self, from_time: Optional[datetime] = None) -> list[Event]:
        now = from_time or datetime.now()
        return [e for e in self.all_events() if e.scheduled_time > now]
