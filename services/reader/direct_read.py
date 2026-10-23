"""
直接抓取网页并提取正文。

Tavily/Jina 返回的是整页 Markdown，导航常占满调用方的正文窗口；这里拿到原始
HTML 后用 trafilatura 提取正文。抓取发生在 dev 内网，容器内 DNS 又被 clash
解析成 198.18.x.x 假地址，无法据此判断目标是否在内网，所以：
- 域名经 DoH 解析出真实地址，全部是公网地址才继续；
- 直连已核验的 IP（TLS 仍按原域名校验证书），不再二次解析，避免解析被换成内网；
- 不自动跟随跳转，每一跳都重新核验。
任何一步不满足都返回 None，由调用方回退 Tavily/Jina。
"""

import asyncio
import ipaddress
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

logger = logging.getLogger("reader_service")

DIRECT_READ_ENABLED = os.getenv("DIRECT_READ_ENABLED", "1") != "0"
# 实测直接抓取 p50 1.2s、p90 3s；超时留给 Tavily/Jina 回退
DIRECT_READ_TIMEOUT = float(os.getenv("DIRECT_READ_TIMEOUT", "4"))
# 正文太短多半是 JS 渲染页或拦截页，交给回退链路
DIRECT_READ_MIN_CHARS = int(os.getenv("DIRECT_READ_MIN_CHARS", "300"))
DIRECT_READ_MAX_BYTES = int(os.getenv("DIRECT_READ_MAX_BYTES", str(3 * 1024 * 1024)))
DIRECT_READ_MAX_REDIRECTS = 5
DOH_URL = os.getenv("DIRECT_READ_DOH_URL", "https://223.5.5.5/resolve")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
_DNS_RECORD_TYPES = {"A": 1, "AAAA": 28}


@dataclass
class DirectPage:
    content: str
    title: str | None


class DirectReadSkipped(Exception):
    """直接读取不适用；kind 只用于日志，不含 URL。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


async def _resolve_public_ip(client: httpx.AsyncClient, host: str) -> str:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise DirectReadSkipped("private_address")
        return str(literal)

    addresses = []
    for record_type, type_code in _DNS_RECORD_TYPES.items():
        response = await client.get(DOH_URL, params={"name": host, "type": record_type})
        response.raise_for_status()
        for answer in response.json().get("Answer") or []:
            if isinstance(answer, dict) and answer.get("type") == type_code:
                try:
                    addresses.append(ipaddress.ip_address(answer.get("data")))
                except ValueError:
                    raise DirectReadSkipped("dns_invalid") from None
    if not addresses:
        raise DirectReadSkipped("dns_unresolved")
    # 任一记录指向非公网就整体放弃，不挑“安全”的那条去连。
    if not all(address.is_global for address in addresses):
        raise DirectReadSkipped("private_address")
    ipv4 = [address for address in addresses if address.version == 4]
    return str((ipv4 or addresses)[0])


async def _fetch_html(client: httpx.AsyncClient, url: str) -> tuple[str, bytes]:
    current = url
    for _ in range(DIRECT_READ_MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        host = parts.hostname
        if parts.scheme not in ("http", "https") or not host:
            raise DirectReadSkipped("unsupported_url")
        if parts.username or parts.password:
            raise DirectReadSkipped("unsupported_url")
        if parts.port not in (None, 80, 443):
            raise DirectReadSkipped("unsupported_port")

        address = await _resolve_public_ip(client, host)
        netloc = f"[{address}]" if ":" in address else address
        host_header = host
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
            host_header = f"{host}:{parts.port}"
        request = client.build_request(
            "GET",
            parts._replace(netloc=netloc, fragment="").geturl(),
            headers={
                "Host": host_header,
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            extensions={"sni_hostname": host} if parts.scheme == "https" else {},
        )
        response = await client.send(request, stream=True)
        try:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise DirectReadSkipped("bad_redirect")
                current = urljoin(current, location)
                continue
            if response.status_code >= 400:
                raise DirectReadSkipped("http_status")
            if "html" not in response.headers.get("content-type", "").lower():
                raise DirectReadSkipped("not_html")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > DIRECT_READ_MAX_BYTES:
                    raise DirectReadSkipped("too_large")
            return current, bytes(body)
        finally:
            await response.aclose()
    raise DirectReadSkipped("too_many_redirects")


def _extract(html: bytes, url: str) -> DirectPage:
    # 交给 trafilatura 按页面声明识别编码，国内站点不少只在 meta 里写 GBK。
    content = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=True,
    )
    if not content or len(content.strip()) < DIRECT_READ_MIN_CHARS:
        raise DirectReadSkipped("extract_too_short")
    # 只取标题：实测日期常被填成当天、站点名常是版权行，不可靠就不给。
    metadata = trafilatura.extract_metadata(html, default_url=url)
    return DirectPage(content=content, title=getattr(metadata, "title", None) or None)


async def read_direct(url: str, domain: str) -> DirectPage | None:
    """成功返回正文；不适用或失败返回 None，调用方据此回退。"""
    if not DIRECT_READ_ENABLED:
        return None
    started_at = time.monotonic()
    kind = "request_error"
    try:
        async with httpx.AsyncClient(timeout=DIRECT_READ_TIMEOUT, follow_redirects=False) as client:
            final_url, html = await asyncio.wait_for(
                _fetch_html(client, url), timeout=DIRECT_READ_TIMEOUT
            )
        # 提取是纯 CPU 计算，放到线程里免得卡住其他请求；体积已被 MAX_BYTES 限制。
        return await asyncio.to_thread(_extract, html, final_url)
    except DirectReadSkipped as exc:
        kind = exc.kind
    except (httpx.TimeoutException, asyncio.TimeoutError):
        kind = "timeout"
    except httpx.HTTPStatusError:
        kind = "dns_error"
    except Exception:
        kind = "request_error"
    logger.warning(
        "reader_direct_skipped domain=%s kind=%s duration_ms=%s",
        domain,
        kind,
        max(0, int((time.monotonic() - started_at) * 1000)),
    )
    return None
