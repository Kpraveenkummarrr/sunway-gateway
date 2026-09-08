from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment variables / .env.

    No secrets or gateway-specific values have defaults here beyond safe,
    non-functional placeholders — real values must come from the environment.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    app_url: str = "http://localhost:8000"
    app_secret_key: str = ""

    database_url: str = "postgresql+asyncpg://sunway_user:CHANGE_ME@localhost:5432/sunway_gateway"

    asterisk_host: str = "127.0.0.1"
    asterisk_ami_port: int = 5038
    asterisk_ami_username: str = ""
    asterisk_ami_password: str = ""
    asterisk_ari_port: int = 8088
    asterisk_ari_username: str = ""
    asterisk_ari_password: str = ""

    # SMG4004 — REQUIRES PHYSICAL GATEWAY. Kept as plain placeholders; no
    # behavior in this codebase may assume these are populated or correct.
    smg4004_host: str = ""
    smg4004_sip_port: int = 5060
    smg4004_sip_transport: str = "udp"
    smg4004_username: str = ""
    smg4004_password: str = ""
    smg4004_channel_count: int = 4
    smg4004_dtmf_mode: str = "rfc2833"

    internal_api_key: str = ""

    llm_provider: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    stt_provider: str = ""
    stt_api_key: str = ""

    tts_provider: str = ""
    tts_api_key: str = ""

    rag_embedding_model: str = ""
    rag_top_k: int = 4
    rag_similarity_threshold: float = 0.75
    rag_chunk_size: int = 800
    rag_chunk_overlap: int = 150

    recording_enabled: bool = True
    recording_path: str = "/var/lib/sunway-gateway/recordings"
    recording_retention_days: int = 90

    call_ring_timeout_seconds: int = 25
    ivr_input_timeout_seconds: int = 8
    ivr_max_retries: int = 3

    wireguard_interface: str = "wg0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
