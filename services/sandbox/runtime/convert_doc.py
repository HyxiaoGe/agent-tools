#!/usr/local/bin/python3
"""convert-doc：把一份 Markdown 或 HTML 转成 PDF、Word（.docx）或单个 HTML 文件。

沙箱里的命令用，镜像里是 /usr/local/bin/convert-doc：

    convert-doc 报告.md outputs/报告.pdf
    convert-doc 报告.md outputs/报告.docx
    convert-doc 页面.html outputs/页面.pdf

排版是固定的（document.css、构建镜像时生成的 reference.docx），同一份文档每次转出来一个样子，不用调用方自己写排版代码：
- Markdown 按聊天页面的写法读：GFM（表格、删除线、任务列表、裸链接），单个换行就是换行，
  $...$、\\(...\\)、单独一行的 \\[ \\] 是公式；不成对的 $ 是钱数（规矩见 _dollars）。
- PDF：pandoc 出 HTML，WeasyPrint 排成 A4。中文用 Noto Sans CJK SC（有粗体），带页码和 PDF 书签。
  公式由 pandoc 转成 MathML，再用 ziamath 画成 SVG（WeasyPrint 不会排 MathML）；
  画不了的照 TeX 原文留着，并在输出里说一声。
- Word：pandoc 直接出，样式来自 reference.docx（见 make_reference_docx.py），公式是 Word 自己的公式，
  表格各列的宽度按内容定（table_widths.lua）。
- 沙箱不联网：文档里引用的网上的图取不到，留下说明文字，并在输出里说一声。本地的图按输入文件所在目录和当前目录找。

给调用方看的话都用英文（会原样给到模型）。出错退出码非 0，标准错误里一句话说清原因。
"""

from __future__ import annotations

import argparse
import base64
import html as html_lib
import logging
import re
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
# gfm：CommonMark + GitHub 的表格、删除线、任务列表、裸链接，和聊天页面的渲染对得上；
# pandoc 自己的 markdown 方言多出来的写法（缩进四格算代码、开头的 --- 算元数据、@ 算引用）会把模型写的普通文字认错。
READER = "gfm+hard_line_breaks+tex_math_dollars"
INPUTS = {".md": "markdown", ".markdown": "markdown", ".txt": "markdown", ".html": "html", ".htm": "html"}
OUTPUTS = (".pdf", ".docx", ".html")
PANDOC_TIMEOUT_S = 100  # 沙箱里一条命令最长 120 秒，自己先停下来才说得清原因
# 公式的字号（像素）：正文 10.5 磅是 14 像素；数学字体 STIX Two 的小写字母比正文的字体矮，放大一成看着才一样大
MATH_PX = 15.5
# 公式里的中文用的字体：ziamath 只认单个的 TrueType 字体，构建镜像时从文泉驿微米黑拆出来（见 make_math_font.py）
MATH_CJK_FONT = HERE / "math-cjk.ttf"
# 出 PDF 时，高亮过的代码超过这么多行就不高亮了：高亮把一行拆成十来段，排版程序每段都要量一次，
# 实测（dev，沙箱里一个核）一行约 20 毫秒，不高亮的 1.5 毫秒。不设上限的话，几千行代码的文档会排到超时。
HIGHLIGHT_MAX_LINES = 1000
# 表格的几档大小：类名、字号（磅）、一格左右留白加一条框线（磅）。数字要和 document.css 里的对上。
TABLE_SIZES = (("", 9.3, 14.6), ("small", 8.0, 8.6), ("smaller", 7.0, 6.6))
PAGE_WIDTH_PT = 481.9  # A4 宽 210 毫米，左右各留 20 毫米
FIRST_COLUMN_EM = 4.6  # 第一列的最小宽度
HEADER_KEEP_EM = 8.0  # 表头不超过这么宽（以字号为单位）就不折行
LONG_TOKEN_CHARS = 20  # 一串断不开的字到这么长，就允许在任意位置断

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_H1 = re.compile(r"^ {0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$")
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_INLINE_CODE = re.compile(r"(`+[^`]*?`+)")
_PAREN_MATH = re.compile(r"\\\((.+?)\\\)")
_BARE_PIPE = re.compile(r"(?<!\\)\|")
_DIGITS = frozenset("0123456789")
_CHECKBOX = re.compile(r'<input type="checkbox"( checked="")? ?/?>')
_MATH = re.compile(r"<math\b.*?</math>", re.S)
_CODE_LINE = re.compile(r'<span id="cb\d+-\d+">')
_CODE_ANCHOR = re.compile(r'<a href="#cb\d+-\d+" aria-hidden="true" tabindex="-1"></a>')
_CODE_GAP = re.compile(r'(?<=</span>)\n(?=<span id="cb\d+-\d+">)')
_TABLE = re.compile(r"<table\b([^>]*)>(.*?)</table>", re.S)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<(t[dh])\b([^>]*)>(.*?)</\1>", re.S)
_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>")
# 一串中间没有空格、也不是中文的字（数字、单词、哈希、网址）：排版时中间没有可以断行的地方
_TOKEN = re.compile(r"[^\s\u2e80-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]+")
# 数学字体里没有的宽字：中日韩文字、全角标点
_WIDE = re.compile(r"([\u2e80-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]+)")


