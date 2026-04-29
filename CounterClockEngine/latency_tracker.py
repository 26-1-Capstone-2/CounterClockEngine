"""
Tracks user's historical lateness patterns from DB.
Calculates personalized lateness buffer for departure time prediction.
"""

from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean, stdev
from typing import Optional


@dataclass
class LatenessRecord:
    event_id: str
    scheduled_time: datetime
    actual_arrival_time: datetime
    location_id: str

    @property
    def lateness_minutes(self) -> float:
        """Positive = late, Negative = early."""
        delta = self.actual_arrival_time - self.scheduled_time
        return delta.total_seconds() / 60


class LatencyTracker:
    """
    Computes a personalized lateness buffer from DB records.
    Connect to real DB by replacing load_records().
    """

    def __init__(self, user_id: str, db_connector=None):
        self.user_id = user_id
        self.db = db_connector
        self._records: list[LatenessRecord] = []

    def load_records(self, location_id: Optional[str] = None, limit: int = 30):
        """Load recent lateness records from DB (or mock data if no connector)."""
        if self.db:
            raw = self.db.fetch_lateness_records(
                user_id=self.user_id,
                location_id=location_id,
                limit=limit,
            )
            self._records = [LatenessRecord(**r) for r in raw]
        else:
            self._records = self._mock_records()

    def _mock_records(self) -> list[LatenessRecord]:
        """Placeholder until DB is connected."""
        from datetime import timedelta
        base = datetime(2026, 1, 1, 9, 0)
        lateness_minutes = [5, 3, 7, 2, 10, 4, 6, 8, 1, 5]
        records = []
        for i, late in enumerate(lateness_minutes):
            scheduled = base.replace(day=i + 1)
            records.append(LatenessRecord(
                event_id=f"mock_{i}",
                scheduled_time=scheduled,
                actual_arrival_time=scheduled + timedelta(minutes=late),
                location_id="default",
            ))
        return records

    def average_lateness(self) -> float:
        """Average minutes late across loaded records."""
        if not self._records:
            return 0.0
        return mean(r.lateness_minutes for r in self._records)

    def recommended_buffer(self, confidence: float = 0.8) -> float:
        """
        Returns buffer minutes to add so the user arrives on time with
        `confidence` probability (based on lateness distribution).
        confidence=0.8 means covers 80% of past cases.
        """
        if len(self._records) < 2:
            return self.average_lateness()

        values = [r.lateness_minutes for r in self._records]
        avg = mean(values)
        sd = stdev(values)

        # z-scores for common confidence levels
        z_table = {0.70: 0.52, 0.80: 0.84, 0.90: 1.28, 0.95: 1.65, 0.99: 2.33}
        z = z_table.get(confidence, 0.84)
        return max(0.0, avg + z * sd)

    def add_record(self, record: LatenessRecord):
        """Append a new record (e.g., after an event completes)."""
        self._records.append(record)
        if self.db:
            self.db.insert_lateness_record(self.user_id, record)
