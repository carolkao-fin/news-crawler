# -*- coding: utf-8 -*-
"""
歷史紀錄
========

每次蒐集完成後把結果存成一筆 JSON，之後可以列出、重新載入、重新產檔或下載。

儲存位置依序取用：
1. 環境變數 `NEWS_CRAWLER_HISTORY`
2. 程式所在資料夾下的 `history/`

一筆紀錄就是一個檔案（`20260826-103012_台灣禾邦電子有限公司.json`），沒有額外的
索引檔——索引由掃描資料夾即時產生，多個 process 同時寫入也不會互相覆蓋。
"""

from __future__ import annotations

__version__ = "1.4.0"

import io
import json
import os
import re
import zipfile
from datetime import datetime
from typing import Iterable, List, Optional

_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")


def history_dir() -> str:
    d = os.environ.get("NEWS_CRAWLER_HISTORY") or _DEFAULT_DIR
    os.makedirs(d, exist_ok=True)
    return d


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r'[\\\\/:*?"<>|\\s]+', "", text or "")
    return s[:limit] or "未命名"


def save_run(company: str,
             articles: Iterable,
             keywords: Optional[Iterable[str]] = None,
             year: str = "", case_no: str = "", tax_id: str = "",
             params: Optional[dict] = None,
             note: str = "") -> str:
    """存一筆蒐集紀錄，回傳紀錄 id（即檔名去掉副檔名）。"""
    arts = list(articles)
    now = datetime.now()
    rec_id = "%s_%s" % (now.strftime("%Y%m%d-%H%M%S"), _slug(company))
    record = {
        "id": rec_id,
        "saved_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "company": company,
        "year": year,
        "case_no": case_no,
        "tax_id": tax_id,
        "note": note,
        "keywords": list(keywords or []),
        "params": params or {},
        "count": len(arts),
        "sources": _source_summary(arts),
        "articles": [a.to_dict() if hasattr(a, "to_dict") else dict(a) for a in arts],
    }
    path = os.path.join(history_dir(), rec_id + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
    return rec_id


def _source_summary(arts: Iterable) -> dict:
    out: dict = {}
    for a in arts:
        src = getattr(a, "source", None) or (a.get("source") if isinstance(a, dict) else "")
        src = src or "未標示"
        out[src] = out.get(src, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def list_runs(limit: int = 200) -> List[dict]:
    """列出歷史紀錄摘要（不含全文），新到舊。"""
    d = history_dir()
    rows: List[dict] = []
    for name in sorted(os.listdir(d), reverse=True):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:      # noqa: BLE001
            continue
        rows.append({
            "id": rec.get("id", name[:-5]),
            "saved_at": rec.get("saved_at", ""),
            "company": rec.get("company", ""),
            "year": rec.get("year", ""),
            "case_no": rec.get("case_no", ""),
            "tax_id": rec.get("tax_id", ""),
            "count": rec.get("count", len(rec.get("articles", []))),
            "sources": rec.get("sources", {}),
            "note": rec.get("note", ""),
            "size": os.path.getsize(path),
        })
        if len(rows) >= limit:
            break
    return rows


def load_run(rec_id: str) -> Optional[dict]:
    path = os.path.join(history_dir(), rec_id + ".json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def delete_run(rec_id: str) -> bool:
    path = os.path.join(history_dir(), rec_id + ".json")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def record_json(rec_id: str) -> str:
    rec = load_run(rec_id)
    return json.dumps(rec, ensure_ascii=False, indent=2) if rec else "{}"


def export_zip(rec_ids: Optional[Iterable[str]] = None) -> bytes:
    """把指定（或全部）歷史紀錄打包成 ZIP。"""
    ids = list(rec_ids) if rec_ids is not None else [r["id"] for r in list_runs()]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        index_lines = ["儲存時間\t公司\t年度\t案號\t統編\t篇數"]
        for rid in ids:
            rec = load_run(rid)
            if not rec:
                continue
            zf.writestr("%s.json" % rid,
                        json.dumps(rec, ensure_ascii=False, indent=2))
            index_lines.append("\t".join([
                rec.get("saved_at", ""), rec.get("company", ""),
                str(rec.get("year", "")), str(rec.get("case_no", "")),
                str(rec.get("tax_id", "")), str(rec.get("count", ""))]))
        zf.writestr("索引.tsv", "\n".join(index_lines))
    return buf.getvalue()