class ConvertError(Exception):
    pass


# ---------- 读 Markdown 之前 ----------


def _outside_fences(lines: list[str]):
    """一行行给出代码块外面的（行号，行）。"""
    fence = ""
    for n, line in enumerate(lines):
        m = _FENCE.match(line)
        if fence:
            # 收尾的那一行：同一种符号、不比开头的短、后面没有别的字
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip().strip(fence[0]):
                fence = ""
            continue
        if m:
            fence = m.group(1)
            continue
        yield n, line


def _dollars(text: str) -> str:
    """一段字里的 $：成对、像公式的留着，别的转义成字面的 $。

    pandoc 读 gfm 时，两个 $ 之间只要两头不是空白就算公式，“$100–$200/月”里的“100–”、
    “Plus（$8）< Pro（$10）”里的“8）< Pro（”都成了公式（2026-11-12 真实模型写的定价报告，PDF 里钱数全花了）。
    聊天页面用的是 pandoc 读自己的 markdown 时的规矩：开头的 $ 后面紧跟非空白，收尾的 $ 前面紧跟非空白、后面不是数字。
    这里和页面（灵稿 frontend/src/markdown.jsx 的 prepLine）一个写法，改一边要改另一边。
    """
    out: list[str] = []
    i, size = 0, len(text)
    while i < size:
        ch = text[i]
        if ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        if text.startswith("$$", i):  # $$ … $$ 写在一行里：原样留着
            end = text.find("$$", i + 2)
            if end > 0:
                out.append(text[i : end + 2])
                i = end + 2
            else:
                out.append("\\$\\$")
                i += 2
            continue
        close = -1
        if i + 1 < size and not text[i + 1].isspace():
            k = i + 1
            while k < size:
                if text[k] == "\\":
                    k += 2
                    continue
                if text[k] == "$":
                    after = text[k + 1 : k + 2]
                    if after != "$" and not text[k - 1].isspace() and after not in _DIGITS:
                        close = k
                    break
                k += 1
        if close > 0:
            out.append(text[i : close + 1])
            i = close + 1
        else:
            out.append("\\$")
            i += 1
    return "".join(out)


