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

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
