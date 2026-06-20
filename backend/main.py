"""
主程式：FastAPI + WebSocket
整合 音訊擷取 → STT → 翻譯 → 即時推送到前端字幕頁面
"""
import asyncio
import json
import logging
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path

# 壓掉 websockets 函式庫在前端瀏覽器硬斷線時印出的底層雜訊
# （"data transfer failed" 那串 traceback），不影響功能與自己的 print
logging.getLogger("websockets").setLevel(logging.CRITICAL)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import config
from audio_capture import AudioCapturer
from stt import SpeechToText
from translator import Translator


# ===== Queue 與 broadcast 機制 =====
audio_queue: queue.Queue = queue.Queue(maxsize=config.AUDIO_QUEUE_MAX)
text_queue: queue.Queue = queue.Queue(maxsize=config.TEXT_QUEUE_MAX)

# 一個 thread-safe 廣播管道：背景執行緒推結果，FastAPI 主迴圈 pop 後廣播
result_queue: "asyncio.Queue[dict]" = None  # 在 lifespan 裡建立
event_loop: asyncio.AbstractEventLoop = None

# 連線中的 WebSocket 清單
active_websockets: set[WebSocket] = set()
ws_lock = asyncio.Lock()


def on_translation_result(result: dict):
    """翻譯 worker 完成後呼叫；把結果丟進 asyncio queue（thread-safe）"""
    if event_loop and result_queue:
        # 從 thread 安全地把 item 推到 asyncio queue
        asyncio.run_coroutine_threadsafe(
            result_queue.put(result),
            event_loop,
        )


async def broadcaster():
    """背景 task：把 result_queue 裡的翻譯結果廣播給所有連線中的 WebSocket"""
    while True:
        result = await result_queue.get()
        message = json.dumps(result, ensure_ascii=False)
        async with ws_lock:
            dead = []
            for ws in active_websockets:
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                active_websockets.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global result_queue, event_loop
    config.validate()

    event_loop = asyncio.get_running_loop()
    result_queue = asyncio.Queue()

    # 啟動廣播 task
    broadcast_task = asyncio.create_task(broadcaster())

    # 啟動三個 pipeline 元件
    capturer = AudioCapturer(audio_queue)
    stt = SpeechToText(
        audio_queue, text_queue,
        model_size=config.WHISPER_MODEL,
        device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE_TYPE,
        cpu_threads=config.WHISPER_CPU_THREADS,
        num_workers=config.WHISPER_NUM_WORKERS,
    )
    translator = Translator(
        text_queue,
        on_result=on_translation_result,
        api_key=config.GEMINI_API_KEY,
        model=config.GEMINI_MODEL,
        num_workers=config.TRANSLATOR_WORKERS,
    )

    capturer.start()
    stt.start()
    translator.start()

    print(f"[系統] 服務啟動，前往 http://localhost:{config.PORT} 觀看字幕")

    try:
        yield
    finally:
        capturer.stop()
        stt.stop()
        translator.stop()
        broadcast_task.cancel()


app = FastAPI(lifespan=lifespan)


# ===== 靜態檔案 (前端) =====
_frontend_dir = Path(__file__).parent.parent / "frontend"

# 禁止瀏覽器快取，這樣每次改前端都能立刻看到（演唱會場景需要可即時調整）
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(_frontend_dir / "index.html", headers=_NO_CACHE_HEADERS)


@app.get("/style.css")
async def style():
    return FileResponse(_frontend_dir / "style.css", headers=_NO_CACHE_HEADERS)


@app.get("/app.js")
async def js():
    return FileResponse(_frontend_dir / "app.js", headers=_NO_CACHE_HEADERS)


# ===== WebSocket =====
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    async with ws_lock:
        active_websockets.add(websocket)
    print(f"[WS] 前端連線（共 {len(active_websockets)} 條）")
    try:
        while True:
            # 等待前端訊息（目前用不到，但要 keep alive）
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with ws_lock:
            active_websockets.discard(websocket)
        print(f"[WS] 前端斷線（剩 {len(active_websockets)} 條）")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        log_level="warning",
        reload=False,
    )
