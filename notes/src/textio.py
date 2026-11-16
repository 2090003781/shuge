"""文本解码：把外部文件读成 UTF-8 文本（入库前统一）。

按字节存原样会让 GBK 之类的文件在库里变乱码，BOM 也会顶在正文开头；
导入时统一解码 + 去掉 BOM，库里只存干净的 UTF-8。
"""
from __future__ import annotations

import codecs

_BOMS = ((codecs.BOM_UTF8, "utf-8-sig"),
         (codecs.BOM_UTF16_LE, "utf-16"),
         (codecs.BOM_UTF16_BE, "utf-16"),
         (codecs.BOM_UTF32_LE, "utf-32"),
         (codecs.BOM_UTF32_BE, "utf-32"))

FALLBACKS = ("gb18030", "big5")


def decode_text(data: bytes) -> tuple[str, str]:
    """返回 (文本, 用到的编码)。解不出来就 utf-8 + replace，绝不抛。"""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode(enc).lstrip("\ufeff"), enc
            except UnicodeDecodeError:
                break
    try:
        return data.decode("utf-8").lstrip("\ufeff"), "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in FALLBACKS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace").lstrip("\ufeff"), "utf-8/replace"
