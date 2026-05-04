"""
Built-in tunnel manager for Voicetyper.
Auto-starts a free public tunnel so the Android app can reach this server.
Uses cloudflared (Cloudflare trycloudflare.com) — free, no account needed.
Falls back to serveo.net (SSH) if cloudflared is unavailable.
"""

import os
import re
import sys
import time
import signal
import atexit
import shutil
import subprocess
import urllib.request
import threading
import platform

CLOUDFLARED_DIR = os.path.expanduser("~/.local/bin")
CLOUDFLARED_PATH = os.path.join(CLOUDFLARED_DIR, "cloudflared")
CLOUDFLARED_URL = {
    "linux": {
        "x86_64": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
        "aarch64": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64",
        "arm": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm",
    },
    "darwin": {
        "x86_64": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64",
    },
}.get(sys.platform, {}).get(platform.machine(), None)

_tunnel_proc = None
_public_url = None
_url_lock = threading.Lock()


def _ensure_cloudflared():
    """Download cloudflared binary if not present."""
    if os.path.exists(CLOUDFLARED_PATH):
        return CLOUDFLARED_PATH

    if CLOUDFLARED_URL is None:
        return None  # unsupported platform

    os.makedirs(CLOUDFLARED_DIR, exist_ok=True)

    print(f"  Downloading cloudflared (~38MB)...", end="", flush=True)

    def _report(count, block_size, total_size):
        if total_size > 0 and count % 10 == 0:
            pct = min(count * block_size * 100 // total_size, 100)
            print(f"\r  Downloading cloudflared... {pct}%", end="", flush=True)

    try:
        urllib.request.urlretrieve(CLOUDFLARED_URL, CLOUDFLARED_PATH, _report)
    except Exception as e:
        print(f"\r  Download failed: {e}")
        return None

    os.chmod(CLOUDFLARED_PATH, 0o755)
    print(f"\r  cloudflared downloaded to {CLOUDFLARED_PATH}")
    return CLOUDFLARED_PATH


def _start_cloudflared(port: int) -> str | None:
    """Start cloudflared tunnel, return public URL once available."""
    cf = _ensure_cloudflared()
    if not cf:
        return None

    global _tunnel_proc
    try:
        _tunnel_proc = subprocess.Popen(
            [cf, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,
        )
    except Exception as e:
        print(f"  cloudflared failed to start: {e}")
        return None

    # Parse the URL from stdout — format: https://xxxx.trycloudflare.com
    deadline = time.time() + 25
    url = None
    for line in _tunnel_proc.stdout:
        m = re.search(r"https://[-\w]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break
        if time.time() > deadline:
            break

    if url is None:
        print("  cloudflared started but URL not detected — is port correct?")
        return None

    return url


def _start_serveo(port: int, subdomain: str = None) -> str | None:
    """Use serveo.net SSH tunnel with optional fixed subdomain.
    With subdomain: https://<name>.serveo.net (stable across restarts)
    Without: random subdomain"""
    if not shutil.which("ssh"):
        return None

    global _tunnel_proc
    # If subdomain given, use fixed name: ssh -R name:80:localhost:PORT serveo.net
    remote = f"{subdomain}:80:localhost:{port}" if subdomain else f"80:localhost:{port}"
    try:
        _tunnel_proc = subprocess.Popen(
            [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=60",
                "-R", remote,
                "serveo.net",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,
        )
    except Exception:
        return None

    deadline = time.time() + 15
    url = None
    for line in _tunnel_proc.stdout:
        m = re.search(r"https://[-\w]+\.serveo\.\w+", line)
        if m:
            url = m.group(0)
            break
        if time.time() > deadline:
            break

    return url


def start(port: int = 7860, subdomain: str = None) -> str:
    """Start a public tunnel. Returns the public URL or localhost fallback.
    If subdomain is set, tries serveo.net with a FIXED URL first (stable across restarts).
    Falls back to cloudflared (random URL)."""
    global _public_url

    with _url_lock:
        if _public_url:
            return _public_url

    url = None

    # 如果指定了固定子域名，優先使用 serveo（URL 不會變）
    if subdomain:
        print(f"  Trying serveo.net with fixed name '{subdomain}'...", flush=True)
        url = _start_serveo(port, subdomain)

    # 兜底：cloudflared
    if not url:
        url = _start_cloudflared(port)

    # 最後兜底：serveo 隨機子域名
    if not url and not subdomain:
        print("  cloudflared unavailable, trying serveo.net...", flush=True)
        url = _start_serveo(port)

    with _url_lock:
        if url:
            _public_url = url
            atexit.register(_cleanup)
            return url
        _public_url = f"http://localhost:{port}"
        return _public_url


def _cleanup():
    global _tunnel_proc
    if _tunnel_proc and _tunnel_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_tunnel_proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        _tunnel_proc = None


def get_url() -> str | None:
    with _url_lock:
        return _public_url
