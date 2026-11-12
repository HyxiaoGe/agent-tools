"""convert-doc 里不靠 pandoc、排版程序的那部分：写法的整理、表格宽度的估算、公式的预处理、参数检查。

真的转一份文档要镜像里的 pandoc、字体和 WeasyPrint，这台机器上没有，放在 scripts/smoke_sandbox.py 里真跑。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "runtime" / "convert_doc.py"
_spec = importlib.util.spec_from_file_location("convert_doc", SCRIPT)
assert _spec and _spec.loader
convert_doc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(convert_doc)


# ---------- 读 Markdown 之前 ----------


def test_prepare_turns_paren_math_into_dollars():
    assert (
        convert_doc.prepare(r"勾股定理 \( a^2 + b^2 = c^2 \)，以及 \(x\)。") == "勾股定理 $a^2 + b^2 = c^2$，以及 $x$。"
    )


def test_prepare_turns_lone_brackets_into_display_math():
    assert convert_doc.prepare("前\n\\[\nx = 1\n\\]\n后") == "前\n$$\nx = 1\n$$\n后"


def test_prepare_leaves_code_alone():
    fenced = "```\n\\(x\\)\n\\[\n| `a|b` |\n```"
    assert convert_doc.prepare(fenced) == fenced
    tilde = "~~~python\n\\(x\\)\n~~~"
    assert convert_doc.prepare(tilde) == tilde
    assert convert_doc.prepare(r"行内代码 `\(x\)` 不动，外面的 \(y\) 换") == r"行内代码 `\(x\)` 不动，外面的 $y$ 换"


def test_prepare_fence_closes_only_on_a_matching_line():
    # 代码块里比开头短的一排反引号、带别的字的一行都不算收尾
    text = "````\n```\n\\(x\\)\n``` 不是收尾\n````\n\\(y\\)"
    assert convert_doc.prepare(text) == "````\n```\n\\(x\\)\n``` 不是收尾\n````\n$y$"


def test_prepare_escapes_pipes_inside_code_in_table_rows():
    assert convert_doc.prepare("| 写法 | `a | b` | `x || y` |") == r"| 写法 | `a \| b` | `x \|\| y` |"
    # 已经转义过的不再加；不是表格的那一行不动
    assert convert_doc.prepare(r"| `a \| b` |") == r"| `a \| b` |"
    assert convert_doc.prepare("命令是 `a | b`") == "命令是 `a | b`"


def test_prepare_normalizes_windows_line_endings():
    assert convert_doc.prepare("a\r\nb") == "a\nb"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 钱数：两个 $ 之间不成公式，都转义（2026-11-12 真实报告里的三句）
        ("Max 档另附 $100–$200/月 API 额度 [2]。", r"Max 档另附 \$100–\$200/月 API 额度 [2]。"),
        (
            "Plus（$8）< Copilot Pro（$10）< Claude Pro（$17–20）。",
            r"Plus（\$8）< Copilot Pro（\$10）< Claude Pro（\$17–20）。",
        ),
        (
            "| 入门付费 | Plus $8/月 | Pro $20/月（$17 年付） |",
            r"| 入门付费 | Plus \$8/月 | Pro \$20/月（\$17 年付） |",
        ),
        ("从 $5 涨到 $10", r"从 \$5 涨到 \$10"),
        ("一共 $5", r"一共 \$5"),
        # 公式：留着
        ("质能方程 $E=mc^2$ 不变", "质能方程 $E=mc^2$ 不变"),
        ("$x$ 和 $y$", "$x$ 和 $y$"),
        ("结尾是公式 $a+b$", "结尾是公式 $a+b$"),
        (r"集合 $\{a\}$，带钱的公式 $p = \$5$", r"集合 $\{a\}$，带钱的公式 $p = \$5$"),
        # 钱数和公式在一行
        ("价格 $20，公式 $a+b$", r"价格 \$20，公式 $a+b$"),
        # 开头的 $ 后面是空白、收尾的 $ 前面是空白：不是公式
        ("a $ b $ c", r"a \$ b \$ c"),
        ("$a $", r"\$a \$"),
        # 已经转义的不再动；$$ 写在一行里的原样留着，落单的转义
        (r"已经写成 \$5 的", r"已经写成 \$5 的"),
        ("行内的 $$x^2$$ 留着", "行内的 $$x^2$$ 留着"),
        ("落单的 $$ 两个", r"落单的 \$\$ 两个"),
        # 行内代码里不动
        ("命令 `echo $HOME $PATH` 和 $5", r"命令 `echo $HOME $PATH` 和 \$5"),
    ],
)
def test_prepare_keeps_formulas_and_escapes_other_dollars(text: str, expected: str):
    assert convert_doc.prepare(text) == expected


def test_prepare_leaves_display_blocks_and_code_dollars_alone():
    block = "$$\nprice = $5 + $6\n$$"
    assert convert_doc.prepare(block) == block
    # 收尾的 $$ 写在最后一行公式后面；出了公式块，钱数照样转义
    assert convert_doc.prepare("$$\na = $5\nb = 1$$\n之后 $7") == "$$\na = $5\nb = 1$$\n之后 \\$7"
    # \[ \] 换成的公式块里也不动
    assert convert_doc.prepare("\\[\nc = $5\n\\]\n之后 $7") == "$$\nc = $5\n$$\n之后 \\$7"
    fenced = "```bash\necho $HOME costs $5\n```"
    assert convert_doc.prepare(fenced) == fenced


def test_first_heading_skips_code_and_strips_marks():
    assert convert_doc.first_heading("```\n# 代码里的\n```\n\n# **季度** `报告` #\n\n# 第二个") == "季度 报告"
    assert convert_doc.first_heading("## 只有二级标题\n正文") == ""


def test_language_follows_the_text():
    assert convert_doc.language("Quarterly report 2025") == "en"
    assert convert_doc.language("Quarterly 报告") == "zh-CN"


# ---------- pandoc 出来的 HTML ----------


def test_unfetched_lists_each_resource_once():
    warnings = [
        "[WARNING] Could not fetch resource https://example.com/a.png: HttpExceptionRequest ...",
        "[WARNING] Could not fetch resource https://example.com/a.png: HttpExceptionRequest ...",
        "[WARNING] Could not fetch resource 图/缺.png: replacing image with description",
        "[WARNING] Something else",
    ]
    assert convert_doc._unfetched(warnings) == ["https://example.com/a.png", "图/缺.png"]


def test_unconverted_counts_formulas():
    warnings = ["[WARNING] Could not convert TeX math \\foo{x}, rendering as TeX:", "  \\foo{x}", "[WARNING] other"]
    assert convert_doc._unconverted(warnings) == 1


def test_tick_boxes_replaces_form_controls():
    html = '<li><input type="checkbox" checked="" />做完的</li><li><input type="checkbox" />没做的</li>'
    assert convert_doc.tick_boxes(html) == (
        '<li><span class="task-box">☑</span>做完的</li><li><span class="task-box">☐</span>没做的</li>'
    )


def test_code_lines_drops_anchors_and_line_gaps():
    html = (
        '<pre class="sourceCode python"><code class="sourceCode python">'
        '<span id="cb1-1"><a href="#cb1-1" aria-hidden="true" tabindex="-1"></a>'
        '<span class="im">import</span> os</span>\n'
        '<span id="cb1-2"><a href="#cb1-2" aria-hidden="true" tabindex="-1"></a></span>\n'
        '<span id="cb1-3"><a href="#cb1-3" aria-hidden="true" tabindex="-1"></a>x <span class="op">=</span> 1</span>'
        "</code></pre>\n<p>正文\n第二行</p>"
    )
    assert convert_doc.code_lines(html) == (
        '<pre class="sourceCode python"><code class="sourceCode python">'
        '<span id="cb1-1"><span class="im">import</span> os</span>'
        '<span id="cb1-2"></span>'
        '<span id="cb1-3">x <span class="op">=</span> 1</span>'
        "</code></pre>\n<p>正文\n第二行</p>"
    )


# ---------- 表格 ----------


def _table(header: list[str], *rows: list[str], attrs: str = "") -> str:
    head = "".join(f"<th>{cell}</th>" for cell in header)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table{attrs}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


@pytest.mark.parametrize(
    ("tag", "inner", "expected"),
    [
        ("td", "1,000", (3.0, "")),  # 数字断不开，最窄就是它自己
        ("td", "这一格的说明很长很长很长", (1.0, "")),  # 中文哪里都能断，最窄一个字
        ("td", "<code>" + "9f86d081" * 8 + "</code>", (1.0, "long")),  # 太长的一串：许任意断，不算进宽度
        ("td", "见 https://example.com/a/very/long/path 和 12.5%", (3.0, "long")),
        ("td", "Zilliz<br />Cloud", (3.6, "")),  # 换行两边各算各的
        ("td", "", (0.0, "")),
    ],
)
def test_cell_needs(tag, inner, expected):
    needs, mark = convert_doc._cell_needs(tag, inner)
    assert (round(needs, 2), mark) == expected


def test_cell_needs_keeps_short_headers_on_one_line():
    needs, mark = convert_doc._cell_needs("th", "金额（元）")
    assert mark == "keep" and needs == pytest.approx(5 * 1.05)
    # 太长的表头照普通格子算
    assert convert_doc._cell_needs("th", "这是一个写得特别长的表头") == (1.0, "")


def test_size_tables_leaves_an_ordinary_table_at_normal_size():
    html = convert_doc.size_tables(_table(["维度", "说明"], ["开发语言", "Qdrant 内存占用更低"]))
    assert html.startswith("<table><thead>")
    assert html.count('<th class="keep">') == 2
    assert "<td>开发语言</td>" in html


def test_size_tables_marks_long_tokens():
    digest = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    html = convert_doc.size_tables(_table(["项目", "哈希"], ["订单 A", digest]))
    assert f'<td class="long">{digest}</td>' in html
    assert "<td>订单 A</td>" in html
    assert html.startswith("<table><thead>")  # 长串不算进宽度，表格不用缩小


def test_size_tables_shrinks_a_wide_numeric_table():
    months = [f"{n} 月" for n in range(1, 13)]
    html = convert_doc.size_tables(_table(["城市", *months], ["上海", *["1,204"] * 12]))
    assert html.startswith('<table class="small">')
    assert '<th class="keep">12 月</th>' in html


def test_size_tables_falls_back_to_breaking_anywhere():
    weeks = [f"第 {n} 周" for n in range(1, 20)]
    html = convert_doc.size_tables(_table(["门店", *weeks], ["徐家汇店", *["12,382"] * 19]))
    assert html.startswith('<table class="smaller cramped">')
    assert 'class="keep"' not in html  # 放不下的时候，表头也得能折行


def test_size_tables_does_not_estimate_merged_cells():
    html = '<table><tr><th colspan="2">合并的表头</th></tr><tr><td>1,000</td><td>' + "x" * 30 + "</td></tr></table>"
    sized = convert_doc.size_tables(html)
    assert sized.startswith("<table><tr>")
    assert 'class="keep"' not in sized
    assert '<td class="long">' in sized


def test_size_tables_keeps_classes_already_there():
    html = convert_doc.size_tables(_table(["a"], ["x" * 30], attrs=' class="mine"'))
    assert html.startswith('<table class="mine">')
    cell = '<table><tr><td class="num">' + "x" * 30 + "</td></tr></table>"
    assert convert_doc.size_tables(cell) == cell


def test_size_tables_handles_every_table_in_the_page():
    html = convert_doc.size_tables(_table(["a"], ["1"]) + "<p>中间</p>" + _table(["b"], ["x" * 30]))
    assert html.count("<table>") == 2 and html.count('class="long"') == 1


# ---------- 公式 ----------

MATHML = (
    '<math display="inline" xmlns="http://www.w3.org/1998/Math/MathML"><semantics><mrow>{body}</mrow>'
    '<annotation encoding="application/x-tex">{tex}</annotation></semantics></math>'
)


def test_math_tree_unwraps_semantics_and_keeps_the_tex():
    root, tex, wide = convert_doc.math_tree(MATHML.format(body="<mi>E</mi><mo>=</mo><mi>m</mi>", tex="E = m"))
    assert root.tag == "math" and root.get("display") == "inline"
    assert [el.tag for el in root.iter()] == ["math", "mrow", "mi", "mo", "mi"]
    assert tex == "E = m"
    assert wide is False


def test_math_tree_moves_chinese_into_bold_text():
    body = '<mtext mathvariant="normal">ROI（投资回报率）</mtext><mo>=</mo><mi>收入</mi>'
    root, _, wide = convert_doc.math_tree(MATHML.format(body=body, tex=r"\text{ROI（投资回报率）} = 收入"))
    assert wide is True
    texts = [(el.text, el.get("mathvariant")) for el in root.iter("mtext")]
    assert texts == [("ROI", "normal"), ("（投资回报率）", "bold"), ("收入", "bold")]
    assert not list(root.iter("mi"))


def test_draw_math_falls_back_to_tex_when_it_cannot_draw(monkeypatch):
    monkeypatch.setitem(sys.modules, "ziamath", None)  # 相当于没装
    source = MATHML.format(body="<mi>a</mi>", tex="a &lt; b")
    html, failed = convert_doc.draw_math(f"<p>前 {source} 后 {source}</p>")
    assert html == '<p>前 <code class="math-tex">a &lt; b</code> 后 <code class="math-tex">a &lt; b</code></p>'
    assert failed == 2


class _FakeZiamath:
    """只够 _draw 用的 ziamath：记下画的是哪一条，回一张空图。"""

    class _Font:
        class alt_fonts:  # noqa: N801
            bold = None

    class zmath:  # noqa: N801
        loadedfonts = {"default": None}

    class config:  # noqa: N801
        class math:  # noqa: N801
            bold_font = None

    class Math:
        def __init__(self, root, size):
            self.root = root

        def svg(self) -> str:
            return "<svg/>"

        def getyofst(self) -> float:
            return -3.0


_FakeZiamath.zmath.loadedfonts["default"] = _FakeZiamath._Font


def test_draw_keeps_inline_formulas_in_the_line_and_marks_display_ones():
    inline = convert_doc._draw(_FakeZiamath, MATHML.format(body="<mi>n</mi>", tex="n"))
    assert inline.startswith('<img class="math" alt="n" style="vertical-align:-3.00px" src="data:image/svg+xml;base64,')
    block = MATHML.format(body="<mi>n</mi>", tex="n").replace('display="inline"', 'display="block"')
    display = convert_doc._draw(_FakeZiamath, block)
    assert display.startswith('<img class="math display" alt="n" src="data:image/svg+xml;base64,')


def test_stylesheet_centers_only_display_formulas():
    """一段里只有一个行内公式时，它也是这一段唯一的子元素：居中那条规则不能把它算进去。"""
    css = (SCRIPT.parent / "document.css").read_text(encoding="utf-8")
    assert "p > img:only-child:not(.math) {" in css
    assert "img.math.display { display: block;" in css


def test_draw_math_leaves_pages_without_formulas_alone():
    assert convert_doc.draw_math("<p>没有公式</p>") == ("<p>没有公式</p>", 0)


# ---------- 入口：参数不对时怎么说 ----------


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "报告.md"
    path.write_text("# 报告\n正文", encoding="utf-8")
    return path


def test_convert_rejects_unknown_input(tmp_path: Path):
    sheet = tmp_path / "表.xlsx"
    sheet.write_bytes(b"PK")
    with pytest.raises(convert_doc.ConvertError, match=r"cannot read \.xlsx"):
        convert_doc.convert(sheet, tmp_path / "out.pdf")


def test_convert_rejects_unknown_output(report: Path, tmp_path: Path):
    with pytest.raises(convert_doc.ConvertError, match=r"cannot write \.pptx"):
        convert_doc.convert(report, tmp_path / "out.pptx")
    with pytest.raises(convert_doc.ConvertError, match="without an extension"):
        convert_doc.convert(report, tmp_path / "out")


def test_convert_only_turns_html_into_pdf(tmp_path: Path):
    page = tmp_path / "页面.html"
    page.write_text("<p>你好</p>", encoding="utf-8")
    with pytest.raises(convert_doc.ConvertError, match=r"only be converted to \.pdf"):
        convert_doc.convert(page, tmp_path / "out.docx")


def test_convert_reports_missing_empty_and_binary_input(tmp_path: Path):
    with pytest.raises(convert_doc.ConvertError, match="does not exist"):
        convert_doc.convert(tmp_path / "没有.md", tmp_path / "out.pdf")
    empty = tmp_path / "空.md"
    empty.write_text(" \n", encoding="utf-8")
    with pytest.raises(convert_doc.ConvertError, match="is empty"):
        convert_doc.convert(empty, tmp_path / "out.pdf")
    binary = tmp_path / "乱.md"
    binary.write_bytes(b"\xff\xfe\x00\x80")
    with pytest.raises(convert_doc.ConvertError, match="not UTF-8"):
        convert_doc.convert(binary, tmp_path / "out.pdf")
    assert not (tmp_path / "out.pdf").exists()


def test_convert_lists_what_is_there_when_the_name_is_wrong(report: Path, tmp_path: Path):
    (tmp_path / "页面.html").write_text("<p>x</p>", encoding="utf-8")
    (tmp_path / "数据.csv").write_text("a,b", encoding="utf-8")
    (tmp_path / "目录.md").mkdir()
    with pytest.raises(convert_doc.ConvertError) as caught:
        convert_doc.convert(tmp_path / "报告(终版).md", tmp_path / "out.pdf")
    assert str(caught.value).endswith(f"does not exist. Files in {tmp_path}/ that can be converted: 报告.md, 页面.html")
    # 目录都不在：只说不存在
    with pytest.raises(convert_doc.ConvertError, match=r"does not exist$"):
        convert_doc.convert(tmp_path / "没有这个目录" / "报告.md", tmp_path / "out.pdf")


def test_main_prints_one_line_and_fails(tmp_path: Path, capsys):
    assert convert_doc.main([str(tmp_path / "没有.md"), str(tmp_path / "out.pdf")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("convert-doc: ") and captured.err.count("\n") == 1


# ---------- PPT ----------


def _deck(path: Path, slides: dict[str, str]) -> Path:
    """一个只有几张幻灯片 XML 的 zip：找字体名只看这些。"""
    import zipfile

    with zipfile.ZipFile(path, "w") as package:
        for name, body in slides.items():
            package.writestr(name, body)
    return path


def test_asked_fonts_lists_named_fonts_in_slide_order(tmp_path: Path):
    first = (
        '<a:rPr lang="zh-CN"><a:latin typeface="微软雅黑"/><a:ea typeface="微软雅黑"/></a:rPr>'
        '<a:rPr><a:latin typeface="+mn-lt"/><a:cs typeface="Arial" panose="020B0604020202020204"/></a:rPr>'
    )
    second = '<a:rPr b="1"><a:latin panose="x" typeface="AT&amp;T Sans"/><a:sym typeface=""/></a:rPr>'
    deck = _deck(
        tmp_path / "汇报.pptx",
        {
            "ppt/slides/slide1.xml": first,
            "ppt/slides/slide2.xml": second,
            # 主题、版式里的不算：没有哪段字点它的名，就不知道用没用上
            "ppt/theme/theme1.xml": '<a:latin typeface="Calibri"/>',
            "ppt/slideLayouts/slideLayout1.xml": '<a:latin typeface="Georgia"/>',
        },
    )
    assert convert_doc.asked_fonts(deck) == ["微软雅黑", "Arial", "AT&T Sans"]


def test_asked_fonts_gives_up_quietly_on_a_broken_file(tmp_path: Path):
    broken = tmp_path / "坏.pptx"
    broken.write_bytes(b"PK\x03\x04 not really a zip")
    assert convert_doc.asked_fonts(broken) == []
    assert convert_doc.asked_fonts(tmp_path / "没有.pptx") == []


def test_asked_fonts_skips_oversized_slides(tmp_path: Path, monkeypatch):
    deck = _deck(tmp_path / "大.pptx", {"ppt/slides/slide1.xml": '<a:latin typeface="微软雅黑"/>' + " " * 200})
    monkeypatch.setattr(convert_doc, "SLIDE_XML_BYTES", 100)
    assert convert_doc.asked_fonts(deck) == []


def test_replaced_fonts_are_the_named_ones_not_installed():
    installed = {"noto sans cjk sc", "dejavu sans", "文泉驿微米黑"}
    asked = ["微软雅黑", "DejaVu Sans", "文泉驿微米黑", "Arial"]
    assert convert_doc.replaced_fonts(asked, installed) == ["微软雅黑", "Arial"]
    # 问不到装了哪些字体：不乱说
    assert convert_doc.replaced_fonts(["微软雅黑"], None) == []


def test_convert_only_turns_slides_into_pdf(tmp_path: Path):
    deck = _deck(tmp_path / "汇报.pptx", {"ppt/slides/slide1.xml": "<p:sld/>"})
    with pytest.raises(convert_doc.ConvertError, match=r"presentation \(\.pptx\) can only be converted to \.pdf"):
        convert_doc.convert(deck, tmp_path / "out.docx")


def test_convert_checks_the_slides_file_before_starting_libreoffice(tmp_path: Path, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("LibreOffice should not be started")

    monkeypatch.setattr(convert_doc.subprocess, "Popen", never)
    with pytest.raises(convert_doc.ConvertError, match="does not exist"):
        convert_doc.convert(tmp_path / "没有.pptx", tmp_path / "out.pdf")
    empty = tmp_path / "空.pptx"
    empty.write_bytes(b"")
    with pytest.raises(convert_doc.ConvertError, match="is empty"):
        convert_doc.convert(empty, tmp_path / "out.pdf")
    old = tmp_path / "老格式.pptx"
    old.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(convert_doc.ConvertError, match=r"not a \.pptx file, or it is password-protected"):
        convert_doc.convert(old, tmp_path / "out.pdf")


def _fake_soffice(monkeypatch, behave):
    """把 LibreOffice 换成一个假的：behave(参数, 输出目录) 决定它写什么。记下每次的参数。"""
    calls: list[list[str]] = []

    class Process:
        pid = 4_000_000  # 没有这个进程：超时去停它时不会停到别人

        def __init__(self, args, **kwargs):
            calls.append(list(args))
            assert kwargs.get("start_new_session") is True
            self.waits = 0
            self.args = args

        def wait(self, timeout=None):
            self.waits += 1
            return behave(self, Path(self.args[self.args.index("--outdir") + 1]), timeout)

    monkeypatch.setattr(convert_doc.subprocess, "Popen", Process)
    monkeypatch.setattr(convert_doc, "installed_fonts", lambda: {"noto sans cjk sc"})
    return calls


def test_convert_slides_moves_the_result_and_reports_replaced_fonts(tmp_path: Path, monkeypatch):
    deck = _deck(
        tmp_path / "季度 汇报.pptx",
        {"ppt/slides/slide1.xml": '<a:ea typeface="微软雅黑"/><a:latin typeface="Noto Sans CJK SC"/>'},
    )

    def behave(process, made: Path, timeout):
        made.mkdir(parents=True)
        (made / "季度 汇报.pdf").write_bytes(b"%PDF-1.7 fake")
        return 0

    calls = _fake_soffice(monkeypatch, behave)
    out = tmp_path / "outputs" / "结果.pdf"
    said = convert_doc.convert(deck, out)
    assert out.read_bytes() == b"%PDF-1.7 fake"
    args = calls[0]
    assert args[:3] == ["soffice", "--headless", "--norestore"] and args[-1] == str(deck.resolve())
    assert args[args.index("--convert-to") + 1] == "pdf"
    # 用户配置目录、临时的输出目录都不在工作目录里，转完就没了
    profile = next(a for a in args if a.startswith("-env:UserInstallation=file://"))
    assert str(tmp_path) not in profile and not Path(args[args.index("--outdir") + 1]).exists()
    assert said.splitlines()[0] == f"Wrote {out} (1 KB)."  # 假的 PDF 数不出页数，就不写页数
    assert "replaced with similar ones: 微软雅黑." in said and "Noto Sans CJK SC" not in said


def test_convert_slides_fails_clearly_when_nothing_was_written(tmp_path: Path, monkeypatch):
    deck = _deck(tmp_path / "汇报.pptx", {"ppt/slides/slide1.xml": "<p:sld/>"})
    _fake_soffice(monkeypatch, lambda process, made, timeout: 0)
    with pytest.raises(convert_doc.ConvertError, match="could not be opened"):
        convert_doc.convert(deck, tmp_path / "out.pdf")
    assert not (tmp_path / "out.pdf").exists()


def test_convert_slides_stops_libreoffice_on_timeout(tmp_path: Path, monkeypatch):
    deck = _deck(tmp_path / "汇报.pptx", {"ppt/slides/slide1.xml": "<p:sld/>"})
    killed: list[tuple[int, int]] = []

    def behave(process, made: Path, timeout):
        if process.waits == 1:
            raise convert_doc.subprocess.TimeoutExpired("soffice", timeout)
        return -9

    _fake_soffice(monkeypatch, behave)
    monkeypatch.setattr(convert_doc.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(convert_doc.ConvertError, match=f"longer than {convert_doc.SOFFICE_TIMEOUT_S} seconds"):
        convert_doc.convert(deck, tmp_path / "out.pdf")
    assert killed == [(4_000_000, convert_doc.signal.SIGKILL)]


def test_convert_slides_says_so_when_libreoffice_is_missing(tmp_path: Path, monkeypatch):
    deck = _deck(tmp_path / "汇报.pptx", {"ppt/slides/slide1.xml": "<p:sld/>"})

    def missing(*args, **kwargs):
        raise FileNotFoundError("soffice")

    monkeypatch.setattr(convert_doc.subprocess, "Popen", missing)
    with pytest.raises(convert_doc.ConvertError, match="LibreOffice is not available"):
        convert_doc.convert(deck, tmp_path / "out.pdf")
