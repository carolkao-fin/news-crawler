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

    print("\n結果：%s" % ("全部通過" if FAIL == 0 else "%d 項失敗" % FAIL))
    sys.exit(1 if FAIL else 0)
