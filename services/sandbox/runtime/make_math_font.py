"""构建镜像时生成公式里画中文用的字体 math-cjk.ttf（convert-doc 出 PDF 时用，见 convert_doc.py 的 math_tree）。

    python make_math_font.py /opt/sandbox/math-cjk.ttf

画公式的 ziamath 只认单个的 TrueType 字体，装进系统的中文字体都是几种字体合在一起的 .ttc；
思源（Noto CJK）的字形格式它也不认。所以从文泉驿微米黑里把一种字体拆出来。
再把字体的单位换成每字 1000：ziamath 按数学字体（STIX Two，每字 1000）的单位算字宽，文泉驿是每字 2048，
不换的话每个中文字后面空出一个字的位置。
顺带把字缩小一成：公式的字号比正文大一成（数学字体的小写字母矮，放大了才和正文一样高），中文跟着放大就比旁边正文里的中文大一圈。
"""

from __future__ import annotations

import sys

from fontTools.ttLib import TTCollection
from fontTools.ttLib.scaleUpem import scale_upem

SOURCE = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
FAMILY = "WenQuanYi Micro Hei"
UNITS = 1000  # 数学字体每字的单位
BODY_PX, MATH_PX = 14.0, 15.5  # 正文、公式的字号，和 document.css、convert_doc.MATH_PX 一致


def main(target: str) -> None:
    font = next(f for f in TTCollection(SOURCE).fonts if f["name"].getDebugName(1) == FAMILY)
    # 字形按“每字 903 单位”缩好，再声明成每字 1000：画出来就是公式字号的九成，正好是正文的大小
    scale_upem(font, round(UNITS * BODY_PX / MATH_PX))
    font["head"].unitsPerEm = UNITS
    font.save(target)


if __name__ == "__main__":
    main(sys.argv[1])
