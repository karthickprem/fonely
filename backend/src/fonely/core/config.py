"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Fonely"
    debug: bool = False

    # Sarvam AI
    sarvam_api_key: str = ""
    sarvam_stt_ws_url: str = "wss://api.sarvam.ai/speech-to-text/ws"
    sarvam_tts_url: str = "https://api.sarvam.ai/text-to-speech"
    sarvam_tts_stream_url: str = "https://api.sarvam.ai/text-to-speech/stream"
    sarvam_stt_url: str = "https://api.sarvam.ai/speech-to-text"
    sarvam_llm_url: str = "https://api.sarvam.ai/v1/chat/completions"
    sarvam_tts_model: str = "bulbul:v3"
    sarvam_stt_model: str = "saaras:v3"
    sarvam_llm_model: str = "sarvam-105b"

    # Exotel
    exotel_api_key: str = ""
    exotel_api_token: str = ""
    exotel_sid: str = ""
    exotel_phone_number: str = ""

    # Database — async PostgreSQL in production, async SQLite for tests
    database_url: str = "postgresql+asyncpg://localhost:5432/fonely"

    # Connection pool
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Internal API auth
    internal_api_secret: str = ""

    # Readiness
    readiness_timeout_seconds: float = 3.0

    # Conversation
    conversation_timeout_seconds: float = 30.0

    # Logging
    log_format: str = "text"
    log_level: str = "INFO"

    # Rate limiting
    rate_limit_per_minute: int = 60

    # Request protection
    max_request_body_bytes: int = 1_048_576
    request_timeout_seconds: float = 30.0

    # CORS
    cors_origins: str = ""

    # WhatsApp
    whatsapp_verify_token: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_business_mappings: str = ""
    whatsapp_app_secret: str = ""

    # Shutdown
    shutdown_timeout_seconds: float = 10.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
