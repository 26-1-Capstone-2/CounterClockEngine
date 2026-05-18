"""
지각 패턴 학습 API

POST /api/latency/record   — 실제 도착 시간 기록 (지각 데이터 누적)
GET  /api/latency/buffer   — 사용자별 personalized 출발 버퍼(분) 조회
GET  /api/latency/history  — 사용자의 지각 기록 목록 조회
"""

from uuid import uuid4

from flask import Blueprint, request, jsonify, abort

from gps_api.core.latency import ArrivalRecord, save_record, recommended_buffer, load_records, record_count

bp = Blueprint("latency", __name__)


@bp.post("/record")
def record():
    """
    실제 도착 시간을 기록합니다.
    누적된 기록이 많을수록 buffer 예측 정확도가 높아집니다.

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
    과거 지각 패턴을 분석해 권장 출발 버퍼(분)를 반환합니다.
    기록이 없으면 기본값 10분을 반환합니다.

    Query params:
      user_id    : 사용자 ID (필수)
      confidence : 신뢰 수준 0.70~0.99 (기본값: 0.80)

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
    사용자의 지각 기록 목록을 반환합니다.

    Query params:
      user_id : 사용자 ID (필수)
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
