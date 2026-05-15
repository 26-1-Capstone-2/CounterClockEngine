from datetime import datetime, timedelta
from uuid import uuid4
from gps_api.core.optimizer import haversine

TRACKING_WINDOW_HOURS = 4
DEFAULT_BUFFER_MIN = 10
DEFAULT_SPEED_MPS = 1.4  # 도보 기본값

# appointment_id -> {"destination": (lat, lon), "appointment_time": datetime}
_store: dict[str, dict] = {}


def register_appointment(
    destination: tuple[float, float],
    appointment_time: datetime,
) -> str:
    appointment_id = str(uuid4())
    _store[appointment_id] = {
        "destination": destination,
        "appointment_time": appointment_time,
    }
    return appointment_id


def cancel_appointment(appointment_id: str) -> bool:
    """Returns True if found and removed, False if not found."""
    return _store.pop(appointment_id, None) is not None


def get_appointment(appointment_id: str) -> dict | None:
    return _store.get(appointment_id)


def compute_departure(
    current_loc: tuple[float, float],
    destination: tuple[float, float],
    appointment_time: datetime,
    speed_mps: float = DEFAULT_SPEED_MPS,
    buffer_min: float = DEFAULT_BUFFER_MIN,
) -> dict:
    distance_m = haversine(current_loc[0], current_loc[1], destination[0], destination[1])
    travel_sec = distance_m / speed_mps
    buffer_sec = buffer_min * 60

    departure_time = appointment_time - timedelta(seconds=travel_sec + buffer_sec)
    now = datetime.now()

    time_until_departure = (departure_time - now).total_seconds()
    time_until_appointment = (appointment_time - now).total_seconds()

    if time_until_appointment < 0:
        status = "late"
    elif time_until_departure < -buffer_sec:
        # 여유 시간까지 소진 → 이미 지각 위기
        status = "critical"
    elif time_until_departure < 0:
        status = "hurry"
    elif time_until_departure < 600:
        status = "leave_soon"
    else:
        status = "on_time"

    tracking_start = appointment_time - timedelta(hours=TRACKING_WINDOW_HOURS)

    return {
        "departure_time": departure_time.isoformat(),
        "time_until_departure_sec": round(time_until_departure),
        "eta_sec": round(travel_sec),
        "eta_min": round(travel_sec / 60, 1),
        "distance_m": round(distance_m, 1),
        "status": status,
        "tracking_active": now >= tracking_start,
        "tracking_start_time": tracking_start.isoformat(),
    }


def get_tracking_info(appointment_time: datetime) -> dict:
    tracking_start = appointment_time - timedelta(hours=TRACKING_WINDOW_HOURS)
    now = datetime.now()
    return {
        "tracking_start_time": tracking_start.isoformat(),
        "tracking_active": now >= tracking_start,
        "seconds_until_tracking": max(0, round((tracking_start - now).total_seconds())),
    }
