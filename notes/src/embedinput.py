"""嵌入输入的统一构造：**日期 + 正文**，提及名字换占位。

为什么把日期放进嵌入（2026-09-15 辞玖问出来的）：
  日志区大量"今天做了 X"这种同句不同日。纯向量看它们几乎是同一个东西，
  于是"这周五"的查询可能把"上周五"那条捞上来，且完全无法区分。
  日期进了嵌入文本，两条就不再是同一个向量；查询里带日期时也能对上。

同时给检索结果补"上下文"：同篇的 seq/总数、相邻块摘要（见 channel 层）。
"""
from __future__ import annotations

from mentions import mask_mentions


SPEC = "d2"          # d1→d2：掩码只吃"真引用"（伪标签留在正文里）


def spec_model(model_name: str) -> str:
    """向量表 model 列的值 = 模型名 # 嵌入规格版本。

    规格一改（d1 = 加了【日期】前缀），老向量在 pending 判定里就自动作废重算，
    不用手写迁移。
    """
    return "%s#%s" % (model_name, SPEC)


def embed_text(text: str, doc_date: str | None = None, resolve=None) -> str:
    """嵌入用的文本 = 【日期】+ 提及掩码后的正文（代码围栏里的提及保持原样）

    resolve 传 db.ref_resolver() 时，只有"真引用"被掩码（伪标签留着）。
    """
    body = mask_mentions(text or "", resolve)
    d = (doc_date or "").strip()
    return ("【%s】" % d) + body if d else body
