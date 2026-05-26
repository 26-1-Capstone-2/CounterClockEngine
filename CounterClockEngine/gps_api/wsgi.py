"""
WSGI/ASGI entry point.

개발 서버 (SocketIO 내장):
  python -m gps_api.wsgi

프로덕션 (eventlet worker):
  gunicorn "gps_api.wsgi:app" --worker-class eventlet --workers 1 --bind 0.0.0.0:5000
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# .env를 반드시 app 모듈 import 전에 로드해야
# Config 클래스의 os.getenv()가 올바른 값을 읽는다.
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

from gps_api.app import create_app
from gps_api.extensions import socketio

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = app.config.get("DEBUG", False)
    socketio.run(app, host="0.0.0.0", port=port, debug=debug, allow_unsafe_werkzeug=True)
