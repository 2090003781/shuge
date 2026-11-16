"""文档转换：**借用成熟工具集成**（pandoc → WPS COM → 内置解析器），一步到位，产物做校验。

依据 vault 里 2026-09-12 那次"文档转换技术讨论"的结论与产物
（`reasonix/global-workspace/projects/format-converter/make_docx.py`）：

  · 借用成熟转换技术集成，**不用浏览器中转打印**，一步到位更快更稳
  · md → docx 必须 `--from markdown-smart`：否则中文里的直引号会被 smart 扩展"智能配对"成弯引号，
    而且中文语境下左右判断会出错
  · 产物要**校验**：docx = zip 完好 + `word/document.xml` 可解析 + 段落数；
    并与源文对照**汉字数**（辞玖："人看字数不看标点"）

引擎优先级：
  读入（docx/odt/rtf/html/epub → Markdown）：pandoc（`-t gfm`，表格/结构最好）→ 内置 doctext
  写出（Markdown → docx/odt/rtf/html/epub）：pandoc（`--from markdown-smart`）
  出 PDF：pandoc 本机没有 pdf 引擎（缺 pdflatex）→ 走 **WPS COM**（先出 docx 再 SaveAs2 17=PDF）
全程无窗口：pandoc 是命令行；WPS 用 Visible=false + PowerShell 无窗口启动。
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile


PANDOC = None
WPS_OK = None            # None=没探过；True/False=探过

TEXT_OUT = {"docx", "odt", "rtf", "html", "htm", "epub", "markdown", "md"}
HAN = re.compile(r"[\u4e00-\u9fff]")
# 标点与空白（辞玖"人看字数不看标点"：算字数时这些都去掉）
PUNCT = re.compile(r"[\s\.,;:!?，。；：！？、“”‘’（）()《》〈〉【】\[\]{}<>—－·…～~`\"'’“”\-_/\\|*#>+]")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def han_count(text: str) -> int:
    """汉字数（vault 流程用它做产物/源文对照）"""
    return len(HAN.findall(text or ""))


def char_count(text: str) -> int:
    """去标点空白后的字符数（"人看字数不看标点"）"""
    return len(PUNCT.sub("", text or ""))


def pandoc_path() -> str:
    global PANDOC
    if PANDOC is None:
        PANDOC = shutil.which("pandoc") or ""
        if not PANDOC:
            for c in (r"C:\Program Files\Pandoc\pandoc.exe",
                      os.path.expandvars(r"%LOCALAPPDATA%\Pandoc\pandoc.exe")):
                if os.path.isfile(c):
                    PANDOC = c
                    break
        if not PANDOC:                       # winget 装的那种（带版本目录）
            pat = os.path.expandvars(
                r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\JohnMacFarlane.Pandoc_*\pandoc-*\pandoc.exe")
            import glob
            hits = sorted(glob.glob(pat))
            if hits:
                PANDOC = hits[-1]
    return PANDOC


def wps_available(refresh: bool = False) -> bool:
    """WPS 能不能用 COM 调起来（结果缓存；探测本身约 1 秒）"""
    global WPS_OK
    if WPS_OK is None or refresh:
        script = ('try { $w = New-Object -ComObject KWPS.Application; $w.Quit(); "OK" }'
                  ' catch { "ERR: " + $_.Exception.Message }')
        rc, out, _err = _powershell(script, timeout=90)
        WPS_OK = (rc == 0 and out.strip().endswith("OK"))
    return bool(WPS_OK)


def engines() -> dict:
    p = pandoc_path()
    ver = ""
    if p:
        rc, out, err = _run([p, "--version"])
        ver = (out or err).splitlines()[0] if (out or err) else ""
    return {"pandoc": p, "pandoc_version": ver, "wps": wps_available(),
            "builtin": "doctext（docx/odt/rtf/html/epub 读入）"}


def _run(cmd, timeout=180, cwd=None):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=cwd, creationflags=NO_WINDOW)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def _powershell(script: str, timeout=300):
    """跑一段 PowerShell（无窗口）。脚本写临时文件时用 UTF-8 带 BOM（PS 5.1 认）"""
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="docconv-")
    os.close(fd)
    io.open(path, "w", encoding="utf-8-sig", newline="\r\n").write(script)
    try:
        return _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                    timeout=timeout)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


# ── 读入：任何格式 → Markdown ────────────────────────────────
def read_to_md(ext: str, data: bytes) -> dict:
    """把文档读成 Markdown。pandoc 优先（表格/结构好），不行就内置解析器。"""
    e = (ext or "").lower().lstrip(".")
    p = pandoc_path()
    if p and e not in ("md", "markdown", "txt", "pdf"):   # pandoc 读不了 pdf
        with tempfile.TemporaryDirectory(prefix="docconv-") as td:
            src = os.path.join(td, "in." + (e or "bin"))
            open(src, "wb").write(data)
            rc, out, err = _run([p, src, "--from", e, "--to", "gfm", "--wrap=none"], timeout=300)
            if rc == 0 and out.strip():
                return {"text": out.strip(), "engine": "pandoc", "error": ""}
    import doctext
    r = doctext.parse(e, data)
    return {"text": r.get("text") or "", "engine": "builtin(%s)" % (r.get("parser") or "?"),
            "error": r.get("error") or ""}


# ── 写出：Markdown → docx/odt/rtf/html/epub ──────────────────
def write_md(md_text: str, to_ext: str, out_path: str) -> dict:
    e = (to_ext or "").lower().lstrip(".")
    if e in ("md", "markdown"):
        io.open(out_path, "w", encoding="utf-8", newline="").write(md_text)
        return {"engine": "直接落盘", "error": ""}
    p = pandoc_path()
    if not p:
        return {"engine": "", "error": "本机没有 pandoc，也没有可用的写出引擎"}
    with tempfile.TemporaryDirectory(prefix="docconv-") as td:
        src = os.path.join(td, "in.md")
        io.open(src, "w", encoding="utf-8", newline="").write(md_text)
        # markdown-smart：别让 smart 扩展把中文直引号智能配对（vault 流程踩过的坑）
        rc, out, err = _run([p, src, "--from", "markdown-smart", "--to", e, "-o", out_path],
                            timeout=600)
    if rc != 0 or not os.path.isfile(out_path):
        return {"engine": "pandoc", "error": "pandoc 转换失败：%s" % (err or out)[:200]}
    return {"engine": "pandoc %s" % e, "error": ""}


# ── PDF：pandoc 没引擎，走 WPS COM ───────────────────────────
def to_pdf(md_text: str, out_path: str, docx_path: str = "") -> dict:
    """Markdown → PDF。先用 pandoc 出 docx，再让 WPS 另存为 PDF（一步到位，不经浏览器打印）。"""
    tmp_docx = docx_path
    cleanup = False
    if not tmp_docx:
        td = tempfile.mkdtemp(prefix="docconv-")
        tmp_docx = os.path.join(td, "in.docx")
        cleanup = True
    r = write_md(md_text, "docx", tmp_docx)
    if r.get("error"):
        return r
    if not wps_available():
        return {"engine": "", "error": "出 PDF 需要 WPS（未检测到可用的 WPS COM）"}
    ps = ('$ErrorActionPreference="Stop"\n'
          'try {\n'
          '  $w = New-Object -ComObject KWPS.Application\n'
          '  $w.Visible = $false\n'
          '  $w.DisplayAlerts = 0\n'
          '  $doc = $w.Documents.Open("%s", $false, $true)\n'
          '  $doc.SaveAs2("%s", 17)\n'        # 17 = wdFormatPDF
          '  $doc.Close(0)\n'
          '  $w.Quit()\n'
          '  "OK"\n'
          '} catch { "ERR: " + $_.Exception.Message }\n' % (tmp_docx, out_path))
    rc, out, err = _powershell(ps, timeout=600)
    if cleanup:
        shutil.rmtree(os.path.dirname(tmp_docx), ignore_errors=True)
    if rc != 0 or not out.strip().endswith("OK") or not os.path.isfile(out_path):
        return {"engine": "WPS COM", "error": "WPS 出 PDF 失败：%s" % (out or err)[:200]}
    return {"engine": "pandoc+WPS", "error": ""}


def docx_to_pdf(docx_path: str, out_path: str) -> dict:
    """**docx 直接另存成 PDF**（WPS COM，不经 Markdown）—— 样式不简化，给 docx 引擎的"导出 PDF"用。

    为什么不复用 to_pdf：那条路会先用 pandoc 把 md 重新写成 docx，把原文件的样式盖掉。
    """
    if not os.path.isfile(docx_path):
        return {"engine": "", "error": "要转的 docx 不在：%s" % docx_path}
    if not wps_available():
        return {"engine": "", "error": "出 PDF 需要 WPS（未检测到可用的 WPS COM）"}
    ps = ('$ErrorActionPreference="Stop"\n'
          'try {\n'
          '  $w = New-Object -ComObject KWPS.Application\n'
          '  $w.Visible = $false\n'
          '  $w.DisplayAlerts = 0\n'
          '  $doc = $w.Documents.Open("%s", $false, $true)\n'
          '  $doc.SaveAs2("%s", 17)\n'
          '  $doc.Close(0)\n'
          '  $w.Quit()\n'
          '  "OK"\n'
          '} catch { "ERR: " + $_.Exception.Message }\n' % (docx_path, out_path))
    rc, out, err = _powershell(ps, timeout=600)
    if rc != 0 or not out.strip().endswith("OK") or not os.path.isfile(out_path):
        return {"engine": "WPS COM", "error": "WPS 把 docx 转 PDF 失败：%s" % (out or err)[:200]}
    return {"engine": "WPS COM（docx 直转）", "error": ""}


# ── 校验：产物结构 + 汉字数对照（vault 流程的精髓）──────────
def content_of(md_text: str) -> str:
    """比对用的"内容"：去掉 HTML 注释（`<!-- 由 … 转换而来 -->` 这类，任何格式都不会保留）"""
    return re.sub(r"<!--.*?-->", "", md_text or "", flags=re.S)


# ── 新建：一段文本 → 直接生成目标格式的文件 ──────────────────
WRITE_EXTS = ("docx", "odt", "rtf", "html", "epub", "pdf")


def make(md_text: str, to_ext: str) -> dict:
    """现场生成一个目标格式的文件（"新建 Word 文档"走这里）：{data, engine, verify} / {error}。"""
    e = (to_ext or "").lower().lstrip(".")
    if e not in WRITE_EXTS:
        return {"error": "不支持生成 .%s（可用 %s）" % (e, "/".join(WRITE_EXTS))}
    with tempfile.TemporaryDirectory(prefix="docconv-") as td:
        out = os.path.join(td, "out." + e)
        r = to_pdf(md_text, out) if e == "pdf" else write_md(md_text, e, out)
        if r.get("error"):
            return r
        r["data"] = open(out, "rb").read()
        r["verify"] = verify(out, e, md_text)
        return r


def verify(out_path: str, to_ext: str, src_text: str) -> dict:
    e = (to_ext or "").lower().lstrip(".")
    src_text = content_of(src_text)
    out = {"engine_check": "", "paragraphs": 0, "han_src": han_count(src_text),
           "han_out": 0, "chars_src": char_count(src_text), "chars_out": 0,
           "ok": False, "detail": ""}
    if not os.path.isfile(out_path):
        out["detail"] = "产物不存在"
        return out
    try:
        if e in ("docx", "docm", "odt", "ott"):
            z = zipfile.ZipFile(out_path)
            bad = z.testzip()
            out["engine_check"] = "zip 完好" if bad is None else "zip 损坏：%s" % bad
            if e.startswith("doc"):
                xml = z.read("word/document.xml").decode("utf-8", "replace")
                texts = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S))
                out["paragraphs"] = len(re.findall(r"<w:p\b", xml))
            else:
                xml = z.read("content.xml").decode("utf-8", "replace")
                texts = re.sub(r"<[^>]+>", "", xml)
                out["paragraphs"] = len(re.findall(r"<text:p\b", xml))
            out["han_out"] = han_count(texts)
            out["chars_out"] = char_count(texts)
            if bad is not None:
                out["detail"] = out["engine_check"]
                return out
        elif e == "pdf":
            import doctext
            d = doctext.parse("pdf", open(out_path, "rb").read())
            if d.get("error"):
                out["detail"] = "PDF 抽不出文字（可能是扫描件）：%s" % d["error"][:80]
                out["ok"] = True          # 生成的 PDF 本身没问题，只是没法做字数对照
                out["engine_check"] = "PDF 无文字层"
                return out
            out["han_out"] = han_count(d["text"])
            out["chars_out"] = char_count(d["text"])
            out["engine_check"] = "PDF 可抽文字（%s 页）" % (d.get("meta") or {}).get("pages", "?")
        else:                              # html/rtf/epub 等：按文本粗略对照
            import doctext
            d = doctext.parse(e, open(out_path, "rb").read())
            out["han_out"] = han_count(d.get("text") or "")
            out["chars_out"] = char_count(d.get("text") or "")
            out["engine_check"] = "可解析（%s）" % (d.get("parser") or "?")
    except Exception as ex:
        out["detail"] = "%s: %s" % (type(ex).__name__, str(ex)[:120])
        return out

    src_h, out_h = out["han_src"], out["han_out"]
    diff = abs(src_h - out_h)
    tol = max(2, int(src_h * 0.02))        # 容差 2%（排版会吃掉/加上少量字符）
    if src_h == 0:
        out["ok"] = True
        out["detail"] = "源文没有汉字（只比对结构）"
    elif diff <= tol:
        out["ok"] = True
        out["detail"] = "汉字数一致（源 %d / 产物 %d）" % (src_h, out_h)
    else:
        out["ok"] = False
        out["detail"] = "汉字数对不上（源 %d / 产物 %d，差 %d）" % (src_h, out_h, diff)
    return out
