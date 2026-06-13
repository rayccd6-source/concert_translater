# Concert Translator — 交接書

> 給接手的 Claude Code 看的專案快照。讀完不需要再從頭問使用者。

## 一、專案在做什麼

幫演唱會主持人講話做即時翻譯字幕,顯示在大螢幕上。
使用者:Ray,大學生,主修財金 + 資工,quant 團隊實習業務。

## 二、產品定位

演唱會主持人說話 → 即時翻譯成 4 種語言同步顯示在大螢幕

- **輸入語言:** 中、泰、越、印、菲 都可能(見下方「演唱會實際流程」)
- **輸出語言(畫面 4 格):** 泰文 / 越南文 / 印尼文 / 菲律賓文
- **場景:** 主持人持續講話,系統必須不間斷處理
- **字幕:** 4 等分網格,新句子從底部進入舊的往上推,60 秒後淡出消失

### 演唱會實際流程

不是每段都會有翻譯員,情境會在這兩種模式之間切換:

**模式 A — 只有主持人:**
- 主持人講中文 → 系統收音、Whisper 識別、Gemini 譯成 4 國語言 → 4 格更新

**模式 B — 主持人 + 該國翻譯員(某組表演團體上台時帶上來):**
1. 主持人講一句中文 → 同上,Gemini 譯 4 國 → 4 格更新
2. 翻譯員緊接著用該國語言(假設泰文)複述同樣內容 → Whisper 識別出泰文 → **直接顯示在 Thai 格,跳過 Gemini**
3. 重複

**關鍵設計原則:** 系統不需要事先知道現在是哪種模式,完全靠 Whisper 偵測 detected_lang 路由。沒翻譯員的時段就是純走 Gemini 翻譯;有翻譯員的時段那一格自動會被翻譯員的直顯覆蓋。模式切換是無縫的。

同一時間最多一位翻譯員(不會 4 國語言同時收音)。

## 三、架構

```
麥克風
  ↓ (sounddevice 16kHz PCM)
WebRTC VAD 切句
  ↓ (audio_queue, drop-oldest)
Faster-Whisper 本地 STT (language=auto + confidence 檢查)
  ↓ (text_queue, drop-oldest)
依 detected_lang 路由:
  zh    → Gemini 翻 4 國 → 4 格更新
  th/vi/id/tl → 直接顯示到對應格,不送 Gemini,不動其他 3 格
  其他   → 丟棄
  ↓ (asyncio queue)
FastAPI WebSocket 廣播
  ↓
瀏覽器字幕頁面(滾動式)
```

## 四、技術棧與檔案

- Python 3.12
- 後端:`backend/config.py`, `audio_capture.py`, `stt.py`, `translator.py`, `main.py`
- 前端:`frontend/index.html`, `style.css`, `app.js`(純 HTML+JS+CSS,無框架)
- 測試:`tests/test_imports.py`, `test_stt_hallucination.py`, `test_translator_mock.py`
- 套件:見 `requirements.txt`(用 `webrtcvad-wheels` 而非 `webrtcvad`,Windows 無 C++ 編譯器)

## 五、兩台筆電的角色

| 筆電 | 帳號 | 角色 |
|---|---|---|
| 這台(OneDrive 同步) | Ray | 開發、改 code、寫文件 |
| 另一台 GTX 1650 4GB | User | 演唱會現場 runtime |

**注意:** Ray 這台沒有 GPU、跑不了 turbo。所有 GPU 相關修正只能在 User 那台手動套用(不在這條 OneDrive 同步路徑上)。

## 六、目前狀態

### 能跑的(CPU 場景)
- CPU 全管線:`WHISPER_MODEL=medium`,RTF ~1.5x,精度可接受
- 前端字幕滾動、4 國同顯、60 秒消失、舊句變暗
- WebSocket 推送、cache-control 防瀏覽器快取舊版

### 還沒解決
- **GPU pipeline 在 User 筆電(GTX 1650)還沒驗證成功**
- 最後一輪 `stt.py` 的 `_setup_cuda_dll_paths` 改成把 nvidia 套件 bin 同時加進 `os.environ['PATH']` 與 `os.add_dll_directory`(CT2 4.7.2 用標準 LoadLibrary、不認 SEARCH_USER_DIRS flag)
- 這個修正在 OneDrive 這份是新的,**User 筆電上需要手動套用後重跑驗證**

