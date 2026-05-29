"""
Saves and retrieves per-user lateness patterns as JSON files.
As past records accumulate, a personalized departure buffer is computed.
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Optional
from uuid import uuid4

DATA_DIR = Path(__file__).parent.parent / "data"
DEFAULT_BUFFER_MIN = 10.0
MAX_RECORDS = 50


@dataclass
class ArrivalRecord:
    user_id: str
    event_id: str
    scheduled_time: str   # ISO 8601
    actual_arrival_time: str  # ISO 8601
    location_id: str

    @property
    def lateness_minutes(self) -> float:
        """Positive = late, negative = arrived early."""
        scheduled = datetime.fromisoformat(self.scheduled_time)
        actual = datetime.fromisoformat(self.actual_arrival_time)
        return (actual - scheduled).total_seconds() / 60


def _path(user_id: str) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    return DATA_DIR / f"latency_{user_id}.json"


def load_records(user_id: str) -> list[ArrivalRecord]:
    path = _path(user_id)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [ArrivalRecord(**r) for r in json.load(f)]


def save_record(record: ArrivalRecord) -> None:
    records = load_records(record.user_id)
    records.append(record)
    records = records[-MAX_RECORDS:]
    with open(_path(record.user_id), "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in records], f, ensure_ascii=False, indent=2)


def recommended_buffer(user_id: str, confidence: float = 0.80) -> float:
    """
    Returns the confidence percentile of the past lateness record distribution as the departure buffer.
    Returns DEFAULT_BUFFER_MIN if there are no records.
    """
    records = load_records(user_id)
    if not records:
        return DEFAULT_BUFFER_MIN

    values = [r.lateness_minutes for r in records]
    if len(values) < 2:
        return max(DEFAULT_BUFFER_MIN, values[0])

    avg = mean(values)
    sd = stdev(values)
    z_table = {0.70: 0.52, 0.80: 0.84, 0.90: 1.28, 0.95: 1.65, 0.99: 2.33}
    z = z_table.get(confidence, 0.84)
    return max(0.0, avg + z * sd)


def record_count(user_id: str) -> int:
    return len(load_records(user_id))
