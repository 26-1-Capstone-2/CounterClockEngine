#Haversine formula
#input: lat1, lon1, lat2, lon2

from math import radians, sin, cos, sqrt, atan2


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Returns great-circle distance in meters between two GPS coordinates."""
    R = 6_371_000
    lat1, lon1 = radians(lat1), radians(lon1)
    lat2, lon2 = radians(lat2), radians(lon2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return int(2 * R * atan2(sqrt(h), sqrt(1 - h)))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Returns great-circle distance in kilometers between two GPS coordinates."""
    return haversine(lat1, lon1, lat2, lon2) / 1000
