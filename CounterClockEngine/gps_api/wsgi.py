"""
WSGI/ASGI entry point.

Development server (SocketIO built-in):
  python -m gps_api.wsgi

Production (eventlet worker):
  gunicorn "gps_api.wsgi:app" --worker-class eventlet --workers 1 --bind 0.0.0.0:5000
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# .env must be loaded before importing the app module so that
# Config class's os.getenv() reads the correct values.
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

from gps_api.app import create_app
from gps_api.extensions import socketio

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = app.config.get("DEBUG", False)
    socketio.run(app, host="0.0.0.0", port=port, debug=debug, allow_unsafe_werkzeug=True)
