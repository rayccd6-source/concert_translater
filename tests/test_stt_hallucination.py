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
        ("Xin chào các bạn", False),  # 越南文
        ("好的", False),
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
