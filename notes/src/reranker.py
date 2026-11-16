"""精排（Cross-Encoder Rerank）—— 从旧 RAG 工程迁过来，用本机现成的 bge-reranker-v2-m3。

来源与取舍（D:\RAG\src\retriever.py 的两阶段检索）：
  · 粗召回（关键词 + 向量 + 关系）→ 融合分截断到 max_rerank → **只对这部分精排**（提速关键）
  · bge-reranker-v2-m3 是单输出回归式：直接取 logit（越大越相关），不做 softmax
  · 输入截断 input_chars（默认 600）：判分够用，速度差一倍以上

懒加载：不调检索就不加载模型；模型不在就自动降级（不影响关键词+向量检索）。
"""
from __future__ import annotations

import os
import threading

DEFAULT_MODEL = os.environ.get("AI_NOTES_RERANK_MODEL", r"D:\RAG\models\bge-reranker-v2-m3")
DEFAULT_DEVICE = None      # None = 构造时用 device.pick() 现算（环境变量优先）
INPUT_CHARS = int(os.environ.get("AI_NOTES_RERANK_CHARS") or 600)


class Reranker:
    def __init__(self, model_path: str = DEFAULT_MODEL, device: str = DEFAULT_DEVICE):
        from device import pick as _pick_device
        self.model_path = model_path
        self.device = device or _pick_device()
        self._model = None
        self._load_lock = threading.Lock()
        self.model_name = os.path.basename(model_path.rstrip("\\/")) or model_path

    @property
    def available(self) -> bool:
        return os.path.isdir(self.model_path)

    @property
    def ready(self) -> bool:
        """模型已经在内存里了吗（不会触发加载）"""
        return self._model is not None

    def _ensure(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            return self._load_locked()

    def _load_locked(self):
        from device import tune_torch
        tune_torch()
        from sentence_transformers import CrossEncoder
        try:
            self._model = CrossEncoder(self.model_path, device=self.device)
        except Exception:
            print("[rerank] %s 不可用，降级 cpu" % self.device)
            self._model = CrossEncoder(self.model_path, device="cpu")
            self.device = "cpu"
        return self._model

    def score(self, query: str, texts: list) -> list:
        """返回每个 passage 的相关性分（越大越相关）；空输入返回 []"""
        if not texts:
            return []
        m = self._ensure()
        pairs = [(query, (t or "")[:INPUT_CHARS]) for t in texts]
        out = m.predict(pairs)
        return [float(x) for x in out]


_RERANKER = None
_RERANKER_LOCK = threading.Lock()


def get_reranker() -> Reranker:
    global _RERANKER
    if _RERANKER is None:
        with _RERANKER_LOCK:        # 同 get_embedder：加锁，别建出两个
            if _RERANKER is None:
                _RERANKER = Reranker()
    return _RERANKER
