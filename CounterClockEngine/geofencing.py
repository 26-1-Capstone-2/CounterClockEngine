"""
Geofencing module: tracks current position and estimates travel time.
Swap get_travel_time_minutes() with a real Maps API call when ready.
"""

from dataclasses import dataclass
from math import radians, sin, cos, sqrt, atan2
from typing import Optional


@dataclass
class Coordinate:
    lat: float
    lng: float


@dataclass
class GeofenceZone:
    location_id: str
    name: str
    center: Coordinate
    radius_meters: float  # triggers "arrived" when user enters this zone


class GeofencingEngine:
    """
    Calculates travel time between current position and event destination.
    Real-time position updates come from device GPS / WebSocket.
    """

    AVG_SPEED_KMH = 30.0  # default urban speed (walking + transit blended)

    def __init__(self, maps_api=None):
        self.maps_api = maps_api
        self._current_position: Optional[Coordinate] = None
        self._zones: dict[str, GeofenceZone] = {}

    def update_position(self, lat: float, lng: float):
        """Called whenever device sends a GPS update."""
        self._current_position = Coordinate(lat, lng)

    def register_zone(self, zone: GeofenceZone):
        self._zones[zone.location_id] = zone

    def is_inside_zone(self, location_id: str) -> bool:
        """True if current position is within the geofence radius."""
        zone = self._zones.get(location_id)
        if not zone or not self._current_position:
            return False
        dist = self._haversine(self._current_position, zone.center)
        return dist <= zone.radius_meters

    def get_travel_time_minutes(
        self,
        destination: Coordinate,
        mode: str = "transit",
    ) -> float:
        """
        Returns estimated travel time in minutes.
        Uses real Maps API if available, otherwise haversine fallback.
        """
        if not self._current_position:
            raise ValueError("Current position is not set. Call update_position() first.")

        if self.maps_api:
            return self.maps_api.directions(
                origin=self._current_position,
                destination=destination,
                mode=mode,
            ).duration_minutes

        # Fallback: straight-line distance with average speed
        dist_m = self._haversine(self._current_position, destination)
        dist_km = dist_m / 1000
        hours = dist_km / self.AVG_SPEED_KMH
        return hours * 60

    def check_arrivals(self) -> list[str]:
        """Returns location_ids where user has entered the geofence."""
        return [lid for lid in self._zones if self.is_inside_zone(lid)]

    @staticmethod
    def _haversine(a: Coordinate, b: Coordinate) -> float:
        """Great-circle distance in meters."""
        R = 6_371_000
        lat1, lng1 = radians(a.lat), radians(a.lng)
        lat2, lng2 = radians(b.lat), radians(b.lng)
        dlat, dlng = lat2 - lat1, lng2 - lng1
        h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
        return 2 * R * atan2(sqrt(h), sqrt(1 - h))
