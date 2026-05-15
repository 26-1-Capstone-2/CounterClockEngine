from flask import Flask, jsonify
from dotenv import load_dotenv, find_dotenv

from .config import Config
from .routes.optimizer import bp as optimizer_bp
from .routes.simulation import bp as simulation_bp
from .routes.kakao import bp as kakao_bp
from .routes.eta import bp as eta_bp
from .routes.appointment import bp as appointment_bp


def create_app() -> Flask:
    load_dotenv(find_dotenv())

    app = Flask(__name__)
    app.config.from_object(Config)

    app.register_blueprint(optimizer_bp,   url_prefix="/api/optimizer")
    app.register_blueprint(simulation_bp,  url_prefix="/api/simulation")
    app.register_blueprint(kakao_bp,       url_prefix="/api/route")
    app.register_blueprint(eta_bp,         url_prefix="/api/eta")
    app.register_blueprint(appointment_bp, url_prefix="/api/appointment")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        return jsonify({
            "service": "CounterClock GPS Optimizer API",
            "version": "1.0.0",
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
            },
        })

    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"error": str(e)}), 400

    @app.errorhandler(500)
    def internal_error(e):
        return jsonify({"error": "Internal server error"}), 500

    return app
