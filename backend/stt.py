"""
Faster-Whisper 語音識別模組

設計重點：
- 首次載入會下載模型（~250 MB for small），之後快取在 ~/.cache/huggingface
- 自動偵測中文或越南文（不需手動切換）
- 把識別出來的文字推到 text_queue 給翻譯端
- 過濾掉明顯是雜訊/空字串/重複幻覺的結果
"""
import os
import queue
import threading
import time
import re
from typing import Optional
from faster_whisper import WhisperModel
import config

# faster-whisper 在啟動時很慢，所以 lazy import
WhisperModel = None
download_model = None


def _lazy_import():
    global WhisperModel, download_model
    if WhisperModel is None:
        from faster_whisper import WhisperModel as _W
        from faster_whisper import download_model as _dl
        WhisperModel = _W
        download_model = _dl


# 常見幻覺片語黑名單（Whisper 在無聲段容易產生這些）
# 額外加入過去 initial_prompt 的片段，預防舊版 prompt 內容被當輸出
HALLUCINATION_BLACKLIST = {
    "謝謝觀看", "謝謝大家", "感謝您的觀看", "請訂閱", "請按讚",
    "thanks for watching", "thank you for watching",
    "字幕由", "subtitle", "字幕製作",
    "請用中文轉寫", "用中文轉寫", "繁體中文",
    "演唱會主持人發言",
    "transcribe",
}


def _is_hallucination(text: str) -> bool:
    text_lower = text.lower().strip()
    if len(text_lower) < 2:
        return True
    for bl in HALLUCINATION_BLACKLIST:
        if bl in text_lower:
            return True
    # 重複字元（例：哈哈哈哈哈哈 整段一樣）— 門檻放寬到 >= 5
    if len(set(text_lower)) <= 2 and len(text_lower) >= 5:
        return True
    return False


class SpeechToText:
    """
    持續從 audio_queue 取語音段，呼叫 Whisper 識別後，推進 text_queue。
    """

    def __init__(
        self,
        audio_queue: queue.Queue,
        text_queue: queue.Queue,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 0,
        num_workers: int = 1,
    ):
        self.audio_queue = audio_queue
        self.text_queue = text_queue
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.num_workers = num_workers
        self.model: Optional[WhisperModel] = None
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _load_model(self):
        _lazy_import()
        print(f"[STT] 載入 Whisper {self.model_size} 模型中（首次需下載）...")

        # 若 model_size 本身就是一個資料夾路徑，直接用
        if os.path.isdir(self.model_size):
            model_source = self.model_size
        else:
            # 把模型下載到專案內的 models/ 資料夾。
            # 用 output_dir 會讓 faster-whisper 以「複製檔案」方式存放，
            # 而不是建立 symlink，藉此避開 Windows 一般帳號沒有 symlink
            # 權限造成的 WinError 1314。
            target_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "models",
                f"faster-whisper-{self.model_size}",
            )
            os.makedirs(target_dir, exist_ok=True)
            model_source = download_model(self.model_size, output_dir=target_dir)

        self.model = WhisperModel(
            model_source,
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=self.cpu_threads,
            num_workers=self.num_workers,
        )
        import os as _os
        actual_threads = self.cpu_threads or _os.cpu_count() or 4
        print(f"[STT] 模型載入完成 (cpu_threads={actual_threads}, num_workers={self.num_workers})")

    def _run(self):
        try:
            self._load_model()
        except Exception as e:
            err = str(e)
            print(f"[STT] 模型載入失敗：{err}")
            if "Invalid model size" in err or "expected one of" in err:
                print("[STT] 提示：faster-whisper 版本太舊。"
                      "請執行：pip install -U faster-whisper")
            elif "1314" in err or "symlink" in err.lower():
                print("[STT] 提示：Windows symlink 權限錯誤。"
                      "請改用系統管理員身分執行，或開啟 Windows 開發人員模式。")
            return

        while not self._stop_event.is_set():
            try:
                segment = self.audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            audio_np = segment["audio"]
            timestamp = segment["timestamp"]
            duration = segment["duration"]

            try:
                # 根據設定強制指定語言；演唱會場景強烈建議鎖定避免短句誤判
                lang_setting = config.WHISPER_LANGUAGE.lower()
                if lang_setting == "auto":
                    language = None
                elif lang_setting == "vi":
                    language = "vi"
                else:
                    language = "zh"

                t0 = time.time()
                # 注意：刻意不用 initial_prompt。Whisper 已知 bug：
                # 當語音模糊或太短時，會把 initial_prompt 的尾巴當輸出回傳
                # （例如「請用中文轉寫」會一直被當識別結果）。
                # language 強制鎖定就已足夠引導模型，prompt 邊際效益低、風險高。
                segments, info = self.model.transcribe(
                    audio_np,
                    language=language,
                    beam_size=1,                       # 速度優先
                    vad_filter=False,                  # VAD 已在前面做
                    condition_on_previous_text=False,  # 避免幻覺累積
                    without_timestamps=True,           # 不要 timestamp 加速
                    no_speech_threshold=0.8,           # 拉高，更嚴格判定靜音
                    log_prob_threshold=-1.0,           # 低信心輸出直接丟
                    compression_ratio_threshold=2.4,   # 阻止重複型幻覺
                )

                text = " ".join(seg.text for seg in segments).strip()
                detected_lang = info.language
                elapsed = time.time() - t0

                if not text or _is_hallucination(text):
                    print(f"[STT] 過濾掉雜訊或幻覺：{text!r} (處理 {elapsed:.2f}s)")
                    self.audio_queue.task_done() if hasattr(self.audio_queue, "task_done") else None
                    continue

                print(f"[STT] 識別 ({detected_lang}, 語音 {duration:.2f}s / 處理 {elapsed:.2f}s)：{text}")

                # 用 put_nowait + drop-oldest，避免 STT 比翻譯快時堆積
                payload = {
                    "text": text,
                    "lang": detected_lang,
                    "timestamp": timestamp,
                }
                try:
                    self.text_queue.put_nowait(payload)
                except queue.Full:
                    try:
                        self.text_queue.get_nowait()
                        self.text_queue.put_nowait(payload)
                    except queue.Empty:
                        pass

            except Exception as e:
                import traceback
                print(f"[STT] 識別錯誤：{type(e).__name__}: {e}")
                traceback.print_exc()
