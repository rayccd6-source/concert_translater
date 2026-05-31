"""
Test 2: translator JSON parsing and suspicious-result detection.
Does not require google-genai (uses pure parsing functions only).
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("GEMINI_API_KEY", "dummy")

from translator import parse_translation_response, is_suspicious_translation


def test_clean_json():
    raw = '{"original": "hi", "thai": "T1", "malay": "M1", "indonesian": "I1", "filipino": "F1"}'
    r = parse_translation_response(raw)
    assert r is not None
    assert r["original"] == "hi"
    assert r["thai"] == "T1"
    assert r["malay"] == "M1"
    assert r["indonesian"] == "I1"
    assert r["filipino"] == "F1"
    print("[OK]  clean_json")


def test_markdown_wrapped():
    raw = '```json\n{"original": "hi", "thai": "T", "malay": "M", "indonesian": "I", "filipino": "F"}\n```'
    r = parse_translation_response(raw)
    assert r is not None
    assert r["thai"] == "T"
    assert r["filipino"] == "F"
    print("[OK]  markdown_wrapped")


def test_missing_field():
    raw = '{"original": "x", "thai": "T", "malay": "M"}'
    r = parse_translation_response(raw)
    assert r is not None
    assert r["indonesian"] == ""
    assert r["filipino"] == ""
    print("[OK]  missing_field")


def test_invalid_json():
    assert parse_translation_response("nope nothing here") is None
    print("[OK]  invalid_json")


def test_empty_input():
    assert parse_translation_response("") is None
    assert parse_translation_response("   ") is None
    print("[OK]  empty_input")


def test_fallback_original():
    raw = '{"thai": "T", "malay": "M", "indonesian": "I", "filipino": "F"}'
    r = parse_translation_response(raw, fallback_original="ORIG")
    assert r is not None
    assert r["original"] == "ORIG"
    print("[OK]  fallback_original")


def test_suspicious_all_same():
    data = {"thai": "Halo semua", "malay": "Halo semua",
            "indonesian": "Halo semua", "filipino": "Halo semua"}
    assert is_suspicious_translation(data) is True
    print("[OK]  suspicious_all_same")


def test_suspicious_three_same():
    data = {"thai": "Halo semua", "malay": "Halo semua",
            "indonesian": "Halo semua", "filipino": "Kumusta"}
    assert is_suspicious_translation(data) is True
    print("[OK]  suspicious_three_same")


def test_normal_translation_not_suspicious():
    data = {"thai": "T1", "malay": "M1", "indonesian": "I1", "filipino": "F1"}
    assert is_suspicious_translation(data) is False
    print("[OK]  normal_not_suspicious")


def test_two_same_two_different_not_suspicious():
    # Two pairs - not flagged
    data = {"thai": "A", "malay": "A", "indonesian": "B", "filipino": "C"}
    assert is_suspicious_translation(data) is False
    print("[OK]  two_pairs_not_suspicious")


def test_only_one_field_filled():
    data = {"thai": "T", "malay": "", "indonesian": "", "filipino": ""}
    assert is_suspicious_translation(data) is False
    print("[OK]  only_one_field_filled")


if __name__ == "__main__":
    test_clean_json()
    test_markdown_wrapped()
    test_missing_field()
    test_invalid_json()
    test_empty_input()
    test_fallback_original()
    test_suspicious_all_same()
    test_suspicious_three_same()
    test_normal_translation_not_suspicious()
    test_two_same_two_different_not_suspicious()
    test_only_one_field_filled()
    print("\nAll tests passed")
