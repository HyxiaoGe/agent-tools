"""构建镜像时生成 Word 的样式模板 reference.docx（convert-doc 转 .docx 时给 pandoc 的 --reference-doc）。

    python make_reference_docx.py /opt/sandbox/reference.docx

从 pandoc 自带的模板改，不往仓库里放二进制文件：
- 字体：西文 Arial、中文微软雅黑、代码 Consolas（Windows 和装了 Office 的 Mac 都有；
  没有的机器 Word、WPS 自己换成默认的中文字体）。
  自带模板写的是主题字体，中文落到哪种字体看用户机器的设置，这里写死。
- 正文五号（10.5 磅）、1.5 倍行距；标题黑色（自带的是主题蓝），字号按中文报告常用的。
- 表格全框线、表头加粗带底色：自带的只有表头下面一条线，中文报告里的对比表不好读。
- 代码块带底色；引用左边一条竖线、灰字。
- A4（自带的是 Letter），页脚居中页码。

样式一律按样式的 id 找（Heading1、BodyText…）：python-docx 按名字找时会把 “Heading 1” 换成小写的内部名，
而 pandoc 模板里的名字是大写开头的，找不到。
"""

from __future__ import annotations

import subprocess
import sys

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

LATIN, EAST, MONO = "Arial", "微软雅黑", "Consolas"
INK, MUTED, LINK = RGBColor(0x11, 0x11, 0x11), RGBColor(0x57, 0x60, 0x6A), RGBColor(0x0B, 0x57, 0xD0)
BORDER, HEAD_FILL, CODE_FILL = "BFC5CD", "F3F5F7", "F3F4F6"
# 标题：字号（磅）、段前、段后
HEADINGS = {
    "Title": (22, 12, 10),
    "Heading1": (18, 12, 10),
    "Heading2": (15, 18, 8),
    "Heading3": (13, 14, 6),
    "Heading4": (11.5, 12, 4),
    "Heading5": (10.5, 12, 4),
    "Heading6": (10.5, 12, 4),
}
# 这几种元素里子元素的先后是规定死的（WordprocessingML 的 schema），放错了 Word 会说文件损坏
PPR_SEQ = (
    "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr", "w:widowControl", "w:numPr",
    "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
    "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi", "w:adjustRightInd",
    "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
    "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr",
    "w:sectPr", "w:pPrChange",
)  # fmt: skip
RPR_SEQ = (
    "w:rStyle", "w:rFonts", "w:b", "w:bCs", "w:i", "w:iCs", "w:caps", "w:smallCaps", "w:strike", "w:dstrike",
    "w:outline", "w:shadow", "w:emboss", "w:imprint", "w:noProof", "w:snapToGrid", "w:vanish", "w:webHidden",
    "w:color", "w:spacing", "w:w", "w:kern", "w:position", "w:sz", "w:szCs", "w:highlight", "w:u", "w:effect",
    "w:bdr", "w:shd", "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang", "w:eastAsianLayout",
    "w:specVanish", "w:oMath",
)  # fmt: skip
STYLE_SEQ = (
    "w:name", "w:aliases", "w:basedOn", "w:next", "w:link", "w:autoRedefine", "w:hidden", "w:uiPriority",
    "w:semiHidden", "w:unhideWhenUsed", "w:qFormat", "w:locked", "w:personal", "w:personalCompose",
    "w:personalReply", "w:rsid", "w:pPr", "w:rPr", "w:tblPr", "w:trPr", "w:tcPr", "w:tblStylePr",
)  # fmt: skip
TBLPR_SEQ = (
    "w:tblStyle", "w:tblpPr", "w:tblOverlap", "w:bidiVisual", "w:tblStyleRowBandSize", "w:tblStyleColBandSize",
    "w:tblW", "w:jc", "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd", "w:tblLayout", "w:tblCellMar",
    "w:tblLook",
)  # fmt: skip


def put(parent, tag: str, seq: tuple[str, ...], *, fresh: bool = False):
    """取 parent 下的 tag，没有就按规定的先后建一个；fresh：有也换成新的。"""
    found = parent.find(qn(tag))
    if found is not None and fresh:
        parent.remove(found)
        found = None
    if found is None:
        found = OxmlElement(tag)
        parent.insert_element_before(found, *seq[seq.index(tag) + 1 :])
    return found


def attrs(element, **values: str):
    """把一个元素的属性整个换掉（主题字体、主题色这些指向一并去掉）。"""
    element.attrib.clear()
    for name, value in values.items():
        element.set(qn(f"w:{name}"), value)
    return element


def set_fonts(rpr, latin: str, east: str) -> None:
    attrs(put(rpr, "w:rFonts", RPR_SEQ), ascii=latin, hAnsi=latin, cs=latin, eastAsia=east)


def shade(parent, seq: tuple[str, ...], fill: str) -> None:
    attrs(put(parent, "w:shd", seq), val="clear", color="auto", fill=fill)


def border(tag: str, size: str, color: str, space: str = "0"):
    return attrs(OxmlElement(tag), val="single", sz=size, space=space, color=color)


