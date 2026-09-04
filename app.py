"""
Claude Code Desktop Companion - Backend API

Reads local Claude Code CLI data (~/.claude/projects/*.jsonl, settings.json)
and serves it to the web UI. Additionally ingests Claude Code hook events so
the user's own terminal sessions sync to the browser in real time:

  POST /api/hook/session_start        auto-start marker + active session
  POST /api/hook/user_prompt_submit   instant push of user messages
  POST /api/hook/stop                 instant refresh when a reply completes
  POST /api/hook/notification         idle / waiting toasts
  POST /api/hook/session_end          clears the active-session indicator
  POST /api/hook/permission_request   blocks until the browser approves/denies
"""
import asyncio
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
import uvicorn

from uploads_api import UPLOAD_ROOT, router as uploads_router

APP_DIR = Path(__file__).resolve().parent
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
SETTINGS_LOCAL_FILE = CLAUDE_DIR / "settings.local.json"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"
STATE_FILE = CLAUDE_DIR / "webui-state.json"
TITLES_FILE = CLAUDE_DIR / "webui-titles.json"
ARCHIVE_FILE = CLAUDE_DIR / "webui-archived.json"
TRASH_DIR = CLAUDE_DIR / "webui-trash"

CONFIG_FILE = APP_DIR / "webui.config.json"
DEFAULT_CONFIG = {
    "port": 9020,
    "autoOpenBrowser": False,       # auto-open a browser page when a hook cold-starts
                                    # the server (off by default: use the desktop app)
    "autoInstallHooks": True,       # write hooks into ~/.claude/settings.json at startup
    "autoStartAtBoot": True,        # register a Windows login autostart entry
    "browserApprovalEnabled": True, # PermissionRequest hooks may wait for a browser decision
    "permissionTimeoutSec": 120,    # how long the CLI waits for a browser decision
}
CONFIG = dict(DEFAULT_CONFIG)

# --open-browser: passed by the Windows autostart entry / hook cold-start so
# the control page opens once the server is listening.
OPEN_BROWSER_ON_START = "--open-browser" in sys.argv or "--open-app" in sys.argv


def open_app_window() -> None:
    """Open the control page as a standalone desktop app window
    (Edge/Chrome --app mode: no address bar, taskbar-pinnable)."""
    for exe in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if os.path.exists(exe):
            try:
                threading.Thread(
                    target=subprocess.run,
                    args=([exe, "--app=http://127.0.0.1:9020/"],),
                    kwargs={"shell": False},
                    daemon=True,
                ).start()
                return
            except Exception:
                continue
    try:
        os.startfile("http://127.0.0.1:9020/")
    except Exception:
        pass

# Claude Code session ids are hex/UUID-like tokens; enforce that shape anywhere
# a session id flows into a path or a CLI argument. Project keys are bare
# directory names — no separators, no dot segments.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
PROJECT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,200}$")


def _load_config() -> None:
    global CONFIG
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    CONFIG = cfg


