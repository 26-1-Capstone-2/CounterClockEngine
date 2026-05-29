"""
DB server → GPS server Webhook receiver endpoint.

The DB server immediately POSTs in the following two situations:
  1. On data creation/modification → cache refresh
  2. When a participant exits the 500m origin geofence → process movement start and return result

Endpoint:
  POST /webhook/db-sync

Processing by type:
  Cache refresh:
    - "appointment"      : upsert appointment info
    - "participant"      : upsert a single participant
    - "participants"     : replace all participants for an appointment
    - "member_settings"  : upsert member settings (buffer, etc.)
    - "journey"          : upsert a single personal journey
    - "member_journeys"  : replace all journeys for a member
  Events:
    - "geofence_exit"    : process movement start → return summary + gps_intervals
                          Spring receives this response and pushes it to the client.
"""

from flask import Blueprint, request, jsonify, abort

from gps_api.core import cache
from gps_api.core import group as group_core

bp = Blueprint("webhook", __name__)


@bp.post("/db-sync")
def db_sync():
    body = request.get_json(silent=True) or {}
    event_type = body.get("type")

    if not event_type:
        abort(400, description="'type' field is required.")

    # ------------------------------------------------------------------
    # Cache refresh events
    # ------------------------------------------------------------------

    data = body.get("data")

    if event_type == "appointment":
        if not isinstance(data, dict):
            abort(400, description="'data' must be an object for type='appointment'.")
        cache.put_appointment(data)
        return jsonify({"cached": "appointment", "id": data.get("appointment_id")}), 200

    if event_type == "participant":
        if not isinstance(data, dict):
            abort(400, description="'data' must be an object for type='participant'.")
        appt_id = data.get("appointment_id")
        if not appt_id:
            abort(400, description="'appointment_id' is required inside participant data.")
        cache.put_participant(appt_id, data)
        return jsonify({"cached": "participant", "id": data.get("participant_id")}), 200

    if event_type == "participants":
        appt_id = body.get("appointment_id")
        if not appt_id:
            abort(400, description="'appointment_id' is required for type='participants'.")
        if not isinstance(data, list):
            abort(400, description="'data' must be an array for type='participants'.")
        cache.put_participants(appt_id, data)
        return jsonify({"cached": "participants", "count": len(data)}), 200

    # ------------------------------------------------------------------
    # Geofence exit event
    # ------------------------------------------------------------------

    if event_type == "geofence_exit":
        for field in ("appointment_id", "member_id", "current_lat", "current_lon"):
            if field not in body:
                abort(400, description=f"'{field}' is required for type='geofence_exit'.")

        appointment_id = body["appointment_id"]
        member_id = body["member_id"]
        try:
            lat = float(body["current_lat"])
            lon = float(body["current_lon"])
        except (TypeError, ValueError):
            abort(400, description="'current_lat' and 'current_lon' must be numbers.")

        result = group_core.handle_geofence_exit(appointment_id, member_id, lat, lon)
        if result is None:
            abort(404, description=f"Appointment '{appointment_id}' or member '{member_id}' not found.")

        summary, gps_intervals = result

        # Spring receives this response and pushes it to clients via WebSocket/FCM.
        # - summary       → broadcast to the entire group (group_update)
        # - gps_intervals → push to each individual member (request_gps)
        return jsonify({
            "triggered": "geofence_exit",
            "appointment_id": appointment_id,
            "member_id": member_id,
            "summary": summary,
            "gps_intervals": gps_intervals,
        }), 200

    # ------------------------------------------------------------------
    # Member settings cache
    # ------------------------------------------------------------------

    if event_type == "member_settings":
        if not isinstance(data, dict):
            abort(400, description="'data' must be an object for type='member_settings'.")
        member_id = data.get("member_id")
        if not member_id:
            abort(400, description="'member_id' is required inside member_settings data.")
        cache.put_member_settings(member_id, data)
        return jsonify({"cached": "member_settings", "member_id": member_id}), 200

    # ------------------------------------------------------------------
    # Personal journey cache
    # ------------------------------------------------------------------

    if event_type == "journey":
        if not isinstance(data, dict):
            abort(400, description="'data' must be an object for type='journey'.")
        cache.put_journey(data)
        return jsonify({"cached": "journey", "id": data.get("journey_id")}), 200

    if event_type == "member_journeys":
        member_id = body.get("member_id")
        if not member_id:
            abort(400, description="'member_id' is required for type='member_journeys'.")
        if not isinstance(data, list):
            abort(400, description="'data' must be an array for type='member_journeys'.")
        cache.put_member_journeys(member_id, data)
        return jsonify({"cached": "member_journeys", "member_id": member_id, "count": len(data)}), 200

    abort(400, description=f"Unknown type '{event_type}'. Must be one of: appointment | participant | participants | member_settings | journey | member_journeys | geofence_exit.")
