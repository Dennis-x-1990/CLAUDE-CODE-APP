"""
Claude Code WebUI - hook client.

Claude Code calls this script (configured in ~/.claude/settings.json) on
session start, prompt submit, tool permission requests, etc. It relays the
hook payload to the WebUI server on 127.0.0.1:<port>.

- For async events (session_start / user_prompt_submit / stop / notification /
  session_end) it fires the POST and exits immediately, never blocking the CLI.
  If the WebUI server is not running, it cold-starts it (and optionally opens
  the browser) — this is what makes the WebUI auto-start when `claude` starts.
- For permission_request it stays alive while the server waits for the
  browser user to approve/deny, then prints the decision JSON for Claude Code
  to consume. If the server is down or nobody is watching the browser, it
  exits silently so the CLI falls back to its own terminal prompt.

Stdout protocol: any non-empty JSON printed to stdout is treated as hook
output by Claude Code; printing nothing means "no opinion, continue normal
permission flow". This script must therefore never print anything else.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "webui.config.json"

# The hook client only ever talks to the local WebUI server. The host and
# every request path are fixed literals below; only the port comes from
# config and is validated as an integer in range (safe_port).
HOST = "127.0.0.1"
PATH_SESSION_START = "/api/hook/session_start"
PATH_USER_PROMPT = "/api/hook/user_prompt_submit"
PATH_STOP = "/api/hook/stop"
PATH_NOTIFICATION = "/api/hook/notification"
PATH_SESSION_END = "/api/hook/session_end"
PATH_PERMISSION = "/api/hook/permission_request"

EVENT_PATHS = {
    "session_start": PATH_SESSION_START,
    "user_prompt_submit": PATH_USER_PROMPT,
    "stop": PATH_STOP,
    "notification": PATH_NOTIFICATION,
    "session_end": PATH_SESSION_END,
    "permission_request": PATH_PERMISSION,
}

DEFAULT_PORT = 9020
DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    "autoOpenBrowser": True,
    "permissionTimeoutSec": 120,
}

COLD_START_EVENTS = ("session_start", "user_prompt_submit", "stop", "notification")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def safe_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def server_up(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.4):
            return True
    except OSError:
        return False


def start_server(cfg: dict) -> None:
    """Launch the WebUI server detached, with no console window."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    use = pythonw if pythonw.exists() else exe
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [str(use), str(APP_DIR / "app.py")],
            cwd=str(APP_DIR),
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def wait_server(port: int, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if server_up(port):
            return True
        time.sleep(0.25)
    return False


def post(port: int, event: str, payload: dict, timeout: float) -> dict:
    path = EVENT_PATHS.get(event)
    if not path:
        return {}
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        body = json.dumps(payload).encode("utf-8")
        conn.request("POST", path, body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace").strip()
    finally:
        conn.close()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    if event not in EVENT_PATHS:
        return 0

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    cfg = load_config()
    port = safe_port(cfg.get("port"))

    cold_started = False
    if not server_up(port):
        if event in COLD_START_EVENTS:
            start_server(cfg)
            cold_started = wait_server(port, 12)
            if cold_started and event == "session_start" and cfg.get("autoOpenBrowser", True):
                try:
                    os.startfile(f"http://{HOST}:{port}")  # Windows
                except Exception:
                    pass
        else:
            # e.g. permission_request with no WebUI running:
            # stay silent so the CLI uses its own terminal prompt.
            return 0

    if event == "permission_request":
        # Server holds this HTTP request open until the browser decides
        # (or falls through when nobody is watching).
        timeout = float(cfg.get("permissionTimeoutSec", 120)) + 20
    else:
        timeout = 6

    payload.setdefault("hook_event_name", event)
    payload["webui_cold_start"] = cold_started

    try:
        result = post(port, event, payload, timeout)
    except Exception:
        return 0

    if result:
        try:
            print(json.dumps(result))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
