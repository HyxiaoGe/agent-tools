from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelInfo:
    def __init__(self, id: str, name: str, provider: str) -> None:
        self.id = id
        self.name = name
        self.provider = provider


SUPPORTED_MODELS: dict[str, ModelInfo] = {
    "google/gemini-3-pro-image-preview": ModelInfo(
        id="google/gemini-3-pro-image-preview",
        name="Gemini 3 Pro Image",
        provider="Google",
    ),
    "google/gemini-3.1-flash-image-preview": ModelInfo(
        id="google/gemini-3.1-flash-image-preview",
        name="Gemini 3.1 Flash Image",
        provider="Google",
    ),
    "google/gemini-2.5-flash-image": ModelInfo(
        id="google/gemini-2.5-flash-image",
        name="Gemini 2.5 Flash Image",
        provider="Google",
    ),
    "openai/gpt-5-image-mini": ModelInfo(
        id="openai/gpt-5-image-mini",
        name="GPT-5 Image Mini",
        provider="OpenAI",
    ),
    "openai/gpt-5-image": ModelInfo(
        id="openai/gpt-5-image",
        name="GPT-5 Image",
        provider="OpenAI",
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    openrouter_api_key: str = Field(..., description="OpenRouter API key")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    default_model: str = "google/gemini-3-pro-image-preview"
    redis_url: str = "redis://redis:6379/0"
    storage_dir: str = "storage/images"
    static_url_prefix: str = "/static/images"
    host: str = "0.0.0.0"
    port: int = 8090
    service_base_url: str = "http://localhost:8090"  # External URL for generated image links
    api_key: str | None = None  # Optional: protect endpoints with Bearer token
    rate_limit_global: int = 20
    rate_limit_per_ip: int = 5
    rate_limit_window: int = 60
    cache_ttl_seconds: int = 604800  # 7 days
