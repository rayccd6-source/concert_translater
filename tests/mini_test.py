import sys
import os
import importlib

print("step 1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("GEMINI_API_KEY", "dummy")
print("step 2, sys.path[0]=", sys.path[0])

modules_to_test = ["config", "audio_capture", "stt", "translator"]
print("step 3")

for m in modules_to_test:
    try:
        importlib.import_module(m)
        print(f"OK {m}", flush=True)
    except ImportError as e:
        print(f"IMP {m}: {e.name}", flush=True)
    except Exception as e:
        print(f"ERR {m}: {type(e).__name__}: {e}", flush=True)
print("done", flush=True)
