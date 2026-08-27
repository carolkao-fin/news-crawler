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

    print("\n結果：%s" % ("全部通過" if FAIL == 0 else "%d 項失敗" % FAIL))
    sys.exit(1 if FAIL else 0)
