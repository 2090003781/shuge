"""向量嵌入器 —— 复用本地 bge-m3（与旧 RAG 工程同源），落地 FORMAT §3 的 vectors 表。

设计：
  · 模型**懒加载**（第一次 encode 才加载；不 encode 不占显存）
  · 模型路径/设备走环境变量（不硬编码）：AI_NOTES_EMBED_MODEL / AI_NOTES_DEVICE
  · 归一化输出（余弦相似度 = 点积）
  · 向量以 float32 BLOB 存库；模型名与维度写进 manifest（判断"索引是否过时"）

第一期不做：增量批处理调度、多模型并存、sqlite-vec ANN（数据量小时内存检索够用）。
"""
from __future__ import annotations

import os
import struct
import threading

import numpy as np

DEFAULT_MODEL = os.environ.get("AI_NOTES_EMBED_MODEL", r"D:\RAG\models\bge-m3")
DEFAULT_DEVICE = None      # None = 构造时用 device.pick() 现算（环境变量优先）
DIM = 1024          # bge-m3 dense 维度


def pack_vec(v: np.ndarray) -> bytes:
    """float32 → BLOB"""
    return np.asarray(v, dtype=np.float32).tobytes()


def unpack_vec(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


_SHARED = None
_SHARED_LOCK = threading.Lock()


def get_embedder() -> "Embedder":
    """进程内共享的嵌入器。

    预热与检索必须拿到**同一个实例**：否则预热加载一份、检索再加载一份，
    第一次调用会白等一整个模型加载（2026-09-14 实测 27 秒）。
    """
    global _SHARED
    if _SHARED is None:
        with _SHARED_LOCK:          # 2026-09-26：预热线程与主线程几乎同时调这里，
            if _SHARED is None:     # 没锁的话会建出**两个**实例 → 预热的那个不是检索用的那个（实测）
                _SHARED = Embedder()
    return _SHARED


class Embedder:
    def __init__(self, model_path: str = DEFAULT_MODEL, device: str = DEFAULT_DEVICE):
        from device import pick as _pick_device
        self.model_path = model_path
        self.device = device or _pick_device()
        self._model = None
        self._load_lock = threading.Lock()      # 预热线程与请求线程可能同时要模型
        self.model_name = os.path.basename(model_path.rstrip("\\/")) or model_path
        # 查询向量缓存（2026-09-25）：同一句话反复查（翻页/刷新/复核）不必再过一遍模型。
        # 只缓单条查询（encode_one）；文档侧走 encode 批量，不占这里。
        self._qcache = {}
        self._qhit = 0
        self.qcache_max = int(os.environ.get("AI_NOTES_QCACHE") or 512)

    # ── 懒加载 ────────────────────────────────────────────────
    def _ensure(self):
        if self._model is not None:
            return self._model
        with self._load_lock:                   # 只加载一次；后来的等它加载完直接用
            if self._model is not None:
                return self._model
            return self._load_locked()

    def _load_locked(self):
        from device import tune_torch
        tune_torch()
        from sentence_transformers import SentenceTransformer  # 重依赖，按需导入
        dev = self.device
        try:
            self._model = SentenceTransformer(self.model_path, device=dev)
        except Exception:
            # GPU 不可用则降级 CPU（不静默：打一行提示）
            print("[embedder] %s 不可用，降级 cpu" % dev)
            self._model = SentenceTransformer(self.model_path, device="cpu")
            self.device = "cpu"
        return self._model

    @property
    def available(self) -> bool:
        """模型目录在不在（在不在不保证能加载成功）"""
        return os.path.isdir(self.model_path)

    @property
    def ready(self) -> bool:
        """模型已经在内存里了吗（不会触发加载）"""
        return self._model is not None

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        m = self._ensure()
        v = m.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                     show_progress_bar=False)
        return np.asarray(v, dtype=np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        v = self._qcache.get(text)
        if v is not None:
            self._qhit += 1
            return v
        v = self.encode([text])[0]
        if len(self._qcache) >= self.qcache_max:
            self._qcache.clear()            # 满了整锅倒：查询串就那么几句，不值得做 LRU
        self._qcache[text] = v
        return v

    @property
    def qcache_hits(self) -> int:
        return self._qhit