def prepare(markdown: str) -> str:
    """把聊天页面认、pandoc 不认的几种写法理成 pandoc 认的（和页面一个规矩，代码块、行内代码、$$ 公式块里不动）：
    \\( … \\) 换成 $ … $；单独一行的 \\[ 和 \\] 换成 $$；不成公式的 $（钱数）转义，见 _dollars；
    表格那一行里，行内代码中的 | 转义（不然一格会被劈成两格）。
    """
    lines = markdown.replace("\r\n", "\n").split("\n")
    block = False  # 在 $$ 公式块里
    for n, line in _outside_fences(lines):
        bare = line.strip()
        if bare in ("\\[", "\\]"):
            lines[n] = line.replace(bare, "$$")
            block = bare == "\\["
            continue
        if bare.startswith("$$") and (bare == "$$" or "$$" not in bare[2:]):
            block = not block
            continue
        if block:
            if bare.endswith("$$"):  # 收尾的 $$ 写在最后一行公式后面
                block = False
            continue
        row = bare.startswith("|")
        parts = _INLINE_CODE.split(line)
        for k, part in enumerate(parts):
            if k % 2:
                parts[k] = _BARE_PIPE.sub(r"\\|", part) if row else part
            else:
                parts[k] = _dollars(_PAREN_MATH.sub(lambda m: f"${m.group(1).strip()}$", part))
        lines[n] = "".join(parts)
    return "\n".join(lines)


def first_heading(markdown: str) -> str:
    """第一个一级标题（不算代码块里的），去掉行内的标记；没有回空串。"""
    for _, line in _outside_fences(markdown.splitlines()):
        h = _H1.match(line)
        if h:
            return re.sub(r"[*_`]", "", h.group(1)).strip()
    return ""


def language(text: str) -> str:
    """文档的语言：有中文算中文。决定 PDF 里的断行、Word 里的校对语言。"""
    return "zh-CN" if _CJK.search(text) else "en"


# ---------- pandoc ----------


def _pandoc(args: list[str], source: str) -> tuple[str, list[str]]:
    """跑一次 pandoc，输入从标准输入给。返回（标准输出，警告）。"""
    try:
        done = subprocess.run(
            ["pandoc", *args], input=source, capture_output=True, text=True, timeout=PANDOC_TIMEOUT_S, check=False
        )
    except FileNotFoundError:
        raise ConvertError("pandoc is not available in this environment") from None
    except subprocess.TimeoutExpired:
        raise ConvertError(f"the conversion took longer than {PANDOC_TIMEOUT_S} seconds; split the document") from None
    warnings = [line.strip() for line in done.stderr.splitlines() if line.strip()]
    if done.returncode != 0:
        detail = " ".join(warnings)[:500] or f"exit code {done.returncode}"
        raise ConvertError(f"pandoc could not read the document: {detail}")
    return done.stdout, warnings


def _common(source: Path, title: str, lang: str) -> list[str]:
    return [
        "--from",
        READER,
        "--metadata",
        f"pagetitle={title}",
        "--metadata",
        f"lang={lang}",
        # 文档里相对路径的图，先按文档所在的目录找，再按当前目录找
        "--resource-path",
        f"{source.parent}:.",
        # 不让 pandoc 按源文件里表格那几行的宽度定各列的比例（那样每列一样宽），列宽交给排版自己算
        "--columns",
        "100000",
    ]


def _unfetched(warnings: list[str]) -> list[str]:
    """pandoc 的警告里取不到的图。"""
    found = [m.group(1).rstrip(":") for line in warnings if (m := re.search(r"Could not fetch resource (\S+)", line))]
    return list(dict.fromkeys(found))


def _unconverted(warnings: list[str]) -> int:
    """pandoc 的警告里转不了的公式有几条（它照 TeX 原文留着）。"""
    return sum(1 for line in warnings if "Could not convert TeX math" in line)


def tick_boxes(html: str) -> str:
    """任务列表的勾选框换成字符：WeasyPrint 不画表单控件。"""
    return _CHECKBOX.sub(lambda m: f'<span class="task-box">{"☑" if m.group(1) else "☐"}</span>', html)


