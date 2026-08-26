# -*- coding: utf-8 -*-
"""
命令列版：輸入公司名稱 -> 蒐集新聞 -> 產出 Word 檔

用法
----
    python main.py --company 台灣禾邦電子有限公司 --year 115 --case A-I-001 --tax-id 54955208

常用參數
--------
    --keywords 華新科,焦佑衡      額外關鍵字（逗號分隔）
    --related 母公司,轉投資公司    母公司（投資人）與相關企業，一併檢索
    --require-topic               只保留命中關注議題（關稅／地緣政治／供應鏈…）的新聞
    --years 2                     蒐集近幾年（預設 2）
    --max 15                      最多幾篇（預設 15）
    --outdir .                    輸出資料夾
    --json out.json               另存原始資料，方便人工挑選後重跑
    --no-doc                      只產 .docx，不轉 .doc
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from crawler import Article, collect_news
from docbuilder import build_docx, convert_to_doc, make_filename


def _progress(msg: str, pct: float) -> None:
    sys.stdout.write("\r[%3d%%] %-60s" % (int(pct * 100), msg[:60]))
    sys.stdout.flush()
    if pct >= 1.0:
        sys.stdout.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="公司新聞蒐集 -> 訪視報告新聞附件產生器")
    ap.add_argument("--company", required=True, help="目標公司全名")
    ap.add_argument("--year", default="115", help="年度，例如 115")
    ap.add_argument("--case", default="", help="案號，例如 A-I-001")
    ap.add_argument("--tax-id", default="", help="統一編號")
    ap.add_argument("--keywords", default="", help="額外關鍵字，逗號分隔")
    ap.add_argument("--related", default="",
                    help="母公司（投資人）與相關企業／轉投資公司，逗號分隔")
    ap.add_argument("--topic-boost", type=int, default=120,
                    help="命中關注議題的加權天數（預設 120；設 0 = 不加權）")
    ap.add_argument("--require-topic", action="store_true",
                    help="只保留命中關注議題的新聞")
    ap.add_argument("--official-url", default="",
                    help="公司官網網址，會一併抓官網最新消息")
    ap.add_argument("--priority-boost", type=int, default=180,
                    help="優先來源的新鮮度加權天數（預設 180；設 0 = 純依日期，不分來源）")
    ap.add_argument("--priority-domains", default="",
                    help="第一優先來源網域，逗號分隔；預設 money.udn.com,ctee.com.tw,news.cnyes.com")
    ap.add_argument("--years", type=float, default=2.0, help="蒐集近幾年（預設 2）")
    ap.add_argument("--max", type=int, default=15, help="最多納入幾篇")
    ap.add_argument("--outdir", default=".", help="輸出資料夾")
    ap.add_argument("--json", dest="json_path", default="", help="另存 JSON 原始資料")
    ap.add_argument("--from-json", default="", help="改由既有 JSON 產檔，不重新爬取")
    ap.add_argument("--section-title", default="八.近兩年相關新聞")
    ap.add_argument("--source-style", default="auto", choices=["auto", "web", "print"])
    ap.add_argument("--no-cnyes", action="store_true", help="不使用鉅亨網來源")
    ap.add_argument("--no-doc", action="store_true", help="不轉成 .doc")
    ap.add_argument("--delay", type=float, default=0.8, help="請求間隔秒數")
    args = ap.parse_args()

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            raw = json.load(fh)
        arts = []
        for d in raw:
            a = Article(title=d["title"], url=d.get("url", ""),
                        source=d.get("source", ""), author=d.get("author", ""),
                        subtitle=d.get("subtitle", ""),
                        paragraphs=d.get("paragraphs", []))
            if d.get("published"):
                from datetime import datetime
                a.published = datetime.strptime(d["published"], "%Y-%m-%d")
            arts.append(a)
        keywords = []
    else:
        extra = [k.strip() for k in args.keywords.split(",") if k.strip()]
        doms = [d.strip() for d in args.priority_domains.split(",") if d.strip()] or None
        arts, keywords = collect_news(
            args.company, extra_keywords=extra, years=args.years,
            max_articles=args.max, use_cnyes=not args.no_cnyes,
            official_url=args.official_url,
            related_companies=[r.strip() for r in args.related.split(",") if r.strip()],
            topic_boost_days=args.topic_boost, require_topic=args.require_topic,
            priority_domains=doms,
            priority_boost_days={1: args.priority_boost,
                                 2: args.priority_boost // 3, 3: 0},
            progress=_progress, delay=args.delay)

    if not arts:
        print("找不到符合條件的新聞，請放寬年限或補充關鍵字。")
        return 1

    print("\n使用關鍵字：%s" % "、".join(keywords))
    print("納入 %d 篇：" % len(arts))
    for i, a in enumerate(arts, 1):
        print("  %2d. [%s] %s  %s  (%s，%d 字)%s%s"
              % (i, "★" if a.source in ("經濟日報", "工商時報", "鉅亨網")
                 or a.source.endswith("官網") else " ",
                 a.date_str, a.title, a.source, a.char_count,
                 "  〔%s〕" % a.entity if a.entity and a.entity != args.company else "",
                 "  #" + " #".join(a.topics[:4]) if a.topics else ""))

    os.makedirs(args.outdir or ".", exist_ok=True)

    if args.json_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_path)), exist_ok=True)
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump([a.to_dict() for a in arts], fh, ensure_ascii=False, indent=2)
        print("\n原始資料已存：%s" % args.json_path)

    base = make_filename(args.year, args.case, args.tax_id, args.company, ext="")
    docx_path = os.path.join(args.outdir, base + ".docx")
    build_docx(arts, docx_path, section_title=args.section_title,
               source_style=args.source_style, company=args.company)
    print("\n已產出：%s" % docx_path)

    if not args.no_doc:
        doc_path = convert_to_doc(docx_path, os.path.join(args.outdir, base + ".doc"))
        if doc_path:
            print("已轉檔：%s（目錄頁碼已更新）" % doc_path)
        else:
            print("未轉 .doc（本機無 Word 或轉檔失敗）；.docx 可直接用 Word 開啟，"
                  "開檔時會自動更新目錄頁碼。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
