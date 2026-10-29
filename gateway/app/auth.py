from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass

from starlette.datastructures import Headers

from app.config import Settings

_USER_PATTERN = re.compile(r"[a-zA-Z0-9_.:@-]{1,128}")
_SCENE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_REQUEST_ID_PATTERN = re.compile(r"[a-zA-Z0-9_.:-]{1,128}")


@dataclass(frozen=True)
class Caller:
    app_id: str
    user: str | None
    scene: str | None
    request_id: str | None


class AuthError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def _same_bearer(header: str, key: str) -> bool:
    return secrets.compare_digest(header.encode("utf-8"), ("Bearer " + key).encode("utf-8"))


def match_product(settings: Settings, authorization: str) -> str | None:
    """按服务密钥认出调用产品；逐个常量时间比较，未配置密钥的产品不可被匹配。"""
    if not authorization.startswith("Bearer "):
        return None
    matches = []
    for app_id, product in settings.products.items():
        key = os.environ.get(product.service_key_env, "").strip()
        if key and _same_bearer(authorization, key):
            matches.append(app_id)
    # 相同密钥误配到多个产品时拒绝，不能依据配置顺序选择产品。
    return matches[0] if len(matches) == 1 else None


def _optional(headers: Headers, name: str, pattern: re.Pattern[str]) -> str | None:
    value = headers.get(name)
    if value is None:
        return None
    if not pattern.fullmatch(value):
        raise AuthError(400, "invalid_caller_header", f"{name} is malformed.")
    return value


def authorize_service(settings: Settings, caller: Caller, service: str) -> None:
    """付费服务按产品放行：不在该产品 services 里的服务一律 403，不转发。"""
    if service not in settings.allowed_services(caller.app_id):
        raise AuthError(403, "service_not_allowed", f"This product is not allowed to call {service}.")


def authenticate(settings: Settings, headers: Headers) -> Caller:
    """产品身份只来自服务密钥；用户、场景、请求 ID 由产品声明，只用于统计与关联。"""
    app_id = match_product(settings, headers.get("authorization", ""))
    if app_id is None:
        raise AuthError(401, "unauthorized", "Request identity could not be accepted.")
    return Caller(
        app_id=app_id,
        user=_optional(headers, "x-caller-user", _USER_PATTERN),
        scene=_optional(headers, "x-caller-scene", _SCENE_PATTERN),
        request_id=_optional(headers, "x-request-id", _REQUEST_ID_PATTERN),
    )