def code_lines(html: str) -> str:
    """高亮过的代码块：pandoc 一行一个 <span>，行首带一个空的锚点，行与行之间是换行符。
    去掉锚点和换行符，样式里把每行排成单独的一块（document.css）。照 pandoc 原样的话每行是一个 inline-block，
    WeasyPrint 排起来慢四倍，比一页长的代码块还分不了页。
    """
    return _CODE_GAP.sub("", _CODE_ANCHOR.sub("", html))


def _width_em(text: str) -> float:
    """一段字大概多宽，以字号为单位：中文、全角符号、表情一个字宽，其余按 0.6（比实际略宽，宁可估多）。"""
    return sum(1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.6 for ch in text)


def _cell_needs(tag: str, inner: str) -> tuple[float, str]:
    """一个格子最窄要多宽（以字号为单位），和它该标的类名。

    中文每个字后面都能断行，最窄一个字；一串不带空格的西文、数字断不开，最窄就是它的宽度。
    断不开的一串太长（long）就允许在任意位置断，不算进最窄宽度。短的表头（keep）整个不折行。
    """
    text = html_lib.unescape(_TAG.sub("", _BR.sub(" ", inner)))
    tokens = _TOKEN.findall(text)
    long = any(len(token) >= LONG_TOKEN_CHARS for token in tokens)
    if tag == "th" and not long:
        whole = _width_em(" ".join(text.split())) * 1.05  # 表头是粗体，略宽
        if whole <= HEADER_KEEP_EM:
            return whole, "keep"
    needs = max((_width_em(token) for token in tokens if len(token) < LONG_TOKEN_CHARS), default=0.0)
    return max(needs, 1.0 if _WIDE.search(text) or long else 0.0), "long" if long else ""


def size_tables(html: str) -> str:
    """给表格和格子标上类名，样式据此定字号和断行（document.css），为的是表格不伸出页面、数字不被拦腰断开：

    - 格子默认只在字与字、词与词之间断行，“1,000”“2024 年”不会被挤成两行；格子里有一长串断不开的字（long）才许任意断。
    - 这样一来列多、数字长的表格可能比页面宽。按上面的规矩估出每列最窄要多宽，正常字号放不下就换小一号（small）、
      再小一号（smaller）；还放不下（cramped）只好让所有格子都能任意断，宁可难看也不能把数据排到页面外面去。
    - 估的是宽度的上限，不是量出来的；表格里有合并的格子时不估，只标 long。
    """

    def table(m: re.Match[str]) -> str:
        attrs, inner = m.groups()
        columns: list[float] = []
        marks: dict[int, str] = {}  # 格子在源码里的位置 → 类名
        spans = "colspan=" in inner or "rowspan=" in inner
        for row in _ROW.finditer(inner):
            for n, cell in enumerate(_CELL.finditer(row.group(1))):
                needs, mark = _cell_needs(cell.group(1), cell.group(3))
                if mark and "class=" not in cell.group(2):
                    marks[row.start(1) + cell.start()] = mark
                if n == len(columns):
                    columns.append(FIRST_COLUMN_EM if n == 0 else 0.0)
                columns[n] = max(columns[n], needs)
        name = ""
        if columns and not spans:
            fits = [
                name
                for name, size, around in TABLE_SIZES
                if sum(width * size + around for width in columns) <= PAGE_WIDTH_PT
            ]
            name = fits[0] if fits else f"{TABLE_SIZES[-1][0]} cramped"
        if spans or "cramped" in name:
            marks = {at: mark for at, mark in marks.items() if mark == "long"}
        for at in sorted(marks, reverse=True):
            cut = at + 3  # “<td”“<th” 后面
            inner = f'{inner[:cut]} class="{marks[at]}"{inner[cut:]}'
        if name and "class=" not in attrs:
            attrs = f' class="{name}"{attrs}'
        return f"<table{attrs}>{inner}</table>"

    return _TABLE.sub(table, html)


