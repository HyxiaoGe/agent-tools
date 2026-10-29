from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 和风控制台「项目管理」里的五项凭证；缺任一项时服务不对外提供预报（接口返回 503）。
    qweather_api_host: str = ""
    qweather_key_id: str = ""
    qweather_project_id: str = ""
    qweather_developer_id: str = ""
    qweather_private_key: str = ""
    # 单次上游请求超时；调用方（Fusion 工具）自己的总超时更长，由调用方决定放弃。
    qweather_timeout_seconds: float = 8.0

    port: int = 8092
