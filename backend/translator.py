"""
Gemini 文字翻譯模組

設計重點：
- 只送文字（不送音檔），token 用量降到原本的 1/100 以下
- 一次呼叫同時翻成 4 種語言（泰、越、印、菲）
- 用 response_mime_type=application/json 強制 JSON 輸出，省 prompt token
- 多個並行 worker，避免單次 API 延遲堵塞整條 pipeline
- 429 (rate limit) 與其他錯誤的優雅退避
"""
import queue
import threading
import json
import time
import re
from typing import Callable, Optional

# google.genai 是 runtime 才需要；測試純解析邏輯時可以不裝
try:
    from google import genai
    from google.genai import types
    _HAS_GENAI = True
except ImportError:
    _HAS_GENAI = False

import config


REQUIRED_LANGS = ["thai", "vietnamese", "indonesian", "filipino"]


def parse_translation_response(raw: str, fallback_original: str = "") -> Optional[dict]:
    """
    從 Gemini 回傳的字串中解析出翻譯 JSON。
    支援：
      - 乾淨 JSON
      - 含 markdown code fence 的 JSON
      - 缺欄位（補空字串）
    無法解析則回 None。
    """
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    for k in REQUIRED_LANGS:
        if k not in data:
            data[k] = ""
    if "original" not in data and fallback_original:
        data["original"] = fallback_original
    return data


def is_suspicious_translation(data: dict) -> bool:
    """
    若 4 個翻譯欄位過度雷同（例如三個以上完全一樣），
    視為 Gemini 異常輸出（例如把所有欄位都填同一語言）。
    """
    vals = [(data.get(k) or "").strip() for k in REQUIRED_LANGS]
    non_empty = [v for v in vals if v]
    if len(non_empty) < 2:
        return False  # 只填了一個就不算可疑
    # 三個以上完全一樣 → 可疑
    from collections import Counter
    most_common = Counter(non_empty).most_common(1)[0]
    return most_common[1] >= 3


SYSTEM_INSTRUCTION = """你是演唱會即時同步翻譯員。
任務：將使用者送來的文字翻譯成下列四種語言（不論原文是哪一種語言）：
- thai：泰文 (Thai)
- vietnamese：越南文 (Tiếng Việt)
- indonesian：印尼文 (Bahasa Indonesia)
- filipino：菲律賓文 (Tagalog/Filipino)

規則：
1. 直接輸出 JSON，不要 markdown code fence。
2. 翻譯要口語、流暢、適合演唱會主持人語境，不要逐字直譯。
3. 保留人名、地名、品牌名的原文（或常見譯名）。
4. 若文字過短或無意義（如「嗯」「啊」），把該字當作填充詞處理，原樣回傳即可。
5. 不要添加任何解釋、注釋或額外標點。

輸出格式：
{"original": "原文", "thai": "...", "vietnamese": "...", "indonesian": "...", "filipino": "..."}
"""


class Translator:
    """
    多 worker 並行翻譯。
    從 text_queue 取文字 → 呼叫 Gemini → 把翻譯結果交給 on_result callback。
    """

    def __init__(
        self,
        text_queue: queue.Queue,
        on_result: Callable[[dict], None],
        api_key: str,
        model: str = "gemini-2.5-flash",
        num_workers: int = 3,
    ):
        self.text_queue = text_queue
        self.on_result = on_result
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.num_workers = num_workers
        self._stop_event = threading.Event()
        self._threads = []

    def start(self):
        for i in range(self.num_workers):
            t = threading.Thread(
                target=self._worker, args=(i + 1,), daemon=True
            )
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop_event.set()

    def _worker(self, worker_id: int):
        print(f"[翻譯] worker {worker_id} 啟動")
        while not self._stop_event.is_set():
            try:
                item = self.text_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            text = item["text"]
            timestamp = item["timestamp"]
            route = item.get("route", "translate")

            # 翻譯員直顯：不送 Gemini，只組對應語言的 payload 直接推前端
            if route == "direct":
                target = item.get("target_lang")
                if target:
                    payload = {k: "" for k in REQUIRED_LANGS}
                    payload[target] = text
                    payload["original"] = text
                    payload["timestamp"] = timestamp
                    payload["worker_id"] = worker_id
                    payload["source"] = "interpreter"
                    print(f"[翻譯] worker {worker_id} 翻譯員直顯（{target}）：{text}")
                    self.on_result(payload)
                continue

            try:
                t0 = time.time()
                result = self._translate(text)
                elapsed = time.time() - t0
                if result is None:
                    continue

                # 4 國語言過度雷同 → 視為 Gemini 異常，丟棄
                if is_suspicious_translation(result):
                    print(f"[翻譯] worker {worker_id} 結果可疑（4 國雷同），丟棄：{result}")
                    continue

                result["timestamp"] = timestamp
                result["worker_id"] = worker_id
                result["source"] = "gemini"
                # 把原文保險地塞回去（萬一 Gemini 沒回 original）
                result.setdefault("original", text)

                print(f"[翻譯] worker {worker_id} 完成 ({elapsed:.2f}s)")
                self.on_result(result)

            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    # 從錯誤訊息抓出 retry-after，若沒有就用 8 秒
                    retry_after = 8
                    m = re.search(r"retry.*?(\d+)", err.lower())
                    if m:
                        retry_after = max(int(m.group(1)), 5)
                    print(f"[翻譯] worker {worker_id} API 額度耗盡，退避 {retry_after} 秒。"
                          f"建議改用 gemini-2.5-flash-lite 或減少 TRANSLATOR_WORKERS。")
                    time.sleep(retry_after)
                else:
                    print(f"[翻譯] worker {worker_id} 錯誤：{type(e).__name__}: {err}")

    def _translate(self, text: str) -> Optional[dict]:
        response = self.client.models.generate_content(
            model=self.model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.3,
                # 2.5 系列會先「思考」並吃掉 output token，1024 容易在思考後把 JSON 截斷
                # 而解析失敗。舊版 google-genai(0.3.0) 沒有 ThinkingConfig 可關思考，
                # 改用拉高上限的方式讓「思考 + JSON」都塞得下。升級 SDK 後可改用
                # thinking_config=types.ThinkingConfig(thinking_budget=0) 更省更快。
                max_output_tokens=4096,
            ),
        )

        raw = response.text or ""
        data = parse_translation_response(raw, fallback_original=text)
        if data is None:
            # raw 為空通常代表被安全過濾擋掉或輸出中斷；附上 finish_reason 方便判斷
            reason = ""
            try:
                if response.candidates:
                    reason = f"（finish_reason={response.candidates[0].finish_reason}）"
            except Exception:
                pass
            shown = raw[:200] if raw else "<空>"
            print(f"[翻譯] 無法解析回應{reason}：{shown}")
        return data
