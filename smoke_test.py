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

    print("\n結果：%s" % ("全部通過" if FAIL == 0 else "%d 項失敗" % FAIL))
    sys.exit(1 if FAIL else 0)
