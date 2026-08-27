# -*- coding: utf-8 -*-
"""
網頁版：公司新聞蒐集 → 訪視報告「近兩年相關新聞」Word 產生器

本機執行：
    streamlit run app.py

部署到 Streamlit Community Cloud：
    把本資料夾（app.py / crawler.py / docbuilder.py / requirements.txt）推上
    GitHub，到 share.streamlit.io 選 repo，主程式填 app.py 即可。
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import crawler as _crawler          # noqa: E402
import docbuilder as _docbuilder    # noqa: E402

APP_VERSION = "1.5.5"

# Streamlit Cloud 在 repo 更新時會重跑主程式，但已 import 的模組仍留在 sys.modules，
# 於是 app.py 是新版、crawler.py 是舊版，呼叫時就 TypeError。版本不符就強制重載。
if (getattr(_crawler, "__version__", "") != APP_VERSION
        or getattr(_docbuilder, "__version__", "") != APP_VERSION):
    _crawler = importlib.reload(_crawler)
    _docbuilder = importlib.reload(_docbuilder)

collect_news = _crawler.collect_news
make_keywords = _crawler.make_keywords
Article = _crawler.Article
build_docx = _docbuilder.build_docx
convert_to_doc = _docbuilder.convert_to_doc
make_filename = _docbuilder.make_filename

st.set_page_config(page_title="公司新聞蒐集｜近兩年相關新聞產生器",
                   page_icon="📰", layout="wide")

if getattr(_crawler, "__version__", "") != APP_VERSION:
    st.error("程式模組版本不一致（app %s／crawler %s）。請到右下角 Manage app → "
             "Reboot app 重啟一次。" % (APP_VERSION,
                                    getattr(_crawler, "__version__", "未知")))
    st.stop()

st.title("📰 公司新聞蒐集 → 訪視報告新聞附件")
st.caption("輸入目標公司，自動以關鍵字蒐集近兩年新聞，產出與 "
           "`115_A-I-001_54955208_台灣禾邦電子有限公司.doc` 相同版面的 Word 檔。")

if "articles" not in st.session_state:
    st.session_state.articles = []
    st.session_state.keywords = []
    st.session_state.company = ""


def clear_results() -> None:
    """清掉上一次的蒐集結果與所有就地修訂。

    勾選狀態、標題與內文都是以 `pick_i`／`title_i`／`body_i` 存在 session_state，
    不清掉的話下一次蒐集會沿用同一組 key：Streamlit 遇到已存在的 key 會忽略
    `value=`，於是上一家公司改過的標題與內文會套到新結果的同一個序號上。
    """
    for k in list(st.session_state.keys()):
        if str(k).startswith(("pick_", "title_", "body_")):
            del st.session_state[k]
    st.session_state.articles = []
    st.session_state.keywords = []
    st.session_state.company = ""
    st.session_state.pop("docx_bytes", None)
    st.session_state.pop("doc_bytes", None)


def clear_inputs() -> None:
    """清掉左側欄的案件輸入欄位，讓使用者重新輸入下一件。

    只清「每一件都不一樣」的欄位（公司、年度、案號、統編、關鍵字、相關企業、
    官網），檢索與輸出設定那些調校參數保持不動——那是使用者調過一次就想沿用的，
    每換一家公司就被歸零反而礙事。清法是刪掉 widget 的 key，下一次執行時
    widget 會回到自己的預設值。
    """
    for k in list(st.session_state.keys()):
        if str(k).startswith("in_"):
            del st.session_state[k]


# 重設一律延到下一次執行的最開頭做：此時那些 widget 還沒被建立，刪 key 不會和
# 「widget 已實體化」的限制打架。
if st.session_state.pop("_do_reset", False):
    clear_results()
if st.session_state.pop("_do_clear_inputs", False):
    clear_inputs()


# --------------------------------------------------------------------------- #
# 輸入區
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("案件資訊")
    # 這幾個欄位每一件都不一樣，都給 in_ 開頭的 key，「清除輸入」才有辦法重設。
    company = st.text_input("公司全名 *", placeholder="例：台灣禾邦電子有限公司",
                            key="in_company")
    year = st.text_input("年度", value="115", key="in_year")
    case_no = st.text_input("案號", placeholder="例：A-I-001", key="in_case_no")
    tax_id = st.text_input("統一編號", placeholder="例：54955208", key="in_tax_id")

    st.header("檢索設定")
    extra = st.text_input("額外關鍵字（逗號分隔）",
                          placeholder="例：華新科,焦佑衡,精成科",
                          key="in_extra")
    related = st.text_area(
        "母公司／相關企業（每行一家）",
        placeholder="例：\n華新麗華股份有限公司\n精成科技股份有限公司",
        help="母公司（投資人）、受訪公司之相關企業或轉投資公司，會各自展開關鍵字檢索。",
        height=90, key="in_related")
    official_url = st.text_input("公司官網網址（選填）",
                                 placeholder="例：https://www.example.com.tw",
                                 help="填了會一併抓官網「最新消息／新聞中心」。",
                                 key="in_official_url")
    col_a, col_b = st.columns(2)
    with col_a:
        years = st.number_input("近幾年", 0.5, 10.0, 2.0, 0.5)
    with col_b:
        max_articles = st.number_input("最多篇數", 1, 60, 15, 1)
    use_cnyes = st.checkbox("同時檢索鉅亨網", value=True)
    prio = st.multiselect(
        "第一優先來源（定向檢索）",
        ["money.udn.com", "ctee.com.tw", "news.cnyes.com", "moneydj.com",
         "ltn.com.tw", "chinatimes.com", "technews.tw", "cna.com.tw"],
        default=["money.udn.com", "ctee.com.tw", "news.cnyes.com"],
        format_func=lambda d: {"money.udn.com": "經濟日報",
                               "ctee.com.tw": "工商時報",
                               "news.cnyes.com": "鉅亨網",
                               "moneydj.com": "MoneyDJ",
                               "ltn.com.tw": "自由時報",
                               "chinatimes.com": "中時新聞網",
                               "technews.tw": "科技新報",
                               "cna.com.tw": "中央社"}.get(d, d),
        help="這些來源會額外做一次定向檢索，並在排序時加權；其他媒體仍照常蒐集。")
    topic_boost = st.slider("關注議題加權（天）", 0, 365, 120, 15,
                            help="命中關稅／地緣政治／科技戰／出口管制／供應鏈轉移／AI 等"
                                 "議題的新聞會往前排，最多計兩項。這是加權不是門檻，"
                                 "其他題材照樣蒐集。設 0 = 不加權。")
    other_quota = st.slider("保留給其他題材的名額比例", 0.0, 0.6, 0.25, 0.05,
                            help="避免議題加權把一般新聞整批擠掉：這個比例的名額會"
                                 "優先留給未命中議題的新聞。設 0 = 不保留。")
    require_topic = st.checkbox("只保留命中關注議題的新聞", value=False,
                                help="預設不勾：議題只做優先排序，不排除其他題材。")
    boost = st.slider("優先來源加權（天）", 0, 365, 180, 15,
                      help="不是門檻而是加權：第一優先來源等於自動年輕這麼多天，"
                           "第二優先為其 1/3。設 0 就純依日期排序、完全不分來源。")
    delay = st.slider("請求間隔（秒）", 0.3, 3.0, 0.8, 0.1,
                      help="間隔越長越不容易被來源網站擋，但速度較慢。")

    st.header("輸出設定")
    section_title = st.text_input("章節標題", value="八.近兩年相關新聞")
    source_style = st.selectbox(
        "來源標示體例", ["web", "print"],
        format_func=lambda x: {"web": "網址＋媒體 日期",
                               "print": "【日期/媒體】【記者】"}[x],
        help="沒有網址的新聞，「網址＋媒體 日期」會自動退回【日期/媒體】形式。")

    run = st.button("開始蒐集", type="primary", use_container_width=True)
    if st.button("🧹 清除輸入", use_container_width=True,
                 help="清空上方所有案件欄位與蒐集結果，準備輸入下一家公司。"
                      "檢索設定與輸出設定會保留。"):
        st.session_state["_do_clear_inputs"] = True
        st.session_state["_do_reset"] = True
        st.rerun()

if company:
    st.info("將使用的關鍵字：" + "、".join(make_keywords(
        company, [k.strip() for k in extra.split(",") if k.strip()])))


# --------------------------------------------------------------------------- #
# 蒐集
# --------------------------------------------------------------------------- #
if run:
    if not company.strip():
        st.error("請先輸入公司全名。")
        st.stop()
    clear_results()
    bar = st.progress(0.0, text="準備中…")

    def _cb(msg: str, pct: float) -> None:
        bar.progress(min(max(pct, 0.0), 1.0), text=msg)

    with st.spinner("蒐集中，請稍候…"):
        arts, kws = collect_news(
            company.strip(),
            extra_keywords=[k.strip() for k in extra.split(",") if k.strip()],
            years=float(years), max_articles=int(max_articles),
            use_cnyes=use_cnyes, official_url=official_url.strip(),
            related_companies=[r.strip() for r in related.splitlines() if r.strip()],
            topic_boost_days=int(topic_boost), require_topic=require_topic,
            other_quota_ratio=float(other_quota),
            priority_domains=prio,
            priority_boost_days={1: int(boost), 2: int(boost) // 3, 3: 0},
            progress=_cb, delay=float(delay))
    bar.empty()
    st.session_state.articles = arts
    st.session_state.keywords = kws
    st.session_state.company = company.strip()
    if not arts:
        st.warning("查無符合條件的新聞，可試著放寬年限或補充關鍵字。")


articles = st.session_state.articles

# --------------------------------------------------------------------------- #
# 結果檢視與挑選
# --------------------------------------------------------------------------- #
if articles:
    st.subheader("蒐集結果（勾選要放進 Word 的新聞）")
    st.caption("共 %d 篇；預設全選。可展開檢視內文，確認無誤再產出。"
               % len(articles))

    c1, c2 = st.columns([1, 6])
    with c1:
        if st.button("全部取消"):
            for i in range(len(articles)):
                st.session_state["pick_%d" % i] = False
        if st.button("全部勾選"):
            for i in range(len(articles)):
                st.session_state["pick_%d" % i] = True

    for i, a in enumerate(articles):
        star = "★ " if (a.source in ("經濟日報", "工商時報", "鉅亨網")
                        or a.source.endswith("官網")) else ""
        tags = ("  " + " ".join("#" + t for t in a.topics[:4])) if a.topics else ""
        ent = ("  〔%s〕" % a.entity
               if a.entity and a.entity != st.session_state.company else "")
        head = "%s%s｜%s｜%s（%d 字）%s%s" % (star, a.date_str or "日期不明",
                                           a.source or "來源不明",
                                           a.title, a.char_count, ent, tags)
        cols = st.columns([1, 20])
        with cols[0]:
            st.checkbox("納入", key="pick_%d" % i, value=True,
                        label_visibility="collapsed")
        with cols[1]:
            with st.expander(head):
                new_title = st.text_input("標題", value=a.title, key="title_%d" % i)
                a.title = new_title
                if a.url:
                    st.write("🔗 %s" % a.url)
                if a.matched:
                    st.caption("命中關鍵字：" + "、".join(a.matched))
                if a.topics:
                    st.caption("關注議題：" + "、".join(a.topics))
                if a.entity:
                    st.caption("檢索對象：" + a.entity)
                if a.fetch_error:
                    st.warning(a.fetch_error)
                st.text_area("內文", value="\n".join(a.paragraphs),
                             height=220, key="body_%d" % i)

    picked = []
    for i, a in enumerate(articles):
        if st.session_state.get("pick_%d" % i, True):
            body = st.session_state.get("body_%d" % i)
            if body is not None:
                a.paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
            picked.append(a)

    st.divider()
    st.subheader("產出 Word 檔")
    st.write("將納入 **%d** 篇。" % len(picked))

    fname_base = make_filename(year, case_no, tax_id,
                               st.session_state.company or company, ext="")
    st.code(fname_base + ".doc", language=None)

    cc1, cc2, cc3 = st.columns(3)

    if cc2.button("🔄 重新蒐集", use_container_width=True,
                  help="清空目前的結果與所有修訂，回到剛進來的狀態；"
                       "左側欄的設定會保留，改完按「開始蒐集」即可。"):
        st.session_state["_do_reset"] = True
        st.rerun()

    if cc1.button("產生 Word 檔", type="primary", disabled=not picked):
        with tempfile.TemporaryDirectory() as td:
            docx_path = os.path.join(td, fname_base + ".docx")
            build_docx(picked, docx_path, section_title=section_title,
                       source_style=source_style,
                       company=st.session_state.company)
            with open(docx_path, "rb") as fh:
                st.session_state.docx_bytes = fh.read()

            doc_path = convert_to_doc(docx_path, os.path.join(td, fname_base + ".doc"))
            if doc_path and os.path.exists(doc_path):
                with open(doc_path, "rb") as fh:
                    st.session_state.doc_bytes = fh.read()
            else:
                st.session_state.doc_bytes = None
        st.success("已產生，請由下方下載。")

    if st.session_state.get("docx_bytes"):
        st.download_button(
            "⬇️ 下載 .docx", st.session_state.docx_bytes,
            file_name=fname_base + ".docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if st.session_state.get("doc_bytes"):
            st.download_button("⬇️ 下載 .doc（Word 97-2003）",
                               st.session_state.doc_bytes,
                               file_name=fname_base + ".doc",
                               mime="application/msword")
        else:
            st.caption("此環境未安裝 Word，僅提供 .docx；用 Word 開啟後另存新檔即可轉成 .doc。"
                       "目錄頁碼會在 Word 開檔時自動更新（或按 Ctrl+A 後 F9）。")

else:
    st.markdown(
        """
        ### 使用方式
        1. 左側輸入 **公司全名**（必填）與年度／案號／統編。
        2. 需要時補上 **額外關鍵字**：集團名、董事長姓名、股票代號、品牌名等，
           可大幅提高命中率。
        3. 按 **開始蒐集**，系統會從 Google News（涵蓋經濟日報、工商時報、
           自由財經、Yahoo、鉅亨、TechNews 等）與鉅亨網搜尋，抓取全文。
        4. 逐則檢視、勾選、必要時直接在頁面上修訂標題與內文。
        5. 按 **產生 Word 檔** 下載，版面與現行訪視報告新聞附件一致。
        """
    )