### 沒做(待辦,見第八節)
- 多語言輸入路由(Q3):目前強制 `language=zh`,翻譯員講話會被當成中文亂譯
- 翻譯員直顯保護:Gemini 慢回時會覆蓋翻譯員直顯的內容
- VAD 切句節奏:`SILENCE_TIMEOUT_MS=700`,主持人剛講完翻譯員馬上接話可能被合成一段

## 七、踩過的坑(不要重走)

### Whisper / faster-whisper
1. **不要用 `initial_prompt`** — Whisper 已知 bug:語音模糊時會把 prompt 尾巴當識別結果回傳(「請用中文轉寫」會一直冒出)。已從程式碼移除。
2. **語言判斷的歷史** — 早期 `language=None` 自動偵測,短句被誤判成韓文(「OK 媽媽」案例)。當時改成強制 `language=zh` 解決。但這對「翻譯員講外語」的真實場景不適用 → 待辦改回 `language=None` 並加 `language_probability` confidence 檢查(>= 0.7 才收)。
3. **turbo 在 CPU 上不快** — turbo 只砍 decoder,encoder 跟 large-v3 一樣大,CPU 上 encoder 是瓶頸。turbo 的 5~8x 加速只在 GPU 上看得到。CPU 場景請用 medium。
4. **`large-v3-turbo` 需要 faster-whisper >= 1.1.0** — 1.0.x 不認這個名字。
5. **幻覺黑名單** — Whisper 在無聲段會幻覺出「謝謝觀看 / 字幕由 / 哈哈哈哈哈」等,已在 `stt.py` 的 `HALLUCINATION_BLACKLIST` 過濾。
6. **HuggingFace 下載走 symlink** — Windows 一般帳號沒權限,WinError 1314。已改用 `download_model(output_dir=...)` 走複製檔案模式,跳過 symlink。

### 為什麼不換 SenseVoice
評估過阿里巴巴 SenseVoice-Small(中文 15x faster than Whisper-large、WER 更低):
- **只支援 5 語言:中、英、粵、日、韓** — 不支援越、泰、印、菲
- 翻譯員講非中文 → SenseVoice 強制當成訓練語言之一解碼,輸出中文音譯亂碼
- 即使加「Gemini 判斷亂碼」過濾,還是拿不到真正泰/越/印/菲文字
- 結論:不適用於多語言場景。Whisper 多語言覆蓋是必要的。

### Windows CUDA DLL 地獄(耗最久)
按順序這些套件都要齊:
- `nvidia-cublas-cu12`
- `nvidia-cudnn-cu12==9.*`
- `nvidia-cuda-runtime-cu12`(`cudart64_12.dll`,cuBLAS 內部依賴)
- `nvidia-nvjitlink-cu12`(`nvJitLink64_12.dll`,CUDA 12.4+ 開始的新依賴)

裝完還要在 import faster_whisper 之前**把 bin 資料夾加進 `os.environ['PATH']`**(光 `os.add_dll_directory` 不夠,CT2 4.7.2 沒用 SEARCH_USER_DIRS flag)。

**namespace package 偵測陷阱:** `nvidia.cublas` 等是 namespace package,`spec.origin` 是 None,要看 `spec.submodule_search_locations`。

### Gemini
1. **免費版 RPM 限制** — flash 10/min、flash-lite 15/min。預設 `TRANSLATOR_WORKERS=2`,模型 `gemini-2.5-flash-lite`。
2. **可疑翻譯檢測** — 若 4 國欄位有 3 個以上完全相同,Gemini 大概率出錯(常見是把所有欄位填同一語言),直接丟棄,別推到前端。

## 八、待辦清單

### (優先)Q3:多語言輸入路由 + 翻譯員直顯

把 `stt.py` 從強制 `language=zh` 改成完整多語言路由邏輯。**此設計同時覆蓋「只有主持人」與「主持人 + 翻譯員」兩種模式,無需切換。**

1. **stt.py:**
   - `transcribe()` 改 `language=None` 讓 Whisper 自動偵測
   - 拿 `info.language` 與 `info.language_probability`
   - 加入決策邏輯:
     - `language_probability < 0.7` → 丟棄
     - `language == "zh"` → 推 text_queue,標記 `route="translate"`(讓 Gemini 翻 4 國)
     - `language ∈ {"th", "vi", "id", "tl"}` → 推 text_queue,標記 `route="direct"` 與 `target_lang=<該語言>`
     - 其他 → 丟棄

