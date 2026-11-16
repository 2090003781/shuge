"""选设备：环境变量优先；否则只在"显存够 + GPU 不忙"时才用 cuda。

为什么要这么挑（2026-09-25 实测）：本机 GPU 经常被别的活占着（llama-server / 训练 / 量化）。
模型一旦落到显存里，就得跟它们抢 GPU：实测精排一次从 **2 秒变成 86~168 秒**。
所以默认行为改成：显存不够或 GPU 很忙 → 安心跑 CPU（稳定、2 秒级）；
想强制用哪个，设 AI_NOTES_DEVICE=cuda / cpu。
"""
import os
import subprocess
import sys

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def gpu_stat() -> tuple:
    """(free_MB, util_pct)；拿不到就 (None, None)"""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.free,utilization.gpu",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
        line = (r.stdout or "").strip().splitlines()[0]
        free, util = [x.strip() for x in line.split(",")[:2]]
        return int(free), int(util)
    except Exception:
        return None, None


def pick(need_mb: int = 2500, busy_pct: int = 20) -> str:
    """默认 **cpu**；AI_NOTES_DEVICE=cuda / cpu / auto。

    为什么默认不自动上 GPU（2026-09-25 实测）：本机 GPU 跟 **llama-server 共用**，
    它说来就来。模型落到显存之后，只要 llama-server 一跑起来（实测占用 91%），
    精排一次就从 2 秒变成 **86~168 秒**；而 CPU 上稳定在 2 秒级。
    显存那 5 GB "看着空"也不算数 —— 它随时会被抢走。
    想用 GPU：`AI_NOTES_DEVICE=cuda`（自己确定那会儿没人用）；
    想让它自己判断：`AI_NOTES_DEVICE=auto`（显存够且 GPU 不忙才用）。
    """
    env = (os.environ.get("AI_NOTES_DEVICE") or "").strip()
    if env and env != "auto":
        return env
    if not env:
        sys.stderr.write("[device] 用 cpu（默认：GPU 跟 llama-server 共用，随时会被抢；"
                         "要用 GPU 设 AI_NOTES_DEVICE=cuda 或 auto）\n")
        return "cpu"
    free, util = gpu_stat()
    if free is not None and util is not None and free >= need_mb and util <= busy_pct:
        sys.stderr.write("[device] 用 cuda（显存空 %d MB，GPU 占用 %d%%）\n" % (free, util))
        return "cuda"
    sys.stderr.write("[device] 用 cpu（显存空 %s MB，GPU 占用 %s%%：不够或太忙）\n"
                     % ("?" if free is None else free, "?" if util is None else util))
    return "cpu"


def tune_torch(default_threads: int = 4) -> int:
    """CPU 推理别让 torch 开满线程（2026-09-25 实测）。

    本机 16 线程，torch 默认开 12 个 intra-op 线程；这台机上还并着
    llama-server / 浏览器 / 训练脚本……大家一起抢 16 个线程时，
    线程越多反而越慢（实测精排一次 75 秒）。钉到少数几个线程更稳。
    想改：AI_NOTES_TORCH_THREADS。
    """
    n = int(os.environ.get("AI_NOTES_TORCH_THREADS") or default_threads)
    n = max(1, n)
    try:
        import torch
        torch.set_num_threads(n)
        try:
            torch.set_num_interop_threads(max(1, min(2, n)))
        except Exception:
            pass                      # 进程内已经用过就不能改，不影响
    except Exception:
        pass
    return n
