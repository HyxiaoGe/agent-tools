"""读页前查网址（MCP read_page 用）：不合法的、不该读的网址，不交给任何上游（直连、Jina、Tavily、Firecrawl）。

REST /read 不走这里，照旧：线上只有 Fusion 用 REST，它调用前自己已经拦了（见 Fusion url_policy）。

不读的：
- 带账号密码的（user:pass@host）；
- 私网、本机、保留地址，包括整数、十六进制、八进制、省略段这些写法的 IPv4（2130706433、0x7f.1、127.1），
  和映射到 IPv4 的 IPv6；
- localhost、.local / .internal / .lan / .home / .localhost 结尾的，和没有点的单段主机名（容器名、内网机器名）；
- 解析出来是私网地址的域名（经 DoH 查，和直连同一个解析器；查不到不拦，见 resolves_to_private）；
- 参数里带凭据的（token、api_key、signature、password、session……，值不为空）。按完整键名认，
  不按子串：Fusion 按子串拦，keyword、author、zipcode 都会误伤。
锚点（# 后面）去掉再读，不交给上游，所以不查：查了反而误伤 /api#token 这种指向“token”一节的链接。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

import direct_read

MAX_URL_CHARS = 4096
DNS_CHECK_TIMEOUT = 2.0
PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home")
# 键名先转小写、- 换成 _ 再比
SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "auth_token",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "id_token",
        "jwt",
        "key",
        "oauth_token",
        "passwd",
        "password",
        "private_token",
        "pwd",
        "refresh_token",
        "secret",
        "session",
        "session_id",
        "sessionid",
        "sid",
        "sig",
        "signature",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
    }
)
_IPV4_LIKE = re.compile(r"^[0-9a-fx.]+$", re.IGNORECASE)


class UrlRejected(ValueError):
    """code 是 invalid_url 或 unsafe_url；message 是英文，给模型看，不含网址本身。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _IPV4_LIKE.fullmatch(host):
            return None
        try:  # inet_aton 认 127.1、0x7f.0.0.1、2130706433 这些浏览器和 curl 也认的写法
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _sensitive_key(query: str) -> str | None:
    for key, _value in parse_qsl(query):  # 空值的不算，没有凭据
        if key.strip().lower().replace("-", "_") in SENSITIVE_KEYS:
            return key
    return None


def check_url(url: str) -> str:
    """能读就返回去掉锚点的网址（锚点不影响读到的页面，里面可能有令牌），不能读抛 UrlRejected。"""
    text = (url or "").strip()
    if not text:
        raise UrlRejected("invalid_url", "the URL is empty")
    if len(text) > MAX_URL_CHARS:
        raise UrlRejected("invalid_url", f"the URL is longer than {MAX_URL_CHARS} characters")
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower().rstrip(".")
        parts.port  # 端口不是数字或越界时抛 ValueError
    except ValueError:
        raise UrlRejected("invalid_url", "the URL is malformed") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise UrlRejected("invalid_url", "only http:// and https:// URLs can be read")
    if not host:
        raise UrlRejected("invalid_url", "the URL has no host name")
    if parts.username is not None or parts.password is not None:
        raise UrlRejected("unsafe_url", "the URL contains a user name or password")
    address = _ip(host)
    if address is not None:
        if not address.is_global:
            raise UrlRejected("unsafe_url", "the URL points to a private, local, or reserved address")
    elif host == "localhost" or host.endswith(PRIVATE_SUFFIXES) or "." not in host:
        raise UrlRejected("unsafe_url", "the URL points to a local or internal host")
    key = _sensitive_key(parts.query)
    if key is not None:
        raise UrlRejected("unsafe_url", f"the URL contains a credential-like parameter ({key})")
    return urlunsplit(parts._replace(fragment=""))


async def resolves_to_private(host: str) -> bool:
    """域名解析出私网地址时返回 True（任一条记录是私网就算）。

    查不到、解析器出错都返回 False，不拦：外部上游本来就连不到 dev 内网，拦它只为不白花钱、不把内网主机名
    交给第三方；直连自己还会再查一遍，查不到就不读（见 direct_read）。IP 写法的主机已经在 check_url 查过。
    """
    if _ip(host) is not None:
        return False
    try:
        async with httpx.AsyncClient(timeout=DNS_CHECK_TIMEOUT) as client:
            await asyncio.wait_for(direct_read._resolve_public_ip(client, host), timeout=DNS_CHECK_TIMEOUT)
    except direct_read.DirectReadSkipped as exc:
        return exc.kind == "private_address"
    except Exception:
        return False
    return False
