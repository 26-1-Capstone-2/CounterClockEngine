"""
Lateness Pattern Learning API

POST /api/latency/record   — Record actual arrival time (accumulate lateness data)
GET  /api/latency/buffer   — Retrieve personalized departure buffer (minutes) per user
GET  /api/latency/history  — Retrieve a user's lateness record history
"""

from uuid import uuid4

from flask import Blueprint, request, jsonify, abort

from gps_api.core.latency import ArrivalRecord, save_record, recommended_buffer, load_records, record_count

bp = Blueprint("latency", __name__)


@bp.post("/record")
def record():
    """
    Records the actual arrival time.
    The more records accumulated, the more accurate the buffer prediction becomes.

    Request JSON:
      {
        "user_id": "user_001",
        "scheduled_time": "2026-05-14T15:00:00",
        "actual_arrival_time": "2026-05-14T15:07:00",
        "location_id": "loc_school",   // optional
        "event_id": "ev_001"           // optional
      }

    Response JSON:
      {
        "recorded": true,
        "lateness_minutes": 7.0,
        "total_records": 5
      }
    """
    body = request.get_json(silent=True) or {}
    for f in ("user_id", "scheduled_time", "actual_arrival_time"):
        if f not in body:
            abort(400, description=f"{f} field is required.")

    rec = ArrivalRecord(
        user_id=body["user_id"],
        event_id=body.get("event_id", str(uuid4())),
        scheduled_time=body["scheduled_time"],
        actual_arrival_time=body["actual_arrival_time"],
        location_id=body.get("location_id", "default"),
    )
    save_record(rec)

    return jsonify({
        "recorded": True,
        "lateness_minutes": round(rec.lateness_minutes, 2),
        "total_records": record_count(body["user_id"]),
    })


@bp.get("/buffer")
def buffer():
    """
    Analyzes past lateness patterns and returns the recommended departure buffer (minutes).
    Returns the default value of 10 minutes if no records exist.

    Query params:
      user_id    : user ID (required)
      confidence : confidence level 0.70~0.99 (default: 0.80)

    Response JSON:
      {
        "user_id": "user_001",
        "buffer_minutes": 8.5,
        "confidence": 0.8,
        "total_records": 12
      }
    """
    user_id = request.args.get("user_id")
    if not user_id:
        abort(400, description="user_id query parameter is required.")

    try:
        confidence = float(request.args.get("confidence", 0.8))
    except ValueError:
        abort(400, description="confidence must be a float between 0 and 1.")

    if not (0 < confidence < 1):
        abort(400, description="confidence must be between 0 and 1 (exclusive).")

    return jsonify({
        "user_id": user_id,
        "buffer_minutes": round(recommended_buffer(user_id, confidence), 2),
        "confidence": confidence,
        "total_records": record_count(user_id),
    })


@bp.get("/history")
def history():
    """
    Returns the lateness record history for a user.

    Query params:
      user_id : user ID (required)
    """
    user_id = request.args.get("user_id")
    if not user_id:
        abort(400, description="user_id query parameter is required.")

    records = load_records(user_id)
    return jsonify({
        "user_id": user_id,
        "total_records": len(records),
        "records": [
            {
                "event_id": r.event_id,
                "scheduled_time": r.scheduled_time,
                "actual_arrival_time": r.actual_arrival_time,
                "location_id": r.location_id,
                "lateness_minutes": round(r.lateness_minutes, 2),
            }
            for r in records
        ],
    })
