"""集中管理所有設定值"""
import os
from dotenv import load_dotenv

_current_dir = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_current_dir, ".env")
load_dotenv(dotenv_path=_env_path)


# ===== Gemini =====
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

# ===== Whisper =====
# CPU 場景模型推薦：
#   - medium：CPU 上實際最平衡（精度好、RTF ~1.5x）
#   - small：速度快但中文短句準度差，會胡言亂語
#   - large-v3-turbo / large-v3：CPU 上太慢（encoder 跟 large 一樣大，
#                                只有 GPU 才會顯著快），不建議純 CPU 使用
# GPU 場景：直接 large-v3-turbo + WHISPER_DEVICE=cuda 最佳
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

# CPU 多執行緒：0 = 用所有核心；faster-whisper 預設只用 4 核
# 在 8~16 核機器上能加速 2~4x，免費效能升級
WHISPER_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "0"))
# 同時處理的音訊段並行度（CPU 設 1 即可，多了反而互搶資源）
WHISPER_NUM_WORKERS = int(os.getenv("WHISPER_NUM_WORKERS", "1"))

# 語言模式：
#   auto（預設）→ 每段自動偵測語言並路由：
#       zh             → 送 Gemini 翻 4 國
#       th/vi/id/tl    → 直接顯示到對應格（翻譯員直顯，跳過 Gemini）
#       其他 / 信心不足 → 丟棄
#   zh / vi / 其他    → 強制鎖定該語言，一律送 Gemini（單一語言主持人場次用）
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "auto")

# auto 模式下，偵測語言的信心低於此值就丟棄（避免短句誤判亂路由）
LANG_CONFIDENCE_MIN = float(os.getenv("LANG_CONFIDENCE_MIN", "0.7"))

# auto 模式的語言偵測模型：用一顆「輕量模型」專門判語言，再用主模型（medium）
# 鎖定該語言解碼一次。如此主模型的 encoder 只跑一次，而非「偵測 + 解碼」跑兩次，
# 在沒有 Tensor Core 的弱 GPU（如 GTX 1650）上幾乎砍半 STT 延遲。
#   tiny（預設）：偵測極快，5 國語言差異大、判得準
#   base：偵測略準一點、略慢，VRAM 不足或想省可留 tiny
#   設成空字串 "" → 關閉，退回主模型自己偵測（較慢）
# 只有在 WHISPER_LANGUAGE=auto 時才會載入這顆模型。
LANG_DETECT_MODEL = os.getenv("LANG_DETECT_MODEL", "tiny")

# 注意：不使用 initial_prompt。Whisper 已知 bug 會把 prompt 尾巴當識別結果輸出，
# 而強制 language 已經足夠引導模型用對的語言識別。

# ===== 音訊參數 =====
SAMPLE_RATE = 16000  # Whisper 要求 16kHz
CHANNELS = 1
FRAME_DURATION_MS = 30  # WebRTC VAD 接受 10/20/30 ms
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)
VAD_AGGRESSIVENESS = 2  # 0(最寬鬆)~3(最嚴格)

# 一段語音的長度限制
# MIN 拉高到 1000ms，避免短雜音/單音節被誤判成其他語言
MIN_SPEECH_DURATION_MS = 1000    # 短於這個就丟棄
MAX_SPEECH_DURATION_MS = 8000    # 超過就強制切斷送出
SILENCE_TIMEOUT_MS = 700         # 靜音多少 ms 後判定一句話結束

# Queue 流量控制：滿了就丟最舊的，永遠處理最新音訊（演唱會場景不能延遲累積）
AUDIO_QUEUE_MAX = 5
TEXT_QUEUE_MAX = 10

# Gemini 翻譯並行 worker 數
# 免費版 RPM 限制：flash 約 10/min、flash-lite 約 15/min
# 2 個 worker 比較不會撞額度；若用付費版可拉高到 4~6
TRANSLATOR_WORKERS = int(os.getenv("TRANSLATOR_WORKERS", "2"))

# ===== 伺服器 =====
PORT = int(os.getenv("PORT", "8000"))
HOST = "0.0.0.0"


def validate():
    """啟動前檢查必要設定"""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "找不到 GEMINI_API_KEY。請在 backend/.env 設定 API key。"
            "（範例見 .env.example）"
        )
