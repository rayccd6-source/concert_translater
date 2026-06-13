"""
Faster-Whisper 語音識別模組

設計重點：
- 首次載入會下載模型（~250 MB for small），之後快取在 ~/.cache/huggingface
- auto 模式自動偵測語言並路由（中文送翻譯，泰/越/印/菲 直顯，其他丟棄）
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


def _setup_cuda_dll_paths():
    """
    Windows + Python 3.8+：pip 裝的 nvidia 套件把 DLL 放在
    site-packages/nvidia/<lib>/bin/，但 Python / Windows 預設不會搜這裡。

    關鍵：CTranslate2 用標準 LoadLibrary，需要在 PATH 環境變數裡看得到 DLL，
    單純 os.add_dll_directory() 不夠（它只對有用 SEARCH_USER_DIRS flag 的
    LoadLibraryEx 呼叫生效，CT2 4.7.x 並未使用該 flag）。兩個方法都加上去。
    """
    if os.name != "nt":
        return
    try:
        import importlib.util
        added = []
        new_paths = []
        for pkg in (
            "nvidia.cublas", "nvidia.cudnn",
            "nvidia.cuda_runtime", "nvidia.cuda_nvrtc",
            "nvidia.cufft", "nvidia.curand", "nvidia.cusolver",
            "nvidia.cusparse",
            "nvidia.nvjitlink",
        ):
            spec = importlib.util.find_spec(pkg)
            if spec is None:
                continue
            search_paths = []
            if spec.origin:
                search_paths.append(os.path.dirname(spec.origin))
            if spec.submodule_search_locations:
                search_paths.extend(spec.submodule_search_locations)
            for pkg_dir in search_paths:
                bin_dir = os.path.join(pkg_dir, "bin")
                if os.path.isdir(bin_dir):
                    new_paths.append(bin_dir)
                    if pkg not in added:
                        added.append(pkg)
        if new_paths:
            # 1. 加到 PATH（給 CT2 這種用標準 LoadLibrary 的 C 擴展）
            os.environ["PATH"] = os.pathsep.join(new_paths) + os.pathsep + os.environ.get("PATH", "")
            # 2. 同時也 add_dll_directory（給有用 SEARCH_USER_DIRS flag 的呼叫）
            for p in new_paths:
                try:
                    os.add_dll_directory(p)
                except Exception:
                    pass
        if added:
            print(f"[STT] 已加入 CUDA DLL 路徑：{', '.join(added)}")
        else:
            print("[STT] 警告：找不到 nvidia 套件的 bin 資料夾，GPU 可能無法用")
    except Exception as e:
        print(f"[STT] CUDA DLL 路徑設定異常：{e}")


def _lazy_import():
    global WhisperModel, download_model
    if WhisperModel is None:
        _setup_cuda_dll_paths()  # 必須在 import faster_whisper 之前
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


# auto 模式下，Whisper 偵測到這些語言碼 → 直接顯示到對應前端格子（翻譯員直顯），
# 不送 Gemini。key 是 Whisper 語言碼，value 是翻譯/前端的欄位名。
DIRECT_LANG_MAP = {
    "th": "thai",
    "vi": "vietnamese",
    "id": "indonesian",
    "tl": "filipino",
}

# auto 模式只在這 5 國語言內判定（主持人中文 + 4 國翻譯員語言），
# 避免中文短句被 Whisper 誤判成日/韓等「不可能出現」的語言而用錯模型解碼。
ALLOWED_LANGS = ("zh",) + tuple(DIRECT_LANG_MAP)  # ("zh","th","vi","id","tl")


def _pick_allowed_from_probs(all_probs, fallback_lang="zh", fallback_prob=0.0):
    """
    從 [(語言碼, 機率), ...] 清單裡，限定在 ALLOWED_LANGS（5 國）內挑機率最高者。
    回傳 (語言碼, 機率)。清單為空時回傳 fallback；都不在 5 國內時退回清單第一名。
    """
    if all_probs:
        allowed = [(lang, p) for lang, p in all_probs if lang in ALLOWED_LANGS]
        if allowed:
            return max(allowed, key=lambda x: x[1])
        return all_probs[0]
    return fallback_lang, fallback_prob


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
        self.detector: Optional[WhisperModel] = None  # auto 模式專用的輕量語言偵測模型
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _resolve_model_source(self, name: str) -> str:
        """
        name 可以是現成資料夾路徑，或模型代號（如 medium / tiny）。
        模型代號會下載到專案內 models/ 資料夾，並用 output_dir 走「複製檔案」模式，
        避開 Windows 一般帳號沒有 symlink 權限造成的 WinError 1314。
        """
        if os.path.isdir(name):
            return name
        target_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "models",
            f"faster-whisper-{name}",
        )
        os.makedirs(target_dir, exist_ok=True)
        return download_model(name, output_dir=target_dir)

    def _load_model(self):
        _lazy_import()
        print(f"[STT] 載入 Whisper {self.model_size} 模型中（首次需下載）...")

        self.model = WhisperModel(
            self._resolve_model_source(self.model_size),
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=self.cpu_threads,
            num_workers=self.num_workers,
        )
        import os as _os
        actual_threads = self.cpu_threads or _os.cpu_count() or 4
        print(f"[STT] 模型載入完成 (cpu_threads={actual_threads}, num_workers={self.num_workers})")

        # auto 模式：載入輕量偵測模型，專門判語言，讓主模型 encoder 只需跑一次。
        detect_name = config.LANG_DETECT_MODEL.strip()
        if config.WHISPER_LANGUAGE.lower() == "auto" and detect_name:
            if detect_name == self.model_size:
                # 偵測模型跟主模型同一顆 → 共用即可，不另外吃 VRAM（但等於沒加速）
                self.detector = self.model
                print(f"[STT] 語言偵測共用主模型（{detect_name}）")
            else:
                print(f"[STT] 載入語言偵測模型 {detect_name} 中（首次需下載）...")
                self.detector = WhisperModel(
                    self._resolve_model_source(detect_name),
                    device=self.device,
                    compute_type=self.compute_type,
                )
                print(f"[STT] 語言偵測模型載入完成（{detect_name}）")

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
                # auto = 自動偵測 + 路由；其他值 = 強制鎖定該語言（一律送 Gemini）
                lang_setting = config.WHISPER_LANGUAGE.lower()
                forced = lang_setting != "auto"

                t0 = time.time()
                # 注意：刻意不用 initial_prompt。Whisper 已知 bug：語音模糊或太短時，
                # 會把 initial_prompt 尾巴當輸出回傳。language 鎖定已足夠引導模型。
                transcribe_kwargs = dict(
                    beam_size=1,                       # 速度優先
                    vad_filter=False,                  # VAD 已在前面做
                    condition_on_previous_text=False,  # 避免幻覺累積
                    without_timestamps=True,           # 不要 timestamp 加速
                    no_speech_threshold=0.8,           # 拉高，更嚴格判定靜音
                    log_prob_threshold=-1.0,           # 低信心輸出直接丟
                    compression_ratio_threshold=2.4,   # 阻止重複型幻覺
                )

                if forced:
                    # 強制鎖定語言場次：直接用該語言解碼
                    segments, info = self.model.transcribe(
                        audio_np, language=lang_setting, **transcribe_kwargs
                    )
                    detected_lang = info.language
                    lang_prob = getattr(info, "language_probability", 1.0) or 1.0
                elif self.detector is not None:
                    # auto：用輕量模型（tiny）先判語言（只在 5 國內挑），再用主模型
                    # 鎖定該語言解碼一次。主模型 encoder 只跑一次，而非偵測+解碼跑兩次。
                    result = self.detector.detect_language(audio_np)
                    # 兼容不同版本回傳：(lang, prob, all_probs) 或 (lang, prob)
                    all_probs = result[2] if len(result) > 2 else [(result[0], result[1])]
                    detected_lang, lang_prob = _pick_allowed_from_probs(all_probs)
                    segments, info = self.model.transcribe(
                        audio_np, language=detected_lang, **transcribe_kwargs
                    )
                else:
                    # auto 但沒有偵測模型（LANG_DETECT_MODEL=""）→ 退回主模型自偵測（較慢）。
                    segments, info = self.model.transcribe(
                        audio_np, language=None, **transcribe_kwargs
                    )
                    all_probs = (getattr(info, "all_language_probs", None)
                                 or [(info.language,
                                      getattr(info, "language_probability", 1.0) or 1.0)])
                    detected_lang, lang_prob = _pick_allowed_from_probs(all_probs)
                    if detected_lang != info.language:
                        segments, info = self.model.transcribe(
                            audio_np, language=detected_lang, **transcribe_kwargs
                        )

                text = " ".join(seg.text for seg in segments).strip()
                elapsed = time.time() - t0

                if not text or _is_hallucination(text):
                    print(f"[STT] 過濾掉雜訊或幻覺：{text!r} (處理 {elapsed:.2f}s)")
                    self.audio_queue.task_done() if hasattr(self.audio_queue, "task_done") else None
                    continue

                # ===== 語言路由 =====
                # 主持人只講中文：只有「明確且高信心的翻譯員語言」走直顯，
                # 其餘（中文、低信心、誤判）一律送 Gemini。Gemini 的 prompt 不假設
                # 來源語言，所以這個「安全底桶」不會因語言不符而翻錯。
                if (not forced
                        and detected_lang in DIRECT_LANG_MAP
                        and lang_prob >= config.LANG_CONFIDENCE_MIN):
                    route = "direct"
                    target_lang = DIRECT_LANG_MAP[detected_lang]
                else:
                    route = "translate"
                    target_lang = None

                route_desc = "翻譯" if route == "translate" else f"直顯→{target_lang}"
                print(f"[STT] 識別 ({detected_lang} p={lang_prob:.2f}, 語音 {duration:.2f}s / "
                      f"處理 {elapsed:.2f}s, {route_desc})：{text}")

                # 用 put_nowait + drop-oldest，避免 STT 比翻譯快時堆積
                payload = {
                    "text": text,
                    "lang": detected_lang,
                    "timestamp": timestamp,
                    "route": route,
                    "target_lang": target_lang,
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