def markdown_to_html(
    text: str, source: Path, title: str, lang: str, *, highlight: bool = True, embed: bool = False
) -> tuple[str, list[str]]:
    """Markdown → 一份完整的 HTML（样式写在里面，自己能看；公式是 MathML）。
    embed：把引用的图也写进文件里。交出去的 HTML 要这样（单拿走这一个文件，图还在）；接着排 PDF 的不用，排版时直接读图。
    """
    with tempfile.TemporaryDirectory() as tmp:
        header = Path(tmp) / "style.html"
        css = (HERE / "document.css").read_text(encoding="utf-8")
        header.write_text(f"<style>\n{css}\n</style>\n", encoding="utf-8")
        args = [
            *_common(source, title, lang),
            "--to",
            "html5",
            "--standalone",
            "--template",
            str(HERE / "document.html"),
            "--include-in-header",
            str(header),
            "--mathml",
            *(("--highlight-style", "pygments") if highlight else ("--no-highlight",)),
            *(("--embed-resources",) if embed else ()),
        ]
        html, warnings = _pandoc(args, text)
    return size_tables(code_lines(tick_boxes(html))), warnings


def markdown_to_docx(text: str, source: Path, output: Path, title: str, lang: str) -> list[str]:
    args = [
        *_common(source, title, lang),
        "--to",
        "docx",
        "--reference-doc",
        str(HERE / "reference.docx"),
        # 各列的宽度按内容定好，不留给打开文件的程序去调（缘由见 table_widths.lua）
        "--lua-filter",
        str(HERE / "table_widths.lua"),
        "--output",
        str(output),
    ]
    return _pandoc(args, text)[1]


# ---------- 公式：MathML 画成 SVG ----------


def math_tree(source: str) -> tuple[ET.Element, str, bool]:
    """解析一个 <math>，理成 ziamath 认的样子。返回（树，TeX 原文，里面有没有中文）。

    - 去掉命名空间；pandoc 包在外面的 <semantics> 拆掉，里面标注的 TeX 原文取出来（画不了时用）。
    - 中文：数学字体里没有，挑出来单独放进 <mtext mathvariant="bold">。ziamath 遇到数学字体里没有粗体写法的字，
      会去“粗体备用字体”里找，备用字体指给一份中文字体，中文就画出来了（字重还是那份字体本来的，不会真的变粗）。
    """
    root = ET.fromstring(source)
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]
    tex = ""

    def parents() -> dict[ET.Element, ET.Element]:
        return {child: parent for parent in root.iter() for child in parent}

    up = parents()
    for el in [e for e in root.iter() if e.tag in ("annotation", "annotation-xml")]:
        if el.get("encoding") == "application/x-tex":
            tex = (el.text or "").strip()
        up[el].remove(el)
    for el in [e for e in root.iter() if e.tag == "semantics"]:
        parent = up[el]
        at = list(parent).index(el)
        parent.remove(el)
        for k, child in enumerate(list(el)):
            parent.insert(at + k, child)

    up = parents()
    wide = False
    for el in [e for e in root.iter() if e.tag in ("mtext", "mi") and e.text and _WIDE.search(e.text)]:
        wide = True
        row = ET.Element("mrow")
        variant = el.get("mathvariant") if el.tag == "mtext" else None
        for piece in filter(None, _WIDE.split(el.text or "")):
            sub = ET.SubElement(row, "mtext")
            sub.text = piece
            if _WIDE.fullmatch(piece):
                sub.set("mathvariant", "bold")
            elif variant:
                sub.set("mathvariant", variant)
        parent = up[el]
        parent.insert(list(parent).index(el), row)
        parent.remove(el)
    return root, tex, wide


