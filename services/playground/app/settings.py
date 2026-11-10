from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLAYGROUND_", env_file=".env", extra="ignore")

    gateway_url: str = "http://agent-tools-gateway:8100"
    api_key: SecretStr = SecretStr("")
    public_origin: str = ""
    requests_per_minute: int = Field(12, ge=1, le=1000)
    requests_per_day: int = Field(60, ge=1, le=10000)
    images_per_day: int = Field(3, ge=0, le=100)
    site_requests_per_day: int = Field(100, ge=0, le=100000)
    site_images_per_day: int = Field(10, ge=0, le=10000)
    quota_db_path: str = "storage/playground.sqlite3"
    max_concurrent: int = Field(8, ge=1, le=64)
    max_visitors: int = Field(10000, ge=1)

    @field_validator("gateway_url")
    @classmethod
    def validate_gateway(cls, value):
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("网关必须是无账号密码的 HTTP(S) 地址")
        if parts.query or parts.fragment or parts.path not in {"", "/"}:
            raise ValueError("网关地址只包含协议、主机与端口")
        return value.rstrip("/")

    @field_validator("public_origin")
    @classmethod
    def validate_origin(cls, value):
        if not value:
            return value
        return cls.validate_gateway(value)

    @property
    def configured(self):
        return bool(self.api_key.get_secret_value().strip())
