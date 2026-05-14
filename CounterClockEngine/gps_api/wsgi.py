"""
WSGI entry point — pass this file to Gunicorn/uWSGI for production deployment.

Example:
  gunicorn "gps_api.wsgi:app" --bind 0.0.0.0:5000 --workers 4
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gps_api.app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=app.config["DEBUG"])
