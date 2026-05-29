import os


class Config:
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-production")
    KAKAO_API_KEY = os.getenv("KAKAO_API_KEY", "")
    ODSAY_API_KEY = os.getenv("ODSAY_API_KEY", "")
    MAX_HISTORY_SIZE = 5
    DEFAULT_STEP_SEC = 10
    # External DB server URL. Falls back to in-memory if not set.
    DB_BASE_URL = os.getenv("DB_BASE_URL", "")
