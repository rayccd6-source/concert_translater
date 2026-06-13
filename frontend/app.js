// 演唱會即時翻譯 - 前端 WebSocket 接收 + 字幕渲染
// 新行為：每句話從底部進入，舊句子往上推；超過 LIFETIME_MS 自動淡出消失
(function () {
    // 字幕保留秒數（毫秒）— 用戶要求 60 秒
    const LIFETIME_MS = 60 * 1000;
    // 為了防止視覺擁擠，單一 cell 最多保留幾行
    const MAX_LINES_PER_CELL = 8;
    // 每秒掃描一次過期字幕
    const SWEEP_INTERVAL_MS = 1000;

    const elOriginal = document.getElementById('original-text');
    const elStatusDot = document.getElementById('status-dot');
    const elStatusText = document.getElementById('status-text');
    const streams = {
        thai: document.getElementById('stream-thai'),
        vietnamese: document.getElementById('stream-vietnamese'),
        indonesian: document.getElementById('stream-indonesian'),
        filipino: document.getElementById('stream-filipino'),
    };
    const CELL_KEYS = ['thai', 'vietnamese', 'indonesian', 'filipino'];

    // 翻譯員直顯保護：某格被翻譯員直顯後，這段時間內忽略 Gemini 對該格的覆蓋
    const INTERPRETER_PROTECT_MS = 10 * 1000;
    const lastInterpreterTs = {
        thai: 0, vietnamese: 0, indonesian: 0, filipino: 0,
    };

    let ws = null;
    let reconnectDelay = 1000;

    function setStatus(connected) {
        if (connected) {
            elStatusDot.classList.add('connected');
            elStatusText.textContent = '已連線';
        } else {
            elStatusDot.classList.remove('connected');
            elStatusText.textContent = '重連中...';
        }
    }

    function appendLine(streamEl, text) {
        if (!text || !text.trim()) return;
        const line = document.createElement('div');
        line.className = 'line';
        line.textContent = text;
        // data-ts 用本地時間，方便清理（後端送來的 timestamp 可能跟前端時鐘不同步）
        line.dataset.ts = Date.now().toString();
        streamEl.appendChild(line);

        // 限制每個 cell 行數，超過就把最舊的（最頂端）標為 expired
        const lines = streamEl.querySelectorAll('.line:not(.expired)');
        if (lines.length > MAX_LINES_PER_CELL) {
            expireLine(lines[0]);
        }
    }

    function expireLine(lineEl) {
        if (lineEl.classList.contains('expired')) return;
        lineEl.classList.add('expired');
        // 動畫結束後從 DOM 移除
        setTimeout(() => {
            if (lineEl.parentNode) lineEl.parentNode.removeChild(lineEl);
        }, 700);
    }

    function sweepExpiredLines() {
        const now = Date.now();
        for (const streamEl of Object.values(streams)) {
            const lines = streamEl.querySelectorAll('.line:not(.expired)');
            // 標記過期（時間超過 LIFETIME_MS）
            for (const line of lines) {
                const ts = parseInt(line.dataset.ts || '0', 10);
                if (now - ts > LIFETIME_MS) {
                    expireLine(line);
                }
            }
            // 標記「年齡」class — 越舊越暗
            const alive = streamEl.querySelectorAll('.line:not(.expired)');
            const total = alive.length;
            alive.forEach((line, idx) => {
                line.classList.remove('age-1', 'age-2', 'age-3');
                // 從底數來：最新（最後）不加 class，倒數第 2~4 漸暗
                const fromBottom = total - 1 - idx;
                if (fromBottom === 1) line.classList.add('age-1');
                else if (fromBottom === 2) line.classList.add('age-2');
                else if (fromBottom >= 3) line.classList.add('age-3');
            });
        }
    }

    function applyTranslation(data) {
        if (data.original) elOriginal.textContent = data.original;

        // source: "interpreter" = 翻譯員直顯（單格）; "gemini" = 4 國翻譯
        const source = data.source || 'gemini';
        const now = Date.now();
        for (const key of CELL_KEYS) {
            const text = data[key];
            if (!text || !text.trim()) continue;
            // Gemini 結果：若該格 10 秒內剛被翻譯員直顯覆蓋，跳過該格（其他格照常）
            if (source === 'gemini'
                && now - lastInterpreterTs[key] < INTERPRETER_PROTECT_MS) {
                continue;
            }
            appendLine(streams[key], text);
            if (source === 'interpreter') lastInterpreterTs[key] = now;
        }
    }

    function connect() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${location.host}/ws`;
        ws = new WebSocket(url);

        ws.onopen = () => {
            setStatus(true);
            reconnectDelay = 1000;
            setInterval(() => {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send('ping');
                }
            }, 20000);
        };

        ws.onmessage = (e) => {
            try {
                const data = JSON.parse(e.data);
                applyTranslation(data);
            } catch (err) {
                console.warn('Bad message:', e.data);
            }
        };

        ws.onclose = () => {
            setStatus(false);
            setTimeout(connect, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 1.5, 5000);
        };

        ws.onerror = () => {
            setStatus(false);
        };
    }

    // 啟動：定時清理 + 連線
    setInterval(sweepExpiredLines, SWEEP_INTERVAL_MS);
    connect();
})();