def restyle(doc) -> None:
    styles = {style.style_id: style for style in doc.styles}

    # 文档的默认值：字体、五号字、中文
    rpr = doc.styles.element.find(qn("w:docDefaults")).find(qn("w:rPrDefault")).find(qn("w:rPr"))
    set_fonts(rpr, LATIN, EAST)
    for tag in ("w:sz", "w:szCs"):
        attrs(put(rpr, tag, RPR_SEQ), val="21")
    attrs(put(rpr, "w:lang", RPR_SEQ), val="en-US", eastAsia="zh-CN", bidi="ar-SA")

    # 凡是自己指了字体的样式（标题指的是主题里的标题字体），一律写死
    for style in doc.styles:
        own = style.element.rPr
        if own is not None and own.find(qn("w:rFonts")) is not None:
            set_fonts(own, LATIN, EAST)

    for style_id, (size, before, after) in HEADINGS.items():
        style = styles[style_id]
        set_fonts(style.element.get_or_add_rPr(), LATIN, EAST)
        style.font.color.rgb = INK
        style.font.size = Pt(size)
        attrs(put(style.element.rPr, "w:szCs", RPR_SEQ), val=str(int(size * 2)))
        style.font.bold = True
        style.font.italic = False
        fmt = style.paragraph_format
        fmt.space_before, fmt.space_after, fmt.keep_with_next = Pt(before), Pt(after), True

    for style_id in ("BodyText", "FirstParagraph"):
        fmt = styles[style_id].paragraph_format
        fmt.space_before, fmt.space_after, fmt.line_spacing = Pt(0), Pt(6), 1.5
    # Compact：紧凑的列表项、表格的格子用的段落样式
    fmt = styles["Compact"].paragraph_format
    fmt.space_before, fmt.space_after, fmt.line_spacing = Pt(2), Pt(2), 1.3

    # 引用：灰字，左边一条竖线
    quote = styles["BlockText"]
    quote.font.color.rgb = MUTED
    quote.font.italic = False
    quote.paragraph_format.left_indent, quote.paragraph_format.right_indent = Cm(0.5), Cm(0)
    put(quote.element.get_or_add_pPr(), "w:pBdr", PPR_SEQ, fresh=True).append(border("w:left", "18", "D0D7DE", "10"))

    styles["Hyperlink"].font.color.rgb = LINK

    # 代码：行内的和代码块里的字都是 VerbatimChar；代码块那一段是 Source Code（自带模板里没有，pandoc 现补的没有底色）
    code = styles["VerbatimChar"]
    set_fonts(code.element.get_or_add_rPr(), MONO, EAST)
    code.font.size = Pt(9)
    shade(code.element.rPr, RPR_SEQ, CODE_FILL)
    block = doc.styles.add_style("Source Code", WD_STYLE_TYPE.PARAGRAPH)
    block.base_style = styles["Normal"]
    block.element.insert_element_before(attrs(OxmlElement("w:link"), val="VerbatimChar"), *STYLE_SEQ[5:])
    fmt = block.paragraph_format
    fmt.space_before, fmt.space_after, fmt.line_spacing = Pt(4), Pt(8), 1.2
    ppr = block.element.get_or_add_pPr()
    shade(ppr, PPR_SEQ, CODE_FILL)
    attrs(put(ppr, "w:wordWrap", PPR_SEQ), val="0")  # 很长的一行代码可以在任意位置折行

    # 表格：全框线，格子留边，表头加粗带底色，字比正文小半号
    table = styles["Table"]
    table.font.size = Pt(9.5)
    tbl_pr = put(table.element, "w:tblPr", STYLE_SEQ)
    borders = put(tbl_pr, "w:tblBorders", TBLPR_SEQ, fresh=True)
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        borders.append(border(f"w:{side}", "4", BORDER))
    margins = put(tbl_pr, "w:tblCellMar", TBLPR_SEQ, fresh=True)
    for side, width in (("top", "40"), ("left", "100"), ("bottom", "40"), ("right", "100")):
        margins.append(attrs(OxmlElement(f"w:{side}"), w=width, type="dxa"))
    for cond in table.element.findall(qn("w:tblStylePr")):
        if cond.get(qn("w:type")) == "firstRow":
            table.element.remove(cond)
    first = attrs(OxmlElement("w:tblStylePr"), type="firstRow")
    first.append(OxmlElement("w:rPr"))
    first[0].append(OxmlElement("w:b"))
    first.append(OxmlElement("w:tcPr"))
    first[1].append(attrs(OxmlElement("w:shd"), val="clear", color="auto", fill=HEAD_FILL))
    table.element.append(first)


def page_setup(doc) -> None:
    for section in doc.sections:
        section.page_width, section.page_height = Cm(21.0), Cm(29.7)
        section.top_margin = section.bottom_margin = Cm(2.54)
        section.left_margin = section.right_margin = Cm(2.6)
        para = section.footer.paragraphs[0] if section.footer.paragraphs else section.footer.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = para.add_run()
        run.font.size = Pt(9)
        run.font.color.rgb = MUTED
        # 页码是一个域：开始、指令、结束
        run._r.append(attrs(OxmlElement("w:fldChar"), fldCharType="begin"))
        instruction = OxmlElement("w:instrText")
        instruction.set(qn("xml:space"), "preserve")
        instruction.text = "PAGE"
        run._r.append(instruction)
        run._r.append(attrs(OxmlElement("w:fldChar"), fldCharType="end"))


def main(target: str) -> None:
    with open(target, "wb") as out:
        subprocess.run(["pandoc", "--print-default-data-file", "reference.docx"], stdout=out, check=True)
    doc = Document(target)
    restyle(doc)
    page_setup(doc)
    doc.save(target)


if __name__ == "__main__":
    main(sys.argv[1])
