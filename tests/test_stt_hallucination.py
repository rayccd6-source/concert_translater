"""
測試 3：STT 模組的幻覺過濾邏輯
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("GEMINI_API_KEY", "dummy")

from stt import _is_hallucination


def test_filter_hallucinations():
    cases = [
        # (輸入, 預期是否被過濾)
        ("謝謝觀看", True),
        ("請訂閱我的頻道", True),
        ("Thanks for watching!", True),
        ("字幕由 XX 製作", True),
        ("哈哈哈哈哈哈哈哈", True),     # 重複字
        ("a", True),                  # 太短
        ("", True),                   # 空字串
        ("大家好歡迎來到今晚的演唱會", False),
        ("Xin chao cac ban", False),  # 越南文（去聲調避免終端機編碼問題）
        ("好的", False),
        # === 新增：變體字與整句精確比對 ===
        ("謝謝收看", True),            # 收看變體
        ("感謝收看", True),
        ("YOYO 獨播劇場", True),       # 獨播劇場子字串
        ("Thank you.", True),         # 整句精確比對（含標點）
        ("謝謝。", True),             # 整句精確比對（中文標點）
        ("bye bye", True),
        ("謝謝大家今天來到演唱會現場", False),  # 含「謝謝大家」但是真話，不能誤殺
        ("thank you everyone welcome to the show", False),  # 含 thank you 但是真話
    ]
    fail = 0
    for text, expected in cases:
        actual = _is_hallucination(text)
        ok = actual == expected
        mark = "OK" if ok else "FAIL"
        print(f"[{mark}] {text!r} → 過濾={actual}（預期 {expected}）")
        if not ok:
            fail += 1
    if fail:
        raise AssertionError(f"{fail} 個案例失敗")
    print("\n全部測試通過")


if __name__ == "__main__":
    test_filter_hallucinations()
