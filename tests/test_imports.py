"""
Test 1: verify every backend module imports correctly.
- If failure is caused by a missing third-party package -> SKIP
- If failure is a syntax / logic error -> FAIL
"""
import sys
import os
import importlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("GEMINI_API_KEY", "dummy")

EXTERNAL_DEPS = {
    "sounddevice", "webrtcvad", "faster_whisper", "google",
    "numpy", "fastapi", "uvicorn",
}


def is_external_missing(exc):
    name = getattr(exc, "name", None) or ""
    return name.split(".")[0] in EXTERNAL_DEPS


modules_to_test = ["config", "audio_capture", "stt", "translator"]

skipped, failed = [], []
for mod_name in modules_to_test:
    try:
        importlib.import_module(mod_name)
        print(f"[OK]   import {mod_name}", flush=True)
    except ImportError as e:
        if is_external_missing(e):
            print(f"[SKIP] import {mod_name}: missing package {e.name}", flush=True)
            skipped.append(mod_name)
        else:
            print(f"[FAIL] import {mod_name}: {e}", flush=True)
            failed.append(mod_name)
    except Exception as e:
        print(f"[FAIL] import {mod_name}: {type(e).__name__}: {e}", flush=True)
        failed.append(mod_name)

print()
if failed:
    print(f"failed: {len(failed)}, skipped: {len(skipped)}")
    sys.exit(1)
print(f"passed: {len(modules_to_test) - len(skipped)}, skipped: {len(skipped)}")