def _save_config() -> None:
    CONFIG_FILE.write_text(
        json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


_load_config()

# ── runtime state ─────────────────────────────────────────────────────

MAIN_LOOP: asyncio.AbstractEventLoop | None = None
WS_CLIENTS: list = []
LAST_FRONTEND_SEEN = 0.0  # updated by /api/poll & /api/active → "browser connected" heuristic

ACTIVE: dict = {
    "sessionId": None,
    "cwd": None,
    "lastEvent": None,
    "lastActivity": 0,
    "lastPrompt": "",
    "lastAssistantMessage": "",
}


def _load_active_state() -> None:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            ACTIVE.update({k: data.get(k) for k in ACTIVE})
    except Exception:
        pass


def _save_active_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(ACTIVE, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_load_active_state()

# ── FastAPI app (routes below register on this) ──────────────────────

static_dir = APP_DIR / "static"
static_dir.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    try:
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if CONFIG.get("autoInstallHooks", True):
        try:
            import install_hooks
            install_hooks.ensure_installed()
            print("[webui] Claude Code hooks installed (SessionStart/UserPromptSubmit/"
                  "Stop/Notification/PermissionRequest/SessionEnd)")
        except Exception as e:
            print(f"[webui] hook install failed: {e}")
    if CONFIG.get("autoStartAtBoot", True):
        try:
            import install_autostart
            if install_autostart.ensure_installed():
                print("[webui] Windows login autostart registered")
        except Exception as e:
            print(f"[webui] autostart install failed: {e}")
    if OPEN_BROWSER_ON_START:
        def _open_when_ready():
            time.sleep(1.5)
            open_app_window()
        threading.Thread(target=_open_when_ready, daemon=True).start()
    yield


app = FastAPI(title="Claude Code Desktop", lifespan=lifespan)
app.include_router(uploads_router)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── helpers ──────────────────────────────────────────────────────────

def _scan_jsonl_files() -> dict:
    """Scan all project dirs and return {sessionId: filepath} mapping."""
    mapping = {}
    if not PROJECTS_DIR.exists():
        return mapping
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            mapping[f.stem] = f
    return mapping


_PROMPT_CACHE = {}


def _parse_first_prompt(filepath: Path) -> str:
    """Extract first user prompt from a JSONL session file (mtime-cached)."""
    try:
        mtime = filepath.stat().st_mtime
    except OSError:
        return ""
    key = str(filepath)
    cached = _PROMPT_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    prompt = ""
    try:
        for line_no, line in enumerate(open(filepath, encoding="utf-8", errors="replace")):
            if line_no > 100:
                break
            obj = json.loads(line)
            if obj.get("type") == "user" and obj.get("message", {}).get("role") == "user":
                content = obj["message"].get("content", "")
                if isinstance(content, str):
                    prompt = content[:200]
                    break
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            prompt = c.get("text", "")[:200]
                            break
                    if prompt:
                        break
    except Exception:
        pass
    _PROMPT_CACHE[key] = (mtime, prompt)
    return prompt


_CWD_CACHE = {}


def _session_cwd(filepath: Path):
    """Resolve a session's real working directory from the `cwd` field in its
    JSONL (mtime-cached — /api/sessions walks every session on each call)."""
    try:
        mtime = filepath.stat().st_mtime
    except OSError:
        return None
    key = str(filepath)
    cached = _CWD_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    cwd = None
    try:
        with open(filepath, encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh):
                if line_no > 50:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd:
                    break
    except Exception:
        pass
    _CWD_CACHE[key] = (mtime, cwd)
    return cwd


def _list_all_sessions(archived_only: bool = False) -> list:
    """Scan all project dirs — driven by actual JSONL files on disk.
    archived_only=True returns ONLY archived sessions; False returns only
    non-archived ones."""
    entries = []
    all_files = _scan_jsonl_files()
    titles = _load_meta(TITLES_FILE)
    archived = _load_meta(ARCHIVE_FILE)
    sortable = []
    for sid, fpath in all_files.items():
        if (sid in archived) != archived_only:
            continue
        try:
            sortable.append((sid, fpath, fpath.stat()))
        except OSError:
            continue
    sortable.sort(key=lambda x: x[2].st_mtime, reverse=True)

    for sid, fpath, stat in sortable:
        first_prompt = _parse_first_prompt(fpath)
        entries.append({
            "sessionId": sid,
            "firstPrompt": first_prompt,
            "summary": first_prompt[:80] if first_prompt else sid[:8],
            "title": titles.get(sid) or "",
            "archived": sid in archived,
            "messageCount": 0,
            "created": datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat(),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "cwd": _session_cwd(fpath),
            "projectPath": str(fpath.parent),
            "projectKey": fpath.parent.name,
            "fileSize": stat.st_size,
        })
    return entries


def _read_jsonl_tail(fpath: Path, limit: int) -> list:
    """Read the LAST `limit` records of a session file (recent context first-page)."""
    try:
        size = fpath.stat().st_size
        with open(fpath, "rb") as fh:
            if size > 4_000_000:
                fh.seek(-4_000_000, os.SEEK_END)
                fh.readline()  # drop the partial line at the seek boundary
            raw = fh.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if len(lines) > limit:
        lines = lines[-limit:]
    messages = []
    for line in lines:
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return messages


def _parse_jsonl(fpath: Path, limit: int) -> list:
    messages = []
    with open(fpath, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if len(messages) >= limit:
                break
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages


def _read_conversation(session_id: str, project=None, limit: int = 500) -> list:
    if project:
        if not PROJECT_KEY_RE.fullmatch(str(project)):
            raise HTTPException(status_code=400, detail="Invalid project key")
        proj = PROJECTS_DIR / str(project)
        # the project dir must be a direct child of PROJECTS_DIR — no traversal
        try:
            if proj.resolve().parent != PROJECTS_DIR.resolve():
                raise HTTPException(status_code=400, detail="Invalid project key")
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Invalid project key: {e}")
        fpath = proj / f"{session_id}.jsonl"
        if fpath.exists():
            return _read_jsonl_tail(fpath, limit)

    fpath = _scan_jsonl_files().get(session_id)
    if not fpath:
        raise HTTPException(status_code=404, detail="Session not found")
    return _read_jsonl_tail(fpath, limit)


def _resolve_session_cwd(session_id):
    """Best-effort working directory for a session (hook state → JSONL → home)."""
    if session_id and ACTIVE.get("sessionId") == session_id and ACTIVE.get("cwd"):
        return ACTIVE["cwd"]
    if session_id:
        fpath = _scan_jsonl_files().get(session_id)
        if fpath:
            cwd = _session_cwd(fpath)
            if cwd and Path(cwd).exists():
                return cwd
    return str(Path.home())


def _valid_cwd(cwd) -> bool:
    """A usable working directory: absolute, existing, and a directory."""
    if not isinstance(cwd, str) or not cwd:
        return False
    try:
        p = Path(cwd)
        return p.is_absolute() and p.exists() and p.is_dir()
    except OSError:
        return False


def _read_settings() -> dict:
    settings = {}
    for f in (SETTINGS_FILE, SETTINGS_LOCAL_FILE):
        if f.exists():
            try:
                settings.update(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
    return settings


def _load_meta(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_meta(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _write_settings_env(updates: dict) -> dict:
    """Write model settings to settings.local.json env block."""
    current = {}
    if SETTINGS_LOCAL_FILE.exists():
        try:
            current = json.loads(SETTINGS_LOCAL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    if "env" not in current:
        current["env"] = {}
    current["env"].update(updates)

    SETTINGS_LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_LOCAL_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return current


def _get_claude_exe() -> str:
    """Locate the claude CLI. Only pre-known install locations are accepted —
    never a path derived from user input. The native claude.exe is preferred
    for PTY spawning (the .cmd shim just forwards to it)."""
    npm_bin = Path.home() / "AppData" / "Roaming" / "npm"
    native = npm_bin / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if native.is_file():
        return str(native)
    for name in ("claude.cmd", "claude"):
        p = npm_bin / name
        if p.is_file():
            return str(p)
    return "claude"


# ── WebSocket broadcasting ────────────────────────────────────────────

async def _ws_broadcast(payload: dict) -> None:
    dead = []
    for ws in list(WS_CLIENTS):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            WS_CLIENTS.remove(ws)
        except ValueError:
            pass


def broadcast_threadsafe(payload: dict) -> None:
    """Broadcast from a non-asyncio thread (managed CLI reader, etc.)."""
    if MAIN_LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_ws_broadcast(payload), MAIN_LOOP)
    except Exception:
        pass


def _browser_connected() -> bool:
    """A browser tab is considered present if a WebSocket is open or the
    frontend polled recently. Used to avoid hijacking the terminal prompt."""
    if WS_CLIENTS:
        return True
    return (time.time() - LAST_FRONTEND_SEEN) < 10.0


# ── permission request queue ──────────────────────────────────────────

PENDING_PERMISSIONS = {}  # id -> {perm, event: asyncio.Event, result}


def _permission_public(perm: dict) -> dict:
    tool = perm.get("toolName", "")
    ti = perm.get("toolInput") or {}
    detail = ""
    if isinstance(ti, dict):
        if tool == "Bash":
            detail = str(ti.get("command", ""))[:300]
        elif tool in ("Edit", "Write", "Read", "NotebookEdit"):
            detail = str(ti.get("file_path", ti.get("notebook_path", "")))[:300]
        elif tool == "WebFetch":
            detail = str(ti.get("url", ""))[:300]
        elif tool == "WebSearch":
            detail = str(ti.get("query", ""))[:300]
        if not detail:
            try:
                detail = json.dumps(ti, ensure_ascii=False)[:300]
            except Exception:
                detail = ""
    suggestions = perm.get("suggestions") or []
    return {
        "id": perm.get("id"),
        "sessionId": perm.get("sessionId"),
        "cwd": perm.get("cwd"),
        "toolName": tool,
        "toolInput": ti if isinstance(ti, dict) else {},
        "detail": detail,
        "createdAt": perm.get("createdAt"),
        "suggestions": suggestions[:6] if isinstance(suggestions, list) else [],
    }


def _read_targets_upload_root(payload: dict) -> bool:
    """True when a Read tool call targets the WebUI uploads directory — those
    are auto-allowed so attachments can be read without prompting."""
    if payload.get("tool_name") != "Read":
        return False
    ti = payload.get("tool_input") or {}
    target = str(ti.get("file_path", ""))
    try:
        root = str(UPLOAD_ROOT.resolve())
        return os.path.abspath(target).startswith(root + os.sep)
    except Exception:
        return False


async def _handle_permission_request(payload: dict) -> dict:
    """Called from the PermissionRequest hook. Blocks the hook (and therefore
    the CLI's permission prompt) until the browser decides, the timeout hits,
    or we fall through when no browser is watching."""
    if _read_targets_upload_root(payload):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow", "message": "Auto-allowed: read of WebUI uploads dir"},
            }
        }

    if not CONFIG.get("browserApprovalEnabled", True):
        return {}
    if not _browser_connected():
        return {}

    perm_id = uuid.uuid4().hex[:12]
    suggestions = payload.get("permission_suggestions") or []
    perm = {
        "id": perm_id,
        "sessionId": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "toolName": payload.get("tool_name", "unknown"),
        "toolInput": payload.get("tool_input") or {},
        "toolUseId": payload.get("tool_use_id"),
        "createdAt": int(time.time() * 1000),
        "suggestions": suggestions if isinstance(suggestions, list) else [],
    }
    entry = {"perm": perm, "event": asyncio.Event(), "result": None}
    PENDING_PERMISSIONS[perm_id] = entry

    await _ws_broadcast({"type": "permission_request", "permission": _permission_public(perm)})

    timeout = float(CONFIG.get("permissionTimeoutSec", 120))
    try:
        await asyncio.wait_for(entry["event"].wait(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        pass

    PENDING_PERMISSIONS.pop(perm_id, None)
    result = entry["result"]
    decision_label = result["behavior"] if result else "timeout"
    await _ws_broadcast({
        "type": "permission_resolved",
        "id": perm_id,
        "decision": decision_label,
    })

    if not result:
        return {}  # fall through → terminal prompt / normal CLI behavior

    decision = {"behavior": result["behavior"]}
    if result.get("message"):
        decision["message"] = result["message"]
    if result.get("updatedPermissions"):
        decision["updatedPermissions"] = result["updatedPermissions"]
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


# ── hook ingestion ────────────────────────────────────────────────────

async def _ingest_hook(event: str, payload: dict) -> dict:
    sid = payload.get("session_id")
    cwd = payload.get("cwd")

    if event == "session_start":
        ACTIVE.update({"sessionId": sid, "cwd": cwd if _valid_cwd(cwd) else None,
                       "lastEvent": "session_start", "lastActivity": time.time()})
        _save_active_state()
        await _ws_broadcast({"type": "session_started", "sessionId": sid, "cwd": cwd})
    elif event == "user_prompt_submit":
        ACTIVE.update({"sessionId": sid, "cwd": cwd if _valid_cwd(cwd) else ACTIVE.get("cwd"),
                       "lastEvent": "user_prompt_submit", "lastActivity": time.time(),
                       "lastPrompt": str(payload.get("prompt", ""))[:200]})
        _save_active_state()
        await _ws_broadcast({"type": "reload", "sessionId": sid, "reason": "prompt"})
    elif event == "stop":
        ACTIVE.update({"sessionId": sid or ACTIVE.get("sessionId"),
                       "cwd": cwd if _valid_cwd(cwd) else ACTIVE.get("cwd"),
                       "lastEvent": "stop", "lastActivity": time.time(),
                       "lastAssistantMessage": str(payload.get("last_assistant_message", ""))[:300]})
        _save_active_state()
        await _ws_broadcast({"type": "reload", "sessionId": sid, "reason": "stop"})
        await _ws_broadcast({"type": "notification", "level": "green",
                             "message": "Claude 已完成回复"})
    elif event == "notification":
        await _ws_broadcast({"type": "notification", "level": "yellow",
                             "message": str(payload.get("message", ""))[:200]})
    elif event == "session_end":
        if sid and ACTIVE.get("sessionId") == sid:
            ACTIVE.update({"lastEvent": "session_end", "lastActivity": time.time()})
            _save_active_state()
            await _ws_broadcast({"type": "session_ended", "sessionId": sid})
    return {"ok": True}


# ── API routes ────────────────────────────────────────────────────────

@app.get("/api/sessions")
def list_sessions(search: str = Query(default=""), project=None,
                  archived: str = Query(default="")):
    archived_only = archived in ("1", "true", "yes")
    entries = _list_all_sessions(archived_only=archived_only)
    # custom titles override the summary everywhere
    for e in entries:
        if e.get("title"):
            e["summary"] = e["title"]
    if search:
        q = search.lower()
        entries = [e for e in entries
                   if q in (e.get("firstPrompt", "") + e.get("summary", "")).lower()]
    return {
        "sessions": entries,
        "activeSessionId": ACTIVE.get("sessionId"),
    }


class SessionTitle(BaseModel):
    title: str


@app.post("/api/sessions/{session_id}/rename")
def rename_session(session_id: str, body: SessionTitle):
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    if session_id not in _scan_jsonl_files():
        raise HTTPException(status_code=404, detail="Session not found")
    title = body.title.strip()[:120]
    titles = _load_meta(TITLES_FILE)
    if title:
        titles[session_id] = title
    else:
        titles.pop(session_id, None)   # empty title → revert to auto
    _save_meta(TITLES_FILE, titles)
    return {"ok": True, "title": title}


class ArchivedFlag(BaseModel):
    archived: bool


@app.post("/api/sessions/{session_id}/archive")
def archive_session(session_id: str, body: ArchivedFlag):
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    if session_id not in _scan_jsonl_files():
        raise HTTPException(status_code=404, detail="Session not found")
    archived = _load_meta(ARCHIVE_FILE)
    if body.archived:
        archived[session_id] = int(time.time())
    else:
        archived.pop(session_id, None)
    _save_meta(ARCHIVE_FILE, archived)
    return {"ok": True, "archived": body.archived}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    """Soft-delete: the session file is moved into ~/.claude/webui-trash/ so
    it can be restored manually."""
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    fpath = _scan_jsonl_files().get(session_id)
    if not fpath:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        dest = TRASH_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{session_id}.jsonl"
        fpath.rename(dest)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
    # clean up metadata
    for meta in (TITLES_FILE, ARCHIVE_FILE):
        data = _load_meta(meta)
        if session_id in data:
            data.pop(session_id, None)
            _save_meta(meta, data)
    if ACTIVE.get("sessionId") == session_id:
        ACTIVE.update({"sessionId": None, "lastEvent": "deleted", "lastActivity": time.time()})
        _save_active_state()
    return {"ok": True}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, project=None, limit: int = 500):
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    messages = _read_conversation(session_id, project, limit)
    return {"sessionId": session_id, "messages": messages, "count": len(messages)}


@app.get("/api/active")
def get_active():
    global LAST_FRONTEND_SEEN
    LAST_FRONTEND_SEEN = time.time()
    sid = ACTIVE.get("sessionId")
    messages = []
    if sid:
        try:
            messages = _read_conversation(sid, limit=100)
        except HTTPException:
            pass
    return {"active": dict(ACTIVE), "messages": messages}


@app.get("/api/settings")
def get_settings():
    settings = _read_settings()
    env = settings.get("env", {})
    return {
        "model": env.get("ANTHROPIC_MODEL", "unknown"),
        "opusModel": env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", ""),
        "sonnetModel": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
        "haikuModel": env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", ""),
        "baseUrl": env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        "maxTokens": env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", ""),
        "allEnv": env,
    }


class ModelUpdate(BaseModel):
    model: str
    key: str = "ANTHROPIC_MODEL"


@app.post("/api/settings/model")
def set_model(update: ModelUpdate):
    _write_settings_env({update.key: update.model})
    return {"ok": True, "model": update.model}


@app.get("/api/history")
def get_history(limit: int = 50):
    items = []
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        for line in reversed(lines[-limit * 2:]):
            try:
                obj = json.loads(line)
                items.append(obj)
            except Exception:
                continue
            if len(items) >= limit:
                break
    return {"history": items}


# health / poll endpoint for frontend to check if active session changed
@app.get("/api/poll")
def poll():
    global LAST_FRONTEND_SEEN
    LAST_FRONTEND_SEEN = time.time()
    session_sizes = {}
    for sid, fpath in _scan_jsonl_files().items():
        try:
            session_sizes[sid] = fpath.stat().st_size
        except OSError:
            continue
    return {
        "activeSessionId": ACTIVE.get("sessionId"),
        "active": {k: ACTIVE.get(k) for k in
                   ("sessionId", "cwd", "lastEvent", "lastActivity", "lastPrompt")},
        "sessionSizes": session_sizes,
        "pendingPermissions": [_permission_public(e["perm"]) for e in PENDING_PERMISSIONS.values()],
        "browserApprovalEnabled": bool(CONFIG.get("browserApprovalEnabled", True)),
        "managedRunning": managed.running,
        "timestamp": int(time.time() * 1000),
    }


# track background send jobs
_send_jobs = {}


class SendMessage(BaseModel):
    message: str


@app.post("/api/sessions/{session_id}/send")
def send_message(session_id: str, body: SendMessage):
    """Send a message to a session by spawning `claude -p --resume`.

    The session id is validated against SESSION_ID_RE and the message text is
    piped via stdin — no externally-supplied string ever becomes a CLI flag or
    a shell fragment (argv array + shell=False + stdin prompt).
    """
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    all_files = _scan_jsonl_files()
    fpath = all_files.get(session_id)
    if not fpath:
        raise HTTPException(status_code=404, detail="Session not found")

    cwd = _resolve_session_cwd(session_id)

    settings = _read_settings()
    env = os.environ.copy()
    env.update(settings.get("env", {}))

    # ensure npm global bin is in PATH (where claude CLI lives)
    npm_bin = str(Path.home() / "AppData" / "Roaming" / "npm")
    if npm_bin not in env.get("PATH", ""):
        env["PATH"] = npm_bin + os.pathsep + env.get("PATH", "")

    claude_exe = _get_claude_exe()

    job_id = f"{session_id[:8]}-{int(time.time())}"
    _send_jobs[job_id] = {"status": "running", "sessionId": session_id, "started": time.time()}

    def _run():
        try:
            proc = subprocess.run(
                [claude_exe, "-p", "--resume", session_id],
                input=body.message,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                shell=False,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            _send_jobs[job_id]["status"] = "done"
            _send_jobs[job_id]["exitCode"] = proc.returncode
            _send_jobs[job_id]["stderr"] = (proc.stderr or "")[:500]
            _send_jobs[job_id]["stdoutTail"] = (proc.stdout or "")[-500:]
        except subprocess.TimeoutExpired:
            _send_jobs[job_id]["status"] = "timeout"
        except FileNotFoundError:
            _send_jobs[job_id]["status"] = "error"
            _send_jobs[job_id]["error"] = "claude CLI not found in PATH"
        except Exception as e:
            _send_jobs[job_id]["status"] = "error"
            _send_jobs[job_id]["error"] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "jobId": job_id, "message": "Message sent, processing in background"}


# ── chat streaming engine (chat-app style, driven by claude -p) ──────

_chat_jobs: dict = {}
_chat_seq = 0


class ChatRequest(BaseModel):
    sessionId: str | None = None   # None → start a brand-new session in `cwd`
    cwd: str | None = None
    message: str
    permissionMode: str | None = None  # manual | acceptEdits | dontAsk | plan | bypassPermissions


@app.post("/api/chat")
def chat_start(body: ChatRequest):
    """Drive Claude Code headlessly with stream-json output.

    The user's text is piped via stdin; Claude Code emits newline-delimited
    JSON events (partial text deltas included) that the browser polls for a
    typewriter-style live reply. Permission requests still flow through the
    PermissionRequest hook to the browser approval bar."""
    global _chat_seq
    sid = body.sessionId
    if sid:
        if not SESSION_ID_RE.fullmatch(sid):
            raise HTTPException(status_code=400, detail="Invalid session id")
        if sid not in _scan_jsonl_files():
            raise HTTPException(status_code=404, detail="Session not found")
    cwd = body.cwd if _valid_cwd(body.cwd) else _resolve_session_cwd(sid)

    settings = _read_settings()
    env = os.environ.copy()
    env.update(settings.get("env", {}))
    npm_bin = str(Path.home() / "AppData" / "Roaming" / "npm")
    if npm_bin not in env.get("PATH", ""):
        env["PATH"] = npm_bin + os.pathsep + env.get("PATH", "")

    args = [_get_claude_exe(), "-p",
            "--output-format", "stream-json", "--include-partial-messages",
            "--verbose"]
    if sid:
        args += ["--resume", sid]
    mode = body.permissionMode
    if mode:
        allowed_modes = ("manual", "acceptEdits", "dontAsk", "plan", "bypassPermissions")
        if mode not in allowed_modes:
            raise HTTPException(status_code=400, detail=f"Invalid permission mode: {mode}")
        args += ["--permission-mode", mode]

    _chat_seq += 1
    job_id = f"chat-{int(time.time())}-{_chat_seq}"
    job = {"id": job_id, "status": "running", "events": [],
           "sessionId": sid, "cwd": cwd, "started": time.time(),
           "proc": None, "stderrTail": ""}
    _chat_jobs[job_id] = job
    while len(_chat_jobs) > 20:   # keep the last 20 jobs
        _chat_jobs.pop(next(iter(_chat_jobs)), None)

    def _drain_stderr(proc, job):
        try:
            tail = []
            for line in proc.stderr:
                tail.append(line)
                if len(tail) > 20:
                    tail.pop(0)
            job["stderrTail"] = "".join(tail)[-800:]
        except (ValueError, OSError):
            pass

    def _run():
        try:
            proc = subprocess.Popen(
                args, cwd=cwd, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                shell=False,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            job["proc"] = proc
            threading.Thread(target=_drain_stderr, args=(proc, job), daemon=True).start()
            try:
                proc.stdin.write(body.message)
                proc.stdin.close()
            except OSError:
                pass
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                job["events"].append(ev)
                if isinstance(ev, dict):
                    if ev.get("session_id"):
                        job["sessionId"] = ev["session_id"]
                    if ev.get("type") == "result":
                        job["status"] = "done"
                if time.time() - job["started"] > 1800:   # 30 min kill switch
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    job["status"] = "timeout"
                    break
            if job["status"] == "running":
                job["status"] = "done"
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        except FileNotFoundError:
            job["status"] = "error"
            job["error"] = "claude CLI not found in PATH"
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "jobId": job_id}


@app.get("/api/chat/{job_id}")
def chat_poll(job_id: str, since: int = 0):
    job = _chat_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "status": job["status"],
        "sessionId": job["sessionId"],
        "cwd": job["cwd"],
        "events": job["events"][since:],
        "next": len(job["events"]),
        "error": job.get("error"),
    }


@app.post("/api/chat/{job_id}/stop")
def chat_stop(job_id: str):
    job = _chat_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    proc = job.get("proc")
    if proc and job["status"] == "running":
        try:
            proc.terminate()
        except Exception:
            pass
        job["status"] = "stopped"
    return {"ok": True, "status": job["status"]}


# ── recent projects & session files (receive-side of file transfer) ──

@app.get("/api/fs/list")
def fs_list(path: str = Query(default="")):
    """List subdirectories of a local folder for the new-session folder
    picker. Read-only: returns directory names only, never file contents.
    Called without `path` it returns available drive roots."""
    import string

    if not path:
        drives = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append(drive)
        return {"path": "", "parent": None, "dirs": drives, "drives": True}

    p = Path(path)
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="absolute path required")
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail="folder not found")
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            try:
                if child.is_dir():
                    dirs.append(child.name)
            except OSError:
                continue
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot list folder: {e}")
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": dirs}


@app.get("/api/projects")
def list_projects():
    """Distinct working directories from all sessions, most recent first."""
    out = []
    for e in _list_all_sessions():
        c = e.get("cwd")
        if c and c not in out:
            out.append(c)
    return {"projects": out[:15]}


@app.get("/api/sessions/{session_id}/files")
def session_files(session_id: str):
    """Files Claude wrote/edited in this session (from tool_use records)."""
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    fpath = _scan_jsonl_files().get(session_id)
    if not fpath:
        raise HTTPException(status_code=404, detail="Session not found")
    seen = {}
    for m in _read_jsonl_tail(fpath, 4000):
        content = (m.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use" \
                    and c.get("name") in ("Write", "Edit", "NotebookEdit"):
                ti = c.get("input") or {}
                p = ti.get("file_path") or ti.get("notebook_path")
                if isinstance(p, str) and p:
                    seen[p] = c.get("name")
    out = []
    for p, tool in seen.items():
        try:
            pp = Path(p)
            if pp.is_file():
                st = pp.stat()
                out.append({"path": p, "name": pp.name, "size": st.st_size,
                            "mtime": st.st_mtime, "tool": tool})
        except OSError:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": out[:50], "cwd": _resolve_session_cwd(session_id)}


@app.get("/api/file/download")
def file_download(sessionId: str, path: str):
    """Download a file produced in the session (or one you uploaded).
    Only paths inside the session's working directory or the uploads
    directory are served."""
    if not sessionId or not SESSION_ID_RE.fullmatch(sessionId):
        raise HTTPException(status_code=400, detail="Invalid sessionId")
    if not _scan_jsonl_files().get(sessionId):
        raise HTTPException(status_code=404, detail="Session not found")
    cwd = _resolve_session_cwd(sessionId)
    allowed_roots = []
    for root in (cwd, str(UPLOAD_ROOT)):
        try:
            allowed_roots.append(str(Path(root).resolve()))
        except OSError:
            continue
    try:
        p = Path(path).resolve()
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    if not any(str(p).startswith(r + os.sep) or str(p) == r for r in allowed_roots):
        raise HTTPException(status_code=403, detail="Path is outside this session's scope")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if p.stat().st_size > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large to download (max 50MB)")
    content = p.read_bytes()
    media = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{p.name}"'},
    )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _send_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ── hooks ingestion routes ────────────────────────────────────────────

@app.post("/api/hook/session_start")
async def hook_session_start(request: Request):
    return await _ingest_hook("session_start", await _hook_payload(request))


@app.post("/api/hook/user_prompt_submit")
async def hook_user_prompt_submit(request: Request):
    return await _ingest_hook("user_prompt_submit", await _hook_payload(request))


@app.post("/api/hook/stop")
async def hook_stop(request: Request):
    return await _ingest_hook("stop", await _hook_payload(request))


@app.post("/api/hook/notification")
async def hook_notification(request: Request):
    return await _ingest_hook("notification", await _hook_payload(request))


@app.post("/api/hook/session_end")
async def hook_session_end(request: Request):
    return await _ingest_hook("session_end", await _hook_payload(request))


@app.post("/api/hook/permission_request")
async def hook_permission_request(request: Request):
    payload = await _hook_payload(request)
    return await _handle_permission_request(payload)


async def _hook_payload(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── permission decision API (browser side) ───────────────────────────

class PermissionDecision(BaseModel):
    decision: str  # "allow" | "deny"
    reason: str = ""
    updatedPermissions: list | None = None   # permission rules to remember ("don't ask again")""


@app.get("/api/permissions/pending")
def permissions_pending():
    return {"pending": [_permission_public(e["perm"]) for e in PENDING_PERMISSIONS.values()]}


@app.post("/api/permissions/{perm_id}/decide")
async def permissions_decide(perm_id: str, body: PermissionDecision):
    entry = PENDING_PERMISSIONS.get(perm_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Permission request not found (already resolved?)")
    if entry["result"] is not None:
        return {"ok": True, "note": "already decided"}
    allow = body.decision == "allow"
    result = {
        "behavior": "allow" if allow else "deny",
        "message": body.reason or ("Allowed from WebUI" if allow else "Denied from WebUI"),
    }
    if allow and body.updatedPermissions:
        result["updatedPermissions"] = body.updatedPermissions
    entry["result"] = result
    entry["event"].set()
    return {"ok": True, "decision": entry["result"]["behavior"]}


# ── WebUI config ──────────────────────────────────────────────────────

@app.get("/api/config")
def get_webui_config():
    return dict(CONFIG)


class ConfigUpdate(BaseModel):
    updates: dict


@app.post("/api/config")
def update_webui_config(update: ConfigUpdate):
    allowed = set(DEFAULT_CONFIG.keys())
    for k, v in update.updates.items():
        if k in allowed:
            CONFIG[k] = v
    _save_config()
    return {"ok": True, "config": dict(CONFIG)}


# ── Managed Claude Code process (PTY-based) ──────────────────────────

class ManagedClaude:
    """Spawns Claude Code via Windows PTY — full terminal interaction mirrored to Web UI."""

    def __init__(self):
        self.pty = None  # winpty.PtyProcess
        self.output_queue = queue.Queue()
        self.running = False
        self.session_id = None
        self._lock = threading.Lock()
        self._read_thread = None

    def _get_env(self) -> dict:
        env = os.environ.copy()
        settings = _read_settings()
        env.update(settings.get("env", {}))
        npm_bin = str(Path.home() / "AppData" / "Roaming" / "npm")
        if npm_bin not in env.get("PATH", ""):
            env["PATH"] = npm_bin + os.pathsep + env.get("PATH", "")
        return env

    def _spawn_args(self, session_id) -> list:
        """Build the CLI argv. session_id must match SESSION_ID_RE, so the
        argv stays flag-injection-free; no shell is involved."""
        exe = _get_claude_exe()
        if session_id:
            if not SESSION_ID_RE.fullmatch(str(session_id)):
                raise ValueError("Invalid session id")
            return [exe, "--resume", str(session_id)]
        return [exe, "--dangerously-skip-permissions"]

    def start(self, session_id=None, cwd=None):
        with self._lock:
            if self.running:
                self.stop()
            try:
                args = self._spawn_args(session_id)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            work_dir = cwd if _valid_cwd(cwd) else str(Path.home())
            env = self._get_env()

            try:
                from winpty import PtyProcess
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail="pywinpty is not installed — run: pip install pywinpty",
                )

            self.pty = PtyProcess.spawn(
                args,
                cwd=work_dir,
                env=env,
                dimensions=(120, 40),
            )
            self.running = True
            self.session_id = session_id
            self._read_thread = threading.Thread(target=self._pty_read_loop, daemon=True)
            self._read_thread.start()
            threading.Thread(target=self._broadcast_loop, daemon=True).start()

    def _pty_read_loop(self):
        trust_seen = 0
        try:
            while self.running and self.pty and self.pty.isalive():
                try:
                    data = self.pty.read(4096)
                    if data:
                        for line in data.splitlines():
                            if line.strip():
                                self.output_queue.put({"type": "stdout", "text": line, "ts": time.time()})
                        # The page-started session explicitly names its working
                        # directory, which is a trust statement from the user —
                        # auto-confirm Claude Code's workspace trust prompt.
                        # The TUI slices words with cursor-position codes, so
                        # match on the ANSI-stripped text.
                        clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", data)
                        clean = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", clean)
                        if "trust" in clean.lower() and "exit" in clean.lower() and trust_seen < 3:
                            trust_seen += 1
                            threading.Timer(0.8, self._confirm_trust).start()
                except Exception:
                    break
        except Exception:
            pass

    def _confirm_trust(self):
        """Move the selection to 'Yes, I trust this folder' and confirm."""
        try:
            if self.running and self.pty and self.pty.isalive():
                self.pty.write("\x1b[B")
                time.sleep(0.3)
                self.pty.write("\r")
                broadcast_threadsafe({
                    "type": "notification", "level": "green",
                    "message": "已自动确认工作区信任（目录由你在页面指定）",
                })
        except Exception:
            pass

    def _broadcast_loop(self):
        """Drain the output queue and push lines to WebSocket clients."""
        while self.running:
            try:
                msg = self.output_queue.get(timeout=0.3)
                text = msg.get("text", "")
                lower = text.lower()
                # heuristic highlighting for the raw CLI panel (managed mode only)
                if re.search(r'(\(y/n\)|\[y/n\]|yes/no|permission)', lower):
                    msg["promptType"] = "permission"
                broadcast_threadsafe(msg)
            except queue.Empty:
                pass
            except Exception:
                break

    def send_input(self, text: str):
        """Send text to process stdin."""
        try:
            if self.pty and self.running:
                self.pty.write(text + "\r\n")
        except (OSError, BrokenPipeError):
            pass

    def stop(self):
        with self._lock:
            self.running = False
            try:
                if self.pty:
                    self.pty.close()
                    self.pty = None
            except Exception:
                pass


managed = ManagedClaude()


# ── WebSocket ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    WS_CLIENTS.append(ws)
    try:
        await ws.send_json({
            "type": "hello",
            "activeSessionId": ACTIVE.get("sessionId"),
            "pendingPermissions": [_permission_public(e["perm"]) for e in PENDING_PERMISSIONS.values()],
            "browserApprovalEnabled": bool(CONFIG.get("browserApprovalEnabled", True)),
        })
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            action = msg.get("action")
            if action == "start":
                managed.start(session_id=msg.get("sessionId"), cwd=msg.get("cwd"))
                await ws.send_json({"type": "status", "text": "Claude Code started", "running": True})
            elif action == "stop":
                managed.stop()
                await ws.send_json({"type": "status", "text": "Claude Code stopped", "running": False})
            elif action == "input":
                managed.send_input(msg.get("text", ""))
            elif action == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            WS_CLIENTS.remove(ws)
        except ValueError:
            pass


@app.get("/api/managed/status")
def managed_status():
    return {"running": managed.running, "sessionId": managed.session_id, "clientCount": len(WS_CLIENTS)}


class ManagedAction(BaseModel):
    action: str  # start, stop, input, approve
    sessionId: str | None = None
    cwd: str | None = None
    text: str = ""
    response: str = "y"


@app.post("/api/managed/action")
def managed_action(body: ManagedAction):
    if body.action == "start":
        managed.start(session_id=body.sessionId, cwd=body.cwd)
    elif body.action == "stop":
        managed.stop()
    elif body.action == "input":
        managed.send_input(body.text)
    elif body.action == "approve":
        managed.send_input(body.response)
    return {"ok": True, "running": managed.running}


# ── static & spa fallback ─────────────────────────────────────────────


@app.get("/")
def index():
    # no-cache: the desktop app window must always pick up UI updates
    # immediately instead of serving a stale cached copy.
    return FileResponse(str(static_dir / "index.html"),
                        headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(CONFIG.get("port", 9020)), log_level="info")
