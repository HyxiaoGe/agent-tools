"""部署后验证 reader-service 的真实网页读取链路。"""

from __future__ import annotations

import json
import sys
from urllib import error, parse, request

SMOKE_ENDPOINT = "http://127.0.0.1:8091/read"
SMOKE_TARGET = "https://example.com"
SMOKE_TIMEOUT_SECONDS = 22
MAX_RESPONSE_BYTES = 1_000_000
MAX_ERROR_BYTES = 4096


class SmokeFailure(Exception):
    """仅携带允许输出到 CI 日志的安全诊断字段。"""

    def __init__(
        self,
        *,
        http_status: int | str,
        kind: str,
        upstream_status: int | str | None,
    ) -> None:
        super().__init__(kind)
        self.http_status = _safe_status(http_status)
        self.kind = _safe_kind(kind)
        self.upstream_status = _safe_status(upstream_status)


def _safe_status(value: object) -> int | str:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return "unknown"


def _safe_kind(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower().replace("-", "_")
    if normalized and all(char.isalnum() or char == "_" for char in normalized):
        return normalized[:40]
    return "unknown"


def _decode_json(raw: bytes, *, http_status: int | str) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SmokeFailure(
            http_status=http_status,
            kind="invalid_json",
            upstream_status=None,
        ) from None
    if not isinstance(payload, dict):
        raise SmokeFailure(
            http_status=http_status,
            kind="invalid_payload",
            upstream_status=None,
        )
    return payload


def _failure_from_http_error(exc: error.HTTPError) -> SmokeFailure:
    payload: dict = {}
    try:
        decoded = json.loads(exc.read(MAX_ERROR_BYTES).decode("utf-8"))
        if isinstance(decoded, dict):
            payload = decoded
    except Exception:
        pass
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    return SmokeFailure(
        http_status=exc.code,
        kind=detail.get("kind", "http_error"),
        upstream_status=detail.get("upstream_status"),
    )


def run_smoke() -> tuple[int, str]:
    query = parse.urlencode({"url": SMOKE_TARGET})
    try:
        with request.urlopen(
            f"{SMOKE_ENDPOINT}?{query}",
            timeout=SMOKE_TIMEOUT_SECONDS,
        ) as response:
            status = getattr(response, "status", 0)
            raw = response.read(MAX_RESPONSE_BYTES)
    except error.HTTPError as exc:
        raise _failure_from_http_error(exc) from None
    except Exception:
        raise SmokeFailure(
            http_status="unreachable",
            kind="request_error",
            upstream_status=None,
        ) from None

    if status != 200:
        raise SmokeFailure(
            http_status=status,
            kind="unexpected_status",
            upstream_status=None,
        )
    payload = _decode_json(raw, http_status=status)
    content = payload.get("content")
    attempts = payload.get("attempts")
    if not isinstance(content, str) or not content.strip():
        raise SmokeFailure(
            http_status=status,
            kind="empty_content",
            upstream_status=None,
        )
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise SmokeFailure(
            http_status=status,
            kind="invalid_attempts",
            upstream_status=None,
        )
    provider = payload.get("provider")
    return attempts, provider if provider in ("direct", "tavily", "firecrawl", "jina") else "unknown"


def main() -> int:
    try:
        attempts, provider = run_smoke()
    except SmokeFailure as exc:
        print(
            "reader smoke failed "
            f"http_status={exc.http_status} kind={exc.kind} "
            f"upstream_status={exc.upstream_status}",
            file=sys.stderr,
        )
        return 1
    print(
        f"reader smoke passed http_status=200 attempts={attempts} provider={provider}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
