# Voicetyper

优雅的语音广播系统 — AI 对话 + TTS + PWA + 安全追踪

## 功能

| 頁面 | 用途 |
|---|---|
| `/admin` | 廣播控制台：手動發送 / AI 代筆 / 數字分身 / 安全監控 / 遠程錄音 |
| `/listen` | 沉浸式接收端：語音廣播收聽 + AI 語音對話 + GPS 位置回報 |
| `/m` | 手機版輕量 Admin（開發中） |

## 快速開始

```bash
# 1. 克隆倉庫
git clone https://github.com/sq756/Voicetyper
cd Voicetyper

# 2. 安裝依賴
pip install -r requirements.txt

# 3. 啟動 SoVITS API（需先安裝 GPT-SoVITS）
# 確保 http://127.0.0.1:9880 可用

# 4. 設定 config.yaml（首次執行會自動生成）
# 填入 DeepSeek API Key

# 5. 啟動
python app_center_1.7.py
```

## 依賴

- Python 3.10+
- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) — TTS 語音合成（port 9880）
- OpenAI Whisper — 語音轉文字
- DeepSeek API — AI 對話與文案生成
- ffmpeg — 音檔轉碼

## 架構

```
app_center_1.7.py    ← 伺服器主程式（FastAPI + HTML/JS/PWA）
tunnel_manager.py    ← 免費隧道（cloudflared / serveo）
android_app/         ← Android 懸浮窗 App（WebView + 前景服務）
```

## 開源協議

MIT License — 詳見 [LICENSE](LICENSE)
