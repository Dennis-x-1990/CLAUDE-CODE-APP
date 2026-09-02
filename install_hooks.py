"""
Install (or uninstall) Claude Code WebUI hooks into ~/.claude/settings.json.

The hooks wire the user's own `claude` terminal sessions into the WebUI:
  - SessionStart      -> WebUI auto-starts (server + browser) and highlights the session
  - UserPromptSubmit  -> instant push of the user's message to the browser
  - Stop              -> instant refresh when Claude finishes a reply
  - Notification      -> idle / waiting toasts
  - PermissionRequest -> tool approval delegated to the browser (allow/deny)
  - SessionEnd        -> clears the active-session indicator

Usage:
  python install_hooks.py            # install / refresh
  python install_hooks.py uninstall  # remove WebUI hooks

Only entries that reference this project's hook_client.py are touched; all
other user hooks are preserved. A one-time backup of settings.json is made
before the first modification.
"""
import json
import shutil
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
HOOK_SCRIPT = APP_DIR / "hook_client.py"
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
BACKUP_FILE = SETTINGS_FILE.with_name("settings.json.pre-webui-hooks")

# event name in settings.json -> (hook_client arg, async, timeout seconds)
HOOK_EVENTS = {
    "SessionStart": ("session_start", True, 30),
    "UserPromptSubmit": ("user_prompt_submit", True, 15),
    "Stop": ("stop", True, 15),
    "Notification": ("notification", True, 15),
    "SessionEnd": ("session_end", True, 15),
    "PermissionRequest": ("permission_request", False, 150),
}


def _is_ours(hook: dict) -> bool:
    """Identify WebUI-owned hook entries so reinstall/uninstall only touches those."""
    marker = str(HOOK_SCRIPT)
    return marker in str(hook.get("command", "")) or marker in " ".join(
        str(a) for a in hook.get("args", [])
    )


def _read_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _write_settings(data: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not BACKUP_FILE.exists():
        shutil.copy2(SETTINGS_FILE, BACKUP_FILE)
    SETTINGS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _build_entry(python_exe: str, event_arg: str, is_async: bool, timeout: int) -> dict:
    entry = {
        "type": "command",
        "command": python_exe,
        "args": [str(HOOK_SCRIPT), event_arg],
        "timeout": timeout,
    }
    if is_async:
        entry["async"] = True
    return entry


def ensure_installed(python_exe: str | None = None) -> bool:
    """Install/refresh WebUI hooks. Idempotent; preserves unrelated hooks."""
    if not HOOK_SCRIPT.exists():
        return False
    python_exe = python_exe or sys.executable
    data = _read_settings()
    hooks = data.setdefault("hooks", {})

    for event_name, (event_arg, is_async, timeout) in HOOK_EVENTS.items():
        groups = hooks.setdefault(event_name, [])
        # drop previous WebUI entries
        for group in groups:
            group["hooks"] = [h for h in group.get("hooks", []) if not _is_ours(h)]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append({"hooks": [_build_entry(python_exe, event_arg, is_async, timeout)]})

    _write_settings(data)
    return True


def uninstall() -> bool:
    data = _read_settings()
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for event_name in list(hooks.keys()):
        groups = hooks[event_name]
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                group["hooks"] = [h for h in group["hooks"] if not _is_ours(h)]
        hooks[event_name] = [g for g in groups if isinstance(g, dict) and g.get("hooks")]
        if not hooks[event_name]:
            del hooks[event_name]
    _write_settings(data)
    return True


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
        print("WebUI hooks removed from", SETTINGS_FILE)
    else:
        ensure_installed()
        print("WebUI hooks installed into", SETTINGS_FILE)
        print("backup:", BACKUP_FILE)
