"""导出：把库里的文件写回磁盘（备份 / 迁移 / 不锁定）。

库是真相源，导出只是单向快照；导出不改库。private 文件未解锁时不写内容，只记名。
"""
from __future__ import annotations

import json
import os

import vaultcrypto
from db import Database, local_iso

META_NAME = ".ai-notes-export.json"


class Exporter:
    def __init__(self, db: Database, master: bytes | None = None, events=None):
        self.db = db
        self.master = master
        self.events = events

    def export(self, dest: str, overwrite: bool = True, with_meta: bool = True) -> dict:
        root = os.path.abspath(dest)
        os.makedirs(root, exist_ok=True)
        report = {"dest": root, "files": 0, "folders": 0, "skipped": [], "locked": [], "items": []}
        self._walk(self.db.root_folder_id(), root, overwrite, report)
        if with_meta:
            self._write_meta(root, report)
        if self.events:
            self.events.emit("note.exported", {"count": report["files"], "dest_root": root,
                                               "skipped": len(report["skipped"]),
                                               "locked": len(report["locked"])})
        return report

    def _walk(self, folder_id: str, path: str, overwrite: bool, report: dict) -> None:
        report["folders"] += 1
        for f in self.db.list_files(folder_id):
            target = os.path.join(path, f["name"])
            item = {"id": f["id"], "name": f["name"], "path": os.path.relpath(target, report["dest"]),
                    "kind": f["kind"], "visibility": f["visibility"], "mtime": f["mtime"],
                    "mentions": self.db.mentions(f["id"])}
            if f["visibility"] == "private" and self.master is None:
                report["locked"].append(item["path"])
                item["exported"] = False
                report["items"].append(item)
                continue
            if os.path.exists(target) and not overwrite:
                report["skipped"].append(item["path"])
                item["exported"] = False
                report["items"].append(item)
                continue
            try:
                data = self.db.get_content(f["id"], self.master)
            except vaultcrypto.LockedError:
                report["locked"].append(item["path"])
                item["exported"] = False
                report["items"].append(item)
                continue
            tmp = target + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)          # 原子替换，避免导出中途留下半截文件
            report["files"] += 1
            item["exported"] = True
            item["bytes"] = len(data)
            report["items"].append(item)

        for c in self.db.list_folders(folder_id):
            sub = os.path.join(path, c["name"])
            os.makedirs(sub, exist_ok=True)
            self._walk(c["id"], sub, overwrite, report)

    def _write_meta(self, root: str, report: dict) -> None:
        man = {k: v for k, v in self.db.manifest().items() if not k.startswith("kdf_") and k != "verify_blob"}
        meta = {"format": "ai-notes-export/1", "exported_at": local_iso(),
                "source": str(self.db.path), "manifest": man,
                "stats": self.db.stats(), "files": report["items"]}
        with open(os.path.join(root, META_NAME), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1)