def _draw(zm, source: str) -> str:
    """一个 <math> → 一个 <img>（SVG 写在地址里）。画不了就抛错。"""
    root, tex, wide = math_tree(source)
    if wide and not MATH_CJK_FONT.is_file():
        raise ConvertError("no CJK font for math")
    default_font = zm.zmath.loadedfonts["default"]
    zm.config.math.bold_font = str(MATH_CJK_FONT) if wide else None
    try:
        math = zm.Math(root, size=MATH_PX)
        svg = math.svg()
        drop = math.getyofst()
    finally:
        # 备用字体挂在共用的数学字体上，用完摘掉：没有中文的公式里，粗体的符号还是用数学字体自己的
        zm.config.math.bold_font = None
        default_font.alt_fonts.bold = None
    data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    alt = html_lib.escape(tex, quote=True)
    # 单独成行的公式标出来，居中由样式管（document.css）；行内的留在字里行间，按基线对齐
    if root.get("display") == "block":
        return f'<img class="math display" alt="{alt}" src="data:image/svg+xml;base64,{data}">'
    return f'<img class="math" alt="{alt}" style="vertical-align:{drop:.2f}px" src="data:image/svg+xml;base64,{data}">'


def draw_math(html: str) -> tuple[str, int]:
    """HTML 里的 MathML 换成 SVG 图。返回（新的 HTML，画不了、照 TeX 原文留着的条数）。"""
    if "<math" not in html:
        return html, 0
    try:
        import ziamath as zm

        zm.config.svg2 = False  # 每个字形直接画成路径，不用 <use>
        zm.config.math.color = "#1f2328"
    except ImportError:
        zm = None
    failed = 0

    def swap(m: re.Match[str]) -> str:
        nonlocal failed
        source = m.group(0)
        try:
            if zm is None:
                raise ConvertError("no math renderer")
            return _draw(zm, source)
        except Exception:  # noqa: BLE001  画不了的原因五花八门，一律退回 TeX 原文
            failed += 1
            tex = re.search(r'<annotation encoding="application/x-tex">(.*?)</annotation>', source, re.S)
            return f'<code class="math-tex">{tex.group(1).strip()}</code>' if tex else source

    return _MATH.sub(swap, html), failed


# ---------- PDF ----------


def html_to_pdf(html: str, base: Path, output: Path) -> tuple[int, list[str], int]:
    """HTML → PDF。返回（页数，取不到的图和样式表，画不了的公式条数）。"""
    try:
        from weasyprint import HTML
        from weasyprint.urls import URLFetcher
    except ImportError:
        raise ConvertError("the PDF engine (weasyprint) is not available in this environment") from None

    root = base.as_uri() + "/"
    here = Path.cwd().as_uri() + "/"

    class Fetcher(URLFetcher):
        """只取本地文件和写在地址里的数据；网上的不去试（沙箱不联网，试了也是白等），取不到的都记下来。
        相对路径的图先按文档所在的目录找，找不到再按当前目录找（和出 Word 时 pandoc 的找法一样）：
        报告放在只读的 uploads/ 里、引用的图在 outputs/ 里是常事。
        """

        def __init__(self) -> None:
            super().__init__(allowed_protocols=("file", "data"))
            self.missing: list[str] = []

        def fetch(self, url, headers=None):
            try:
                return super().fetch(url, headers)
            except Exception:
                if url.startswith(root) and here != root:
                    try:
                        return super().fetch(here + url[len(root) :], headers)
                    except Exception:  # noqa: BLE001  两处都没有，按没取到算
                        pass
                if not url.startswith("data:"):
                    self.missing.append(url)
                raise

    html, failed = draw_math(html)
    fetcher = Fetcher()
    try:
        document = HTML(string=html, base_url=root, url_fetcher=fetcher).render()
        document.write_pdf(str(output))
    except Exception as exc:  # noqa: BLE001  排版库的错原样说不清，只给类型和一句话
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"
        raise ConvertError(f"the document could not be laid out as PDF ({detail})") from None
    missing = [unquote(url[len(root) :]) if url.startswith(root) else url for url in dict.fromkeys(fetcher.missing)]
    return len(document.pages), missing, failed


# ---------- 入口 ----------


