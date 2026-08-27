# -*- coding: utf-8 -*-
"""
冒煙測試：實際執行 app.py 並檢查有無例外。

    python smoke_test.py

注意：`streamlit run` 起得來不代表程式沒錯——Streamlit 的健康檢查在主程式執行前就
回 200，腳本要等瀏覽器連上才跑。所以改用 AppTest 真的把腳本執行一遍，NameError、
ImportError 這類問題才抓得到。本測試不連網，只驗證頁面初始化與模組版本一致性。
"""

from __future__ import annotations

import sys

FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAIL
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not ok:
        FAIL += 1


if __name__ == "__main__":
    print("1. 模組匯入與版本一致性")
    import crawler
    import docbuilder

    versions = {"crawler": crawler.__version__,
                "docbuilder": docbuilder.__version__}
    check("兩個模組都能匯入", True, str(versions))
    check("版本一致", len(set(versions.values())) == 1, str(versions))

    src = open("app.py", encoding="utf-8").read()
    app_ver = ""
    for line in src.split("\n"):
        if line.startswith("APP_VERSION"):
            app_ver = line.split("=", 1)[1].strip().strip('"')
            break
    check("app.py 的 APP_VERSION 與模組一致",
          app_ver == crawler.__version__, "app=%s 模組=%s" % (app_ver, crawler.__version__))

    print("2. 實際執行 app.py")
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError:
        check("streamlit.testing 可用", False, "streamlit 版本過舊，跳過")
        sys.exit(1 if FAIL else 0)

    at = AppTest.from_file("app.py", default_timeout=90).run()
    if at.exception:
        for e in at.exception:
            print("     ", e.value)
    check("執行時無例外", not at.exception)
    check("頁面有標題", len(at.title) > 0 or len(at.header) > 0)
    check("側欄有輸入欄位", len(at.sidebar.text_input) >= 3,
          "text_input=%d" % len(at.sidebar.text_input))

    print("3. 重新蒐集（清空上一次的結果與修訂）")
    from crawler import Article

    EDITED = "使用者改過的標題"

    def _fake():
        return [Article(title="B公司法說會", source="鉅亨網", paragraphs=["新內容"]),
                Article(title="B公司接單", source="工商時報", paragraphs=["新內容2"])]

    def _titles(t):
        return [w.value for w in t.text_input
                if str(w.key or "").startswith("title_")]

    at = AppTest.from_file("app.py", default_timeout=90)
    at.session_state["articles"] = _fake()
    at.session_state["company"] = "B公司"
    at.run()
    labels = [b.label for b in at.button]
    check("有「重新蒐集」按鈕", any("重新蒐集" in n for n in labels), str(labels))
    if any("重新蒐集" in n for n in labels):
        [b for b in at.button if "重新蒐集" in b.label][0].click().run()
        check("點擊後無例外", not at.exception)
        check("結果已清空", at.session_state["articles"] == [])

    # 對照組：殘留的 title_／pick_ 會蓋掉新結果——這是 clear_results() 要解決的問題，
    # 先確認這個機制真的存在，否則下面的修復組恆真、測不出任何東西。
    at = AppTest.from_file("app.py", default_timeout=90)
    at.session_state["title_0"] = EDITED
    at.session_state["articles"] = _fake()
    at.session_state["company"] = "B公司"
    at.run()
    leak = _titles(at)
    check("（對照）殘留確實會汙染新結果", bool(leak) and leak[0] == EDITED, str(leak))

    # 修復組：走 _do_reset，即「重新蒐集」按鈕做的事
    at = AppTest.from_file("app.py", default_timeout=90)
    at.session_state["title_0"] = EDITED
    at.session_state["pick_1"] = False
    at.session_state["docx_bytes"] = b"fake"
    at.session_state["_do_reset"] = True
    at.run()
    check("舊的 docx_bytes 已清掉",
          not at.session_state.filtered_state.get("docx_bytes"))
    arts = _fake()
    at.session_state["articles"] = arts
    at.session_state["company"] = "B公司"
    at.run()
    check("清空後新結果不帶入舊修訂",
          _titles(at) == ["B公司法說會", "B公司接單"], str(_titles(at)))
    check("清空後勾選回到全選",
          all(at.session_state["pick_%d" % i] for i in range(2)))
    check("Article 物件標題未被汙染",
          [a.title for a in arts] == ["B公司法說會", "B公司接單"],
          str([a.title for a in arts]))

    print("4. 產檔時濾掉 XML 不接受的控制字元")
    import os
    import tempfile

    import docbuilder as _db

    BAD = "\x07"        # BEL：肉眼看不到，但 lxml 會直接拒收
    dirty = [Article(title="A公司" + BAD + "利多消息", source="經濟日報",
                     url="https://example.com/1",
                     subtitle="副標" + BAD,
                     paragraphs=["內容裡夾了控制字元" + BAD + "，句子夠長會走內文樣式。",
                                 "第二段" + chr(0) + "含 NUL。"])]

    # 對照組：先確認不清理真的會炸，否則下面的檢查測不出東西
    _orig = _db.xml_safe
    _db.xml_safe = lambda t: ("" if not t else str(t))
    try:
        with tempfile.TemporaryDirectory() as td:
            _db.build_docx(dirty, os.path.join(td, "t.docx"))
        crashed = False
    except Exception:       # noqa: BLE001
        crashed = True
    finally:
        _db.xml_safe = _orig
    check("（對照）不清理確實會產檔失敗", crashed)

    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "t.docx")
        try:
            _db.build_docx(dirty, out, company="測試公司")
            ok = os.path.exists(out)
        except Exception as e:      # noqa: BLE001
            ok = False
            print("     ", e)
        check("含控制字元也能產出 .docx", ok)
        if ok:
            from docx import Document as _Doc
            texts = [p.text for p in _Doc(out).paragraphs]
            residue = [t for t in texts
                       if any(ord(c) < 32 and c not in "\t\n" for c in t)]
            check("產出的內容沒有殘留控制字元", not residue, str(residue))
            check("標題本身仍完整", any("A公司利多消息" in t for t in texts),
                  str([t for t in texts if "A公司" in t]))

    check("xml_safe 保留正常文字",
          _db.xml_safe("台灣禾邦電子\n第二行\t欄位") == "台灣禾邦電子\n第二行\t欄位")
    check("xml_safe 正規化 CRLF", _db.xml_safe("a\r\nb") == "a\nb")

    print("5. 排除「相關文章／推薦閱讀」版面元件")
    from bs4 import BeautifulSoup

    # 仿 TechOrange（Elementor）的版面：正文 <p> 之後接一組推薦文章，標題是 <h3>。
    # 這些標題夠長，事後靠字數與標點猜測的 _trim_tail_noise() 砍不掉。
    REC = "機器人基礎模型市場價值上看 1,500 億美元：瑞士新創如何打造會拆解任務的 AI 大腦？"
    HTML = """
    <html><body><div class="post">
      <div class="elementor-widget-theme-post-content"><div>
        <p>鴻海科技集團與科技報橘首次聯合舉辦台灣 AI 機器人高峰會，探討產業趨勢與落地應用。</p>
        <h2>鴻海目標從勞動力密集轉化為 AI 密集</h2>
        <p>史喆說明，儘管多數企業在加工與倉儲物流方面已經實現自動化，但系統組裝環節仍是挑戰。</p>
        <p>劉冠良表示，訓練 Physical AI 的基礎模型所需要的數據，不像語言模型擁有數萬億筆數據。</p>
      </div></div>
      <div class="elementor-posts-container">
        <article class="elementor-post"><h3 class="elementor-post__title">%s</h3></article>
        <article class="elementor-post"><h3 class="elementor-post__title">南韓砸逾 8,800 億美元打造 AI 國家隊：拆解台、日、韓的 AI 國力競賽</h3></article>
      </div>
    </div></body></html>
    """ % REC

    def _extract(strip: bool):
        s = BeautifulSoup(HTML, "html.parser")
        if strip:
            crawler.strip_noise(s)
        return crawler._densest_block(s)

    dirty_paras = _extract(strip=False)
    check("（對照）不清理時推薦文章會混進內文",
          any(REC[:12] in p for p in dirty_paras), str(dirty_paras[-1:]))

    clean_paras = _extract(strip=True)
    check("清理後推薦文章不再出現",
          not any(REC[:12] in p for p in clean_paras), str(clean_paras[-1:]))
    check("清理後正文段落完整保留", len(clean_paras) == 4,
          "%d 段：%s" % (len(clean_paras), [p[:14] for p in clean_paras]))
    check("小標（h2）仍保留",
          any("勞動力密集" in p for p in clean_paras), str([p[:14] for p in clean_paras]))

    print("6. 清除輸入（重設案件欄位，保留調校設定）")
    IN_KEYS = ["in_company", "in_case_no", "in_tax_id", "in_extra",
               "in_related", "in_official_url"]

    at = AppTest.from_file("app.py", default_timeout=90)
    at.run()
    # 使用者調整過的檢索設定：這些刻意沒給 in_ key，清除時不該被歸零
    at.sidebar.slider[0].set_value(300).run()
    at.sidebar.number_input[0].set_value(5.0).run()
    for k in IN_KEYS:
        at.session_state[k] = "填過的值"
    at.session_state["in_year"] = "999"
    at.session_state["articles"] = _fake()
    at.session_state["company"] = "B公司"
    at.run()

    labels = [b.label for b in at.sidebar.button]
    check("側欄有「清除輸入」按鈕", any("清除" in n for n in labels), str(labels))
    tuned = (at.sidebar.slider[0].value, at.sidebar.number_input[0].value)

    if any("清除" in n for n in labels):
        [b for b in at.sidebar.button if "清除" in b.label][0].click().run()
        check("點擊後無例外", not at.exception,
              str(at.exception[0].value) if at.exception else "")
        fs = at.session_state.filtered_state
        left = {k: fs.get(k) for k in IN_KEYS if fs.get(k)}
        check("案件欄位已清空", not left, str(left))
        check("年度回到預設 115", fs.get("in_year") == "115", repr(fs.get("in_year")))
        check("蒐集結果一併清空", at.session_state["articles"] == [])
        check("調校設定保留未被歸零",
              (at.sidebar.slider[0].value, at.sidebar.number_input[0].value) == tuned,
              "清除前 %s → 清除後 %s"
              % (tuned, (at.sidebar.slider[0].value, at.sidebar.number_input[0].value)))

    print("7. 濾掉推薦新聞的連結清單（連結密度）")
    LINKS = ("<a href='/1'>騎驢找馬？高為元續任投票過關3天就赴美選校長 清大教授怒：誠信問題</a> "
             "▪ <a href='/2'>續任投票僅3天…清大校長高為元赴美選校長挨批渣男 校方說話了</a> "
             "▪ <a href='/3'>分科錄取門檻下修！台大醫不採國文估這級分可上</a>")
    TAIL_LINK = "<a href='/9'>台積電加減碼分歧、波克夏加碼 Alphabet…美機構第2季科技股大調整</a>"
    ART = """
    <div><section class="body">
      <p>台灣半導體產業全球領先，四大業者在全球市占率近 75%%，卻面臨嚴峻的人才缺口。</p>
      <p>高雄科技大學半導體製程設備技術人才培育基地今在楠梓校區啟用，每年培訓逾千名學生。</p>
      <p>在產能持續擴張、人才需求孔急之際，這座基地的成立象徵南部半導體廊道再添戰力。</p>
      <div>【文教熱話題】<p>%s</p></div>
      <p>%s</p>
    </section></div>
    """ % (LINKS, TAIL_LINK)

    def _paras(html):
        s = BeautifulSoup(html, "html.parser")
        return crawler._paragraphs_from(s.select_one("section.body"))

    got = _paras(ART)
    check("多則擠成一段的連結清單被濾掉",
          not any("騎驢找馬" in p for p in got), str([p[:16] for p in got]))
    check("文末的單則連結段落也被濾掉",
          not any("加減碼分歧" in p for p in got), str([p[:16] for p in got]))
    check("正文三段完整保留", len(got) == 3,
          "%d 段：%s" % (len(got), [p[:14] for p in got]))

    # 對照組一：同樣的文字但不是連結 —— 必須留著，否則就是靠字面猜測而非結構
    plain = ART.replace("<a href='/1'>", "").replace("<a href='/2'>", "") \
               .replace("<a href='/3'>", "").replace("<a href='/9'>", "") \
               .replace("</a>", "")
    got_plain = _paras(plain)
    check("（對照）純文字不含連結時不會被誤刪", len(got_plain) == 5,
          "%d 段：%s" % (len(got_plain), [p[:14] for p in got_plain]))

    # 對照組二：正文中夾帶少量連結的正常段落不受影響
    MIXED = """<div><section class="body">
      <p>根據 <a href='/x'>台積電</a> 法說會說明，先進封裝產能供不應求，訂單能見度已看到明年下半年。</p>
      <p>業界指出，先進封裝瓶頸改善也能讓供應鏈與零組件動能增強，載板廠可望同步受惠。</p>
    </section></div>"""
    check("（對照）夾帶少量連結的正文不受影響", len(_paras(MIXED)) == 2,
          str([p[:16] for p in _paras(MIXED)]))

    print("8. 來源體例：web 完整承接舊 auto 的行為")
    from datetime import datetime

    def _old_auto(a):
        """舊版 auto 的邏輯，原樣重寫一份當基準——不能拿新程式碼自己驗自己。"""
        date, src = a.date_str, a.source or ""
        author = (a.author or "").strip()
        if not a.url:
            head = "【%s】" % "/".join(x for x in [date, src] if x)
            return [head + ("【%s】" % author if author else "")]
        bits = [x for x in [src, date] if x]
        if author:
            bits.append("記者%s 報導" % author
                        if not author.startswith("記者") else author)
        lines = [a.url]
        if bits:
            lines.append(" ".join(bits))
        return lines

    D = datetime(2025, 7, 31)
    cases = [
        ("有網址有記者", Article(title="t", url="https://x/1", source="經濟日報",
                            author="尹慧中", published=D)),
        ("有網址無記者", Article(title="t", url="https://x/1", source="經濟日報",
                            published=D)),
        ("無網址有記者", Article(title="t", url="", source="經濟日報",
                            author="尹慧中", published=D)),
        ("無網址無記者", Article(title="t", url="", source="經濟日報", published=D)),
        ("無網址無日期", Article(title="t", url="", source="經濟日報")),
    ]
    diffs = [n for n, a in cases
             if _db.source_lines(a, "web") != _old_auto(a)]
    check("web 的輸出與舊 auto 逐案一致", not diffs, str(diffs))

    # 有網址時 web 與 print 必須不同，否則這兩個選項也是重複的
    with_url = cases[0][1]
    check("有網址時 web 與 print 仍有區別",
          _db.source_lines(with_url, "web") != _db.source_lines(with_url, "print"),
          str(_db.source_lines(with_url, "web")))
    check("無網址時 web 退回【】形式",
          _db.source_lines(cases[2][1], "web") == _db.source_lines(cases[2][1], "print"),
          str(_db.source_lines(cases[2][1], "web")))

    at = AppTest.from_file("app.py", default_timeout=90).run()
    # 注意 AppTest 的 options 回傳的是 format_func 之後的顯示文字，不是原始值
    opts = [o for s in at.sidebar.selectbox for o in s.options]
    check("下拉選單剩兩個選項", len(opts) == 2, str(opts))
    check("已無「自動」選項", not any("自動" in o for o in opts), str(opts))
    check("預設仍是 web（等同原本的自動）",
          at.sidebar.selectbox[0].value == "web", str(at.sidebar.selectbox[0].value))

    print("9. 關注議題清單顯示在說明欄")
    import importlib

    import app as _app
    _app = importlib.reload(_app)

    topics = list(crawler.TOPIC_KEYWORDS)
    regions = list(crawler.SUPPLY_CHAIN_REGIONS)
    missing = [t for t in topics if t not in _app.TOPIC_HELP]
    check("14 類議題全部出現在說明文字", not missing,
          "缺少：%s" % missing if missing else "共 %d 類" % len(topics))
    check("供應鏈轉移的地區也列出",
          all(r in _app.TOPIC_HELP for r in regions), str(regions))

    at = AppTest.from_file("app.py", default_timeout=90).run()
    joined = chr(10).join(str(w.help or "") for w in at.sidebar.slider)
    check("議題加權滑桿的說明欄含完整清單",
          all(t in joined for t in topics),
          "缺少：%s" % [t for t in topics if t not in joined])
    # 「只保留命中議題」核取方塊刻意不放 help：同一份清單在滑桿說明與首頁都有
    only = [w for w in at.sidebar.checkbox if "只保留" in str(w.label)]
    check("找得到「只保留命中議題」核取方塊", len(only) == 1,
          str([str(w.label) for w in at.sidebar.checkbox]))
    if only:
        check("該核取方塊已不再掛說明欄", not (only[0].help or ""),
              repr(only[0].help)[:60])

    # 說明是由詞庫產生、不是另抄一份：塞一個假議題進詞庫，說明必須跟著出現
    crawler.TOPIC_KEYWORDS["測試用假議題"] = ["測試用假議題"]
    try:
        _app2 = importlib.reload(_app)
        picked_up = "測試用假議題" in _app2.TOPIC_HELP
        check("（對照）詞庫新增議題時說明自動跟上", picked_up,
              "" if picked_up else "說明未更新，可能是硬寫死的清單")
    finally:
        crawler.TOPIC_KEYWORDS.pop("測試用假議題", None)
        importlib.reload(_app)

    print("10. 使用方式頁面顯示議題一覽表")
    at = AppTest.from_file("app.py", default_timeout=90).run()
    page = "\n".join([m.value for m in at.markdown]
                     + [c.value for c in at.caption])
    check("落地頁有「關注議題」段落", "關注議題（共" in page,
          page[:60].replace("\n", " "))
    missing = [t for t in topics if t not in page]
    check("14 類議題全部列在頁面上", not missing, "缺少：%s" % missing)
    check("表格附上主要關鍵詞",
          "加徵關稅" in page and "人形機器人" in page and "碳關稅" in page)
    check("供應鏈轉移的地區與動作詞條件有說明",
          all(r in page for r in regions) and "同時出現" in page)
    check("標註了關鍵詞比對而非語意判讀", "不是語意判讀" in page)

    # 有結果時是檢視畫面，不該再佔版面
    at2 = AppTest.from_file("app.py", default_timeout=90)
    at2.session_state["articles"] = _fake()
    at2.session_state["company"] = "B公司"
    at2.run()
    page2 = "\n".join([m.value for m in at2.markdown]
                      + [c.value for c in at2.caption])
    check("（對照）有蒐集結果時不顯示議題表", "關注議題（共" not in page2)

    print("\n結果：%s" % ("全部通過" if FAIL == 0 else "%d 項失敗" % FAIL))
    sys.exit(1 if FAIL else 0)
