"""直接读取时保住页面里的代码块（direct_read._extract 在 trafilatura 提取前后调用）。

trafilatura 按 class 去噪，文档站包代码块的那几层 div 常被当成噪声，整块代码扔掉（2026-11-05 实测 2.3.1，
也是 PyPI 上最新版）：
- class 里含 bar 的当导航栏：Mintlify（docs.langchain.com 等）的滚动区是 base-ui-disable-scrollbar，
  一页 27 段代码一段不剩；
- 含 button 的当按钮：Next.js 文档的代码块外层；
- 列表项里的代码被当成普通文字，换行和缩进没了、下划线被转义（docs.python.org）。
所以提取前把每段代码连同只包着它的那几层换成一个占位，提取完再换回 Markdown 代码块，代码不经过 trafilatura。
包着它的那几层里可以有一行短的标签（文件名，比如 Next.js 的 app/page.tsx），放在代码块上面一行。
占位本身也是 <pre><code>，后面补上和代码一样长的 0：trafilatura 按一块里字的多少、链接占多少判断留不留，
占位太短时，带“Fork”链接的可编辑示例（react.dev）整块被当成链接列表去掉了，补到和代码一样长就和原来的判断一样。
占位所在的那块要是被 trafilatura 当成噪声去掉了（侧栏、页脚里的代码），代码也跟着不要，和以前一样。

标签页（role=tablist / tab / tabpanel）：同一段代码的几种写法（Decorator / Class、TypeScript / JavaScript）
各在一页，没选中的那几页带着 hidden，trafilatura 也会去掉。这里把每页都显示出来、页首加一行标签名，
那一排标签去掉（不然只剩一串标签名）。
"""

from __future__ import annotations

import re

from lxml import etree