def _nearby(source: Path) -> str:
    """输入文件不存在时，顺带说那个目录里有哪些能转的文件：多半是文件名记错了，省得调用方再跑一条命令去看。"""
    try:
        names = sorted(p.name for p in source.parent.iterdir() if p.suffix.lower() in INPUTS and p.is_file())
    except OSError:
        return ""
    if not names:
        return ""
    shown = ", ".join(names[:10]) + (f", and {len(names) - 10} more" if len(names) > 10 else "")
    return f". Files in {source.parent}/ that can be converted: {shown}"


def convert(source: Path, output: Path, title: str | None = None) -> str:
    """转一份。返回给调用方看的话（写了什么、多大、有没有取不到的图、画不了的公式）。"""
    kind = INPUTS.get(source.suffix.lower())
    target = output.suffix.lower()
    if kind is None:
        got = source.suffix or "a file without an extension"
        raise ConvertError(f"cannot read {got}; the input must be Markdown (.md) or HTML (.html)")
    if target not in OUTPUTS:
        got = output.suffix or "a file without an extension"
        raise ConvertError(f"cannot write {got}; the output must end in .pdf, .docx, or .html")
    if kind == "html" and target != ".pdf":
        raise ConvertError("an HTML file can only be converted to .pdf")
    if not source.is_file():
        raise ConvertError(f"{source} does not exist{_nearby(source)}")
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ConvertError(f"{source} is not UTF-8 text") from None
    if not text.strip():
        raise ConvertError(f"{source} is empty")
    output.parent.mkdir(parents=True, exist_ok=True)

    where = source.resolve()
    missing: list[str] = []
    pages = failed = 0
    if kind == "html":
        pages, missing, failed = html_to_pdf(text, where.parent, output)
    else:
        text = prepare(text)
        lang = language(text)
        title = (title or first_heading(text) or source.stem).strip()
        if target == ".docx":
            warnings = markdown_to_docx(text, where, output, title, lang)
            missing, failed = _unfetched(warnings), _unconverted(warnings)
        elif target == ".html":
            html, warnings = markdown_to_html(text, where, title, lang, embed=True)
            missing, failed = _unfetched(warnings), _unconverted(warnings)
            output.write_text(html, encoding="utf-8")
        else:
            html, warnings = markdown_to_html(text, where, title, lang)
            if len(_CODE_LINE.findall(html)) > HIGHLIGHT_MAX_LINES:
                html, warnings = markdown_to_html(text, where, title, lang, highlight=False)
            pages, missing, failed = html_to_pdf(html, where.parent, output)
            failed += _unconverted(warnings)

    size = max(1, round(output.stat().st_size / 1024))
    length = f"{pages} page{'' if pages == 1 else 's'}, " if pages else ""
    notes = [f"Wrote {output} ({length}{size} KB)."]
    if missing:
        shown = ", ".join(missing[:5]) + (f", and {len(missing) - 5} more" if len(missing) > 5 else "")
        notes.append(
            f"Left out because they could not be loaded: {shown}. Web addresses are not reachable from here; "
            "local paths are looked up in the input file's folder, then in the current folder."
        )
    if failed:
        notes.append(f"{failed} formula(s) could not be typeset and appear as TeX source.")
    return "\n".join(notes)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="convert-doc",
        description="Convert a Markdown (.md) or HTML (.html) file to PDF, Word (.docx), or a standalone HTML file. "
        "The output format follows the output file's extension. Layout and fonts are fixed, "
        "so the result looks the same every time.",
    )
    ap.add_argument("input", help="the Markdown or HTML file to convert")
    ap.add_argument("output", help="where to write the result; must end in .pdf, .docx, or .html")
    ap.add_argument("--title", help="document title for the file's metadata (default: the first top-level heading)")
    args = ap.parse_args(argv)
    # 用到的库有的直接往根日志里写提醒；给根日志一个不出声的去处，免得这些话混进给调用方看的输出
    logging.getLogger().addHandler(logging.NullHandler())
    try:
        print(convert(Path(args.input), Path(args.output), args.title))
    except ConvertError as exc:
        print(f"convert-doc: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
