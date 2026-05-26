from flask import Flask, jsonify
from dotenv import load_dotenv, find_dotenv

from .config import Config
from .extensions import socketio
from .routes.optimizer import bp as optimizer_bp
from .routes.simulation import bp as simulation_bp
from .routes.kakao import bp as kakao_bp
from .routes.eta import bp as eta_bp
from .routes.appointment import bp as appointment_bp
from .routes.group import bp as group_bp
from .routes.latency import bp as latency_bp
from .routes.journey import bp as journey_bp
from .routes.webhook import bp as webhook_bp
from .routes.personal import bp as personal_bp
from .routes.alarm import bp as alarm_bp


def create_app() -> Flask:
    load_dotenv(find_dotenv())

    app = Flask(__name__)
    app.config.from_object(Config)

    socketio.init_app(app, cors_allowed_origins="*", async_mode="threading")

    # 외부 DB 서버 연결 (DB_BASE_URL 설정 시 활성화)
    if app.config.get("DB_BASE_URL"):
        from .core.db_client import DBClient
        from .core import group as group_core
        from .core import journey as journey_core
        db_client = DBClient(app.config["DB_BASE_URL"])
        group_core.init_db(db_client)
        journey_core.init_db(db_client)

    app.register_blueprint(optimizer_bp,   url_prefix="/api/optimizer")
    app.register_blueprint(simulation_bp,  url_prefix="/api/simulation")
    app.register_blueprint(kakao_bp,       url_prefix="/api/route")
    app.register_blueprint(eta_bp,         url_prefix="/api/eta")
    app.register_blueprint(appointment_bp, url_prefix="/api/appointment")
    app.register_blueprint(group_bp,       url_prefix="/api/group")
    app.register_blueprint(latency_bp,     url_prefix="/api/latency")
    app.register_blueprint(journey_bp,     url_prefix="/api/journey")
    app.register_blueprint(webhook_bp,     url_prefix="/webhook")
    app.register_blueprint(personal_bp,    url_prefix="/api/personal")
    app.register_blueprint(alarm_bp,       url_prefix="/internal/alarm")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        return jsonify({
            "service": "CounterClock GPS Optimizer API",
            "version": "1.1.0",
            "endpoints": {
                "health":     "GET  /health",
                "optimizer":  "POST /api/optimizer/interval",
                "simulation": [
                    "POST /api/simulation/run",
                    "POST /api/simulation/route/generate",
                ],
                "route": [
                    "POST /api/route/fetch",
                    "POST /api/route/simulate",
                    "POST /api/route/departure",
                ],
                "eta": [
                    "POST /api/eta/calculate",
                    "POST /api/eta/movement",
                    "POST /api/eta/track",
                ],
                "appointment": [
                    "POST /api/appointment/register",
                    "POST /api/appointment/departure",
                    "POST /api/appointment/cancel",
                ],
                "group": [
                    "POST   /api/group/create",
                    "POST   /api/group/<id>/join",
                    "POST   /api/group/<id>/location",
                    "GET    /api/group/<id>",
                    "POST   /api/group/<id>/arrive",
                    "DELETE /api/group/<id>/leave",
                ],
                "latency": [
                    "POST /api/latency/record",
                    "GET  /api/latency/buffer?user_id=&confidence=",
                    "GET  /api/latency/history?user_id=",
                ],
                "journey": [
                    "GET  /api/journey/<journey_id>",
                    "GET  /api/journey/member/<member_id>",
                    "POST /api/journey/<journey_id>/location",
                    "POST /api/journey/eta",
                ],
                "websocket": {
                    "join_group":    "emit('join_group', {group_id}) → 그룹 room 입장",
                    "group_update":  "on('group_update', callback) → 그룹 ETA 현황 수신",
                    "join_member":   "emit('join_member', {member_id}) → 개인 room 입장",
                    "request_gps":   "on('request_gps', callback) → GPS 전송 트리거 수신 (next_interval_sec, gps_mode)",
                    "join_journey":  "emit('join_journey', {journey_id}) → 여정 room 입장",
                    "journey_update": "on('journey_update', callback) → 개인 ETA 수신",
                },
                "webhook": [
                    "POST /webhook/db-sync  (type: appointment | participant | participants)",
                ],
                "personal": [
                    "POST /api/personal/departure",
                ],
                "internal": [
                    "POST /internal/alarm/appointment",
                ],
            },
        })

    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"error": str(e)}), 400

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": str(e)}), 404

    @app.errorhandler(500)
    def internal_error(e):
        return jsonify({"error": "Internal server error"}), 500

    return app
