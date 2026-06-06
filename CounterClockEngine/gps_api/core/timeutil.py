"""
Timezone helpers.

Clients of the alarm endpoints send and expect wall-clock Korea Standard Time
(KST, UTC+9), but the server may run in GMT/UTC. To keep "now" consistent with
the naive KST datetimes parsed from request bodies, evaluate the current time in
KST everywhere those values are compared or combined.
"""

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """Current KST wall-clock time as a naive datetime.

    Returned naive so it can be mixed directly with the naive KST datetimes
    produced by datetime.fromisoformat() on client-supplied target_time values.
    """
    return datetime.now(KST).replace(tzinfo=None)