2. **translator.py:**
   - `route="translate"` 的 item → 照原邏輯送 Gemini → 4 格全更新
   - `route="direct"` 的 item → **不送 Gemini**,直接組一個只含對應語言的 payload,推到前端
   - WebSocket payload 加欄位 `source: "gemini" | "interpreter"`

3. **frontend/app.js:**
   - 每個格子追蹤 `lastInterpreterTs`
   - 收到 `source="interpreter"` 訊息 → 更新對應格,記錄時間戳
   - 收到 `source="gemini"` 訊息 → 檢查每格的 `lastInterpreterTs`,**若 < 10 秒前則略過該格更新**(其他格照常)

4. **config.py:**
   - `SILENCE_TIMEOUT_MS` 從 700 改 500(讓 VAD 不會把主持人剛結束 + 翻譯員接話合成一段)

5. **config.py / WHISPER_LANGUAGE:**
   - 保留設定但語意改成「強制鎖定」(`zh` / `vi` / 其他)vs `auto`(預設)
   - `auto` 即上面的路由邏輯;指定特定語言則照舊強制

### (中)驗證 GPU pipeline

在 User 那台 GTX 1650 筆電套用最新 `stt.py` 的 PATH-env-var DLL 修正,確認 GPU 真的跑起來。預期 turbo 在 1650 上 RTF 0.3~0.5x、medium 0.1~0.2x。

### (低)延遲改善的進階選項

如果 Q3 + GPU 完成後延遲仍不滿意:

- **接雲端 STT** — Gemini 2.0 Live API 或 Google Cloud Speech-to-Text,延遲 ~500ms。**前提是演唱會場館有穩定有線網路,wifi 不穩會炸。**
- **滑動窗口 streaming** — 不換模型,維持 5 秒 buffer,每 0.5 秒 transcribe 一次,只 push 新增字。Whisper 不是真 streaming 設計,GPU 算力消耗 5~10 倍。GTX 1650 可能不夠力。
- **升級硬體到 RTX 3060 12GB** — 一張卡解決多數問題。

## 九、Ray 的工作偏好(必須遵守)

- **繁體中文回應**,英文要使用時 Ray 會明說
- **不加 emoji**(任何回覆、文件、腳本輸出皆是)
- **文件輸出用 HTML(.html)而非 Word(.docx)** — 風格見 `_context/rules/html-preferences.md`(黑底白字、最大寬度 720px、行距 1.75、Noto Sans TC 字體)
- 直接給答案和重點、不要廢話
- 條列式比一大段文字好讀
- 搜尋資料時附來源
- 不懂技術 → 白話解釋,不要丟一堆專業術語
- 下載/安裝任何東西前說明是什麼、安不安全

## 十、安全紅線(絕對不可跳過)

1. **未經 Ray 確認,不刪除任何檔案**
2. **覆蓋已有檔案前,必須先問 Ray 確認**
3. **重要設定存檔前,先讓 Ray 看內容**
4. 上面三件事執行前都要先問

## 十一、檔案存放規則

- 所有工作檔案存在 `C:\Users\Ray\OneDrive\Desktop\for_claude\` 內
- 跟專案相關的(含輸出)→ `projects/<專案名>/`
- 一次性、無歸屬的輸出 → `outputs/`

## 十二、快速上手指令

```powershell
# 安裝套件(第一次)
cd C:\Users\Ray\OneDrive\Desktop\for_claude\projects\concert-translator
python -m pip install -r requirements.txt

# 跑後端(會啟動 FastAPI + WebSocket on :8000)
cd backend
python main.py

# 跑測試(不需要麥克風或 API key)
cd tests
python test_imports.py
python test_stt_hallucination.py
python test_translator_mock.py
```

## 十三、有用的參考文件

- 詳細使用說明:`projects/concert-translator/使用說明.html`
- Ray 的個人背景:`_context/about-me.md`
- Ray 的工作教訓累積:`_context/lessons-learned.md`
- HTML 輸出風格:`_context/rules/html-preferences.md`
- 全域工作規則:`.claude/CLAUDE.md`(專案根)
