"""Application configuration loaded from environment variables."""

from __future__ import annotations

from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings
from sqlalchemy import URL


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
    exotel_webhook_secret: str = ""
    exotel_number_mappings: str = ""

    # Database — constructed from components or explicit DATABASE_URL
    database_url: str = "postgresql+asyncpg://localhost:5432/fonely"
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "fonely"
    db_password: str = ""
    db_name: str = "fonely"

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

    @model_validator(mode="before")
    @classmethod
    def _construct_database_url(cls, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("database_url") or data.get("DATABASE_URL"):
            return data
        component_keys = (
            "db_password",
            "DB_PASSWORD",
            "db_host",
            "DB_HOST",
            "db_port",
            "DB_PORT",
            "db_user",
            "DB_USER",
            "db_name",
            "DB_NAME",
        )
        has_components = any(data.get(k) for k in component_keys)
        if not has_components:
            return data
        password = data.get("db_password") or data.get("DB_PASSWORD") or ""
        host = data.get("db_host") or data.get("DB_HOST") or "localhost"
        port = int(data.get("db_port") or data.get("DB_PORT") or 5432)
        user = data.get("db_user") or data.get("DB_USER") or "fonely"
        name = data.get("db_name") or data.get("DB_NAME") or "fonely"
        url = URL.create(
            drivername="postgresql+asyncpg",
            username=str(user),
            password=str(password) if password else None,
            host=str(host),
            port=port,
            database=str(name),
        )
        data["database_url"] = url.render_as_string(hide_password=False)
        return data


settings = Settings()
