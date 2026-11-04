"""给模型的正文（MCP read_page 用）：剥掉 Jina 的包装头、认出没读到正文的、从文章标题起取、截断一次。

REST /read 照旧返回上游原文，Fusion 在自己那边做同样的处理（这里照它的规则）：
- Jina 的包装（Title: / URL Source: / ... / Markdown Content:）只在形状完全对上时才认，正文从包装后开始；
- 去掉包装和图片后没有字的算没读到：包装头里的 Warning 写了验证码、拒绝访问、要登录的是 access_blocked，
  其余 empty_content。只看包装头，不按正文里的词判（讲验证码的文章照样算读到，Fusion d92ee747、18aaaa53）；
- 正文里有和标题一字不差的一级标题时从它开始，跳过页首导航（Fusion url_read_body）；代码块里的不算；
- 截断在这里做一次，8000 字（Fusion d1f0eb26：外层再截一次把截断提示截掉了；提示由调用方放在正文后）。
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass

MAX_CHARS = 8000
_MARKER = re.compile(r"(?im)^Markdown Content:[ \t]*\r?$")
_SOURCE = re.compile(r"(?im)^URL Source:[ \t]*https?://")
_BLOCKED = re.compile(r"(?im)^Warning:.*(?:CAPTCHA|access denied|access blocked|requires? authentication)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_H1 = re.compile(r"^ {0,3}#[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@dataclass
class PageText:
    content: str
    total_chars: int  # 截断前的长度
    truncated: bool


class NoContent(Exception):
    """读回来了，但没有正文。code 是 empty_content 或 access_blocked。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def split_wrapper(content: str) -> tuple[str, str] | None:
    """Jina 的规范包装：返回 (包装头, 正文)；不是这个形状返回 None。"""
    marker = _MARKER.search(content[:4000])
    if marker is None:
        return None
    header = content[: marker.start()]
    if not header.lstrip().startswith("Title:") or not _SOURCE.search(header):
        return None
    return header, content[marker.end() :]


def _normalize_title(title: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", html.unescape(str(title or ""))).split()).casefold()


def article_body(body: str, title: str | None) -> str:
    """从和标题一字不差（忽略大小写、全半角、空白）的一级标题开始；没有就原样。"""
    expected = _normalize_title(title)
    if not expected:
        return body
    offset = 0
    active_fence = ""
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        fence = _FENCE.match(stripped)
        if fence:
            token, suffix = fence.groups()
            if not active_fence:
                active_fence = token
            elif token[0] == active_fence[0] and len(token) >= len(active_fence) and not suffix.strip():
                active_fence = ""
        elif not active_fence:
            heading = _H1.match(stripped)
            if heading and _normalize_title(heading.group(1)) == expected:
                return body[offset:]
        offset += len(line)
    return body


def page_text(content: str, title: str | None) -> PageText:
    """读到的原文变成给模型的正文；没有正文抛 NoContent。"""
    wrapped = split_wrapper(content)
    header = ""
    if wrapped is not None:
        header, content = wrapped
    if not _IMAGE.sub("", content).strip():
        raise NoContent("access_blocked" if header and _BLOCKED.search(header) else "empty_content")
    text = (article_body(content, title) if wrapped is not None else content).strip()
    total = len(text)
    if total > MAX_CHARS:
        return PageText(text[:MAX_CHARS], total, True)
    return PageText(text, total, False)
