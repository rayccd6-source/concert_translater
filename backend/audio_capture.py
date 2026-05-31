"""
音訊擷取 + WebRTC VAD（語音活動檢測）模組

設計重點：
- 用 sounddevice 連續從麥克風讀 30ms frame
- 用 WebRTC VAD 判斷每個 frame 是「語音」或「靜音」
- 累積語音 frame；遇到一段持續靜音就把這段送出當成一句話
- 避免 fixed pause threshold（原專案的 1.2 秒）造成主持人講話過程被截斷或卡住
"""
import queue
import threading
import collections
import time
import numpy as np
import sounddevice as sd
import webrtcvad

import config


class AudioCapturer:
    """
    連續錄音 + VAD 切句。

    使用：
        cap = AudioCapturer(output_queue)
        cap.start()
        # output_queue 會持續吐出 np.ndarray (float32, mono, 16kHz) 語音段
    """

    def __init__(self, output_queue: queue.Queue):
        self.output_queue = output_queue
        self.vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        frames_per_silence_timeout = int(
            config.SILENCE_TIMEOUT_MS / config.FRAME_DURATION_MS
        )
        max_frames = int(
            config.MAX_SPEECH_DURATION_MS / config.FRAME_DURATION_MS
        )
        min_frames = int(
            config.MIN_SPEECH_DURATION_MS / config.FRAME_DURATION_MS
        )

        # 用 deque 紀錄最近的靜音/語音狀態（滑動窗口）
        ring_buffer = collections.deque(maxlen=frames_per_silence_timeout)

        triggered = False           # 是否正在錄一段語音
        voiced_frames = []          # 累積的語音 frame
        silent_count = 0            # 連續靜音 frame 數

        def audio_callback(indata, frames, time_info, status):
            """sounddevice 的 callback：把 raw bytes 推到內部 queue"""
            if status:
                # buffer underflow 等狀況，不中斷
                pass
            # indata 是 int16 mono，flatten 後 tobytes 給 VAD
            raw_queue.put(bytes(indata))

        raw_queue: queue.Queue = queue.Queue()

        # 開啟麥克風串流（int16 PCM，VAD 需要）
        stream = sd.RawInputStream(
            samplerate=config.SAMPLE_RATE,
            blocksize=config.FRAME_SAMPLES,
            dtype="int16",
            channels=config.CHANNELS,
            callback=audio_callback,
        )

        with stream:
            print(f"[音訊] 麥克風啟動，採樣率 {config.SAMPLE_RATE} Hz")
            while not self._stop_event.is_set():
                try:
                    frame_bytes = raw_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # frame 必須剛好是 FRAME_SAMPLES 樣本
                if len(frame_bytes) != config.FRAME_SAMPLES * 2:
                    continue

                is_speech = self.vad.is_speech(frame_bytes, config.SAMPLE_RATE)

                if not triggered:
                    # 還沒開始錄一段語音，看 ring buffer 有沒有足夠語音 frame 觸發
                    ring_buffer.append((frame_bytes, is_speech))
                    num_voiced = sum(1 for _, sp in ring_buffer if sp)
                    if num_voiced > 0.5 * ring_buffer.maxlen:
                        triggered = True
                        # 把 ring buffer 裡的全部都當作語音開頭
                        voiced_frames = [f for f, _ in ring_buffer]
                        ring_buffer.clear()
                        silent_count = 0
                else:
                    voiced_frames.append(frame_bytes)
                    if is_speech:
                        silent_count = 0
                    else:
                        silent_count += 1

                    # 條件 1：連續靜音超過閾值 → 一句話結束
                    # 條件 2：累積 frame 超過上限 → 強制切
                    if silent_count >= frames_per_silence_timeout or \
                            len(voiced_frames) >= max_frames:
                        if len(voiced_frames) >= min_frames:
                            self._emit_segment(voiced_frames)
                        triggered = False
                        voiced_frames = []
                        silent_count = 0
                        ring_buffer.clear()

    def _emit_segment(self, voiced_frames):
        """
        把語音 frame 合成 numpy 陣列推到 output queue。

        關鍵策略：drop-oldest。若 queue 滿（代表 STT 跟不上），
        直接丟最舊那段、塞入最新；演唱會場景永遠優先處理最新音訊，
        避免延遲累積到幾十秒。
        """
        pcm = b"".join(voiced_frames)
        # int16 → float32（Whisper 要 float32 [-1, 1]）
        audio_np = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        duration = len(audio_np) / config.SAMPLE_RATE
        timestamp = time.time()
        item = {
            "audio": audio_np,
            "duration": duration,
            "timestamp": timestamp,
        }

        try:
            self.output_queue.put_nowait(item)
            print(f"[音訊] 切出一段語音：{duration:.2f}s")
        except queue.Full:
            try:
                dropped = self.output_queue.get_nowait()
                self.output_queue.put_nowait(item)
                print(f"[音訊] 切出一段語音：{duration:.2f}s "
                      f"(STT 跟不上，丟掉舊段 {dropped['duration']:.2f}s)")
            except queue.Empty:
                pass