MARK = "AGENTTOOLSCODE{}X"  # 后面补 0，见模块说明
# 占位在提取结果里：trafilatura 把只有一行的 <pre><code> 写成行内代码（`占位`），也可能是只有它的代码块；
# 在列表项里被压成一行时是行里的一个词
_MARK_IN_TEXT = re.compile(
    r"\n*[ \t]*(?:`{3,}[^\n`]*\n[ \t]*|`)?AGENTTOOLSCODE(\d+)X0*(?:[ \t]*\n[ \t]*`{3,}|`)?[ \t]*\n*"
)
# 往上换掉的只能是这几种包装；碰到别的（列表项、表格、正文段落）就停，占位留在原处
_WRAPPER_TAGS = {"div", "span", "figure"}
# 包装里代码以外的字：按钮、链接上的（复制、Fork、在沙盒里打开）不算；最多一行这么长的标签（文件名、标题）
_NOT_TEXT = {"button", "a", "script", "style", "svg", "template", "noscript"}
_CAPTION_MAX = 80
# 包装里有这些就是正文了，不往上换（不然正文被压成一行标签，里面的链接、行内代码都没了）
_PROSE_TAGS = {"p", "ul", "ol", "li", "table", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
# 代码里按行分开的写法：每行一个 div（CodeMirror、Prism）或 <br>
_LINE_TAGS = {"div", "p", "li", "tr"}
# 代码里的行号（Pygments 的 linenos、Prism 的 line-numbers）：不是代码
_GUTTER = re.compile(r"^(?:line-?numbers?(?:-rows)?|linenos?|lineno|gutter)$", re.I)
_LANGUAGE_CLASS = re.compile(r"^(?:language|lang)-([\w+#.-]{1,20})$")
_LANGUAGE_ATTRS = ("language", "data-language", "data-lang")
_LANGUAGE = re.compile(r"^[\w+#.-]{1,20}$")
_HIDDEN_CLASS = re.compile(r"hidden|hide", re.I)


def _visible_text(el) -> str:
    return " ".join(el.text_content().split())


def _is_gutter(el) -> bool:
    return any(_GUTTER.match(c) for c in (el.get("class") or "").split())


def code_text(pre) -> str:
    """<pre> 里的代码：<br> 和每行一个的 div 换成换行，行号不要，&nbsp; 当空格。"""
    parts: list[str] = []

    def walk(el) -> None:
        if not isinstance(el.tag, str):  # 注释
            pass
        elif el.tag == "br":
            parts.append("\n")
        elif not _is_gutter(el):
            if el.text:
                parts.append(el.text)
            for child in el:
                walk(child)
            # 每行一个 div 时，有的高亮器在 div 之间还留着换行，这时不再补
            if el.tag in _LINE_TAGS and parts and not parts[-1].endswith("\n") and not (el.tail or "").startswith("\n"):
                parts.append("\n")
        if el is not pre and el.tail:
            parts.append(el.tail)

    walk(pre)
    return "".join(parts).replace("\xa0", " ").strip("\n")


def _language(chain: list) -> str:
    for el in chain:
        for attr in _LANGUAGE_ATTRS:
            value = (el.get(attr) or "").strip()
            if _LANGUAGE.match(value):
                return value
        for cls in (el.get("class") or "").split():
            if m := _LANGUAGE_CLASS.match(cls):
                return m.group(1)
    return ""


def _remove(el) -> None:
    """去掉一个元素，它后面紧跟的文字留着。"""
    parent = el.getparent()
    if el.tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def show_tab_panels(tree) -> None:
    """标签页的每一页都显示出来，有内容的页首加一行标签名；标签都对得上页的那一排标签去掉。

    只渲染选中那一页的站（Next.js 文档、Anthropic 文档）别的页是空的，不加标签名，不然只剩一个孤零零的“JavaScript:”。"""
    panels = {p.get("id"): p for p in tree.iterfind('.//*[@role="tabpanel"]') if p.get("id")}
    for panel in tree.iterfind('.//*[@role="tabpanel"]'):
        for attr in ("hidden", "aria-hidden", "style"):
            panel.attrib.pop(attr, None)
        if cls := panel.get("class"):
            panel.set("class", " ".join(c for c in cls.split() if not _HIDDEN_CLASS.search(c)))
    for tablist in list(tree.iterfind('.//*[@role="tablist"]')):
        tabs = tablist.findall('.//*[@role="tab"]')
        pairs = [(tab, panels[tab.get("aria-controls")]) for tab in tabs if tab.get("aria-controls") in panels]
        if not tabs or len(pairs) != len(tabs):
            continue  # 对不上的不动：只留一串标签名也比把标签弄丢强
        for tab, panel in pairs:
            if (label := _visible_text(tab)) and _visible_text(panel):
                heading = etree.Element("p")
                heading.text = f"{label}:"
                panel.insert(0, heading)
        _remove(tablist)


class _Prose(Exception):
    pass


def _text_beside(wrapper, pre) -> str | None:
    """wrapper 里除了这段代码和按钮、链接以外的字；有段落、列表这些正文时返回 None。"""
    parts: list[str] = []

    def walk(el) -> None:
        if el is not pre and isinstance(el.tag, str) and el.tag not in _NOT_TEXT:
            if el.tag in _PROSE_TAGS and el.text_content().strip():
                raise _Prose
            if el.text:
                parts.append(el.text)
            for child in el:
                walk(child)
        if el is not wrapper and el.tail:
            parts.append(el.tail)

    try:
        walk(wrapper)
    except _Prose:
        return None
    return " ".join(" ".join(parts).split())


def take_code_blocks(tree) -> list[tuple[str, str, str]]:
    """每段代码换成一个占位，返回 (语言, 标签, 代码)；第 i 段的占位是 MARK.format(i) 加上和代码一样长的 0。

    往上连同只包着这段代码（复制按钮、滚动条、折叠这些）的 div、span、figure 一起换掉，中间可以有一行短的标签；
    到标签页、列表项、正文段落、还包着别的代码的这些为止。"""
    blocks: list[tuple[str, str, str]] = []
    for pre in tree.xpath("//pre[not(ancestor::pre)]"):
        text = code_text(pre)
        if not text.strip():
            continue
        chain = [pre, *pre.findall("code")]
        target, caption = pre, ""
        for parent in pre.iterancestors():
            if parent.tag not in _WRAPPER_TAGS or parent.get("role") == "tabpanel" or parent.getparent() is None:
                break
            if len(parent.xpath(".//pre")) > 1:
                break
            beside = _text_beside(parent, pre)
            if beside is None:
                break
            if beside != caption:
                if caption or len(beside) > _CAPTION_MAX:
                    break
                caption = beside
            target = parent
            chain.append(parent)
        mark = etree.Element("pre")
        etree.SubElement(mark, "code").text = MARK.format(len(blocks)) + "0" * len(text)
        mark.tail = target.tail
        target.getparent().replace(target, mark)
        blocks.append((_language(chain), caption, text))
    return blocks


def put_back(content: str, blocks: list[tuple[str, str, str]]) -> str:
    """提取结果里的占位换回 Markdown 代码块，前后各空一行；有标签的标签放在上面一行（和语言一样的不重复写）。
    围栏比代码里最长的一串反引号多一个。"""

    def block(m: re.Match) -> str:
        index = int(m.group(1))
        if index >= len(blocks):
            return m.group(0)
        language, caption, text = blocks[index]
        longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
        fence = "`" * max(3, longest + 1)
        head = f"{caption}\n" if caption and caption.lower() != language.lower() else ""
        # 前后先记成 \0（lxml 里的字不会有它），挨着的两段代码中间只空一行
        return f"\0{head}{fence}{language}\n{text}\n{fence}\0"

    return re.sub("\0+", "\n\n", _MARK_IN_TEXT.sub(block, content)).strip("\n")
