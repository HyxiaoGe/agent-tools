"""飞书自定义机器人：日报、每日上限告警共用。webhook 地址本身就是密钥，日志里只记状态码和异常类型。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

import httpx

logger = logging.getLogger("gateway")


def _feishu_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10)


async def send_feishu_text(webhook: str, secret: str, text: str, *, purpose: str) -> bool:
    """发文本消息，飞书返回 code 0 时为 True；secret 非空时按飞书规则签名。"""
    payload: dict = {"msg_type": "text", "content": {"text": text}}
    if secret:
        timestamp = str(int(time.time()))
        signature = hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
        payload["timestamp"] = timestamp
        payload["sign"] = base64.b64encode(signature).decode()
    try:
        async with _feishu_client() as client:
            resp = await client.post(webhook, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("feishu_send_failed: %s %s", purpose, type(exc).__name__)
        return False
    if resp.status_code != 200:
        logger.warning("feishu_send_failed: %s status %s", purpose, resp.status_code)
        return False
    try:
        code = resp.json().get("code", 0)
    except ValueError:
        code = "invalid json"
    if code != 0:
        logger.warning("feishu_send_failed: %s feishu code %s", purpose, code)
        return False
    return True
