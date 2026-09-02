"""
Register the Claude Code WebUI server to start at Windows login.

Writes a HKCU Run entry ("ClaudeCodeWebUI") that launches the server with
pythonw (no console window) and the --open-browser flag, so the control page
is always available after boot - even before any claude session starts.

Usage:
  python install_autostart.py            # install / refresh
  python install_autostart.py --uninstall
"""
import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ClaudeCodeWebUI"
APP_DIR = Path(__file__).resolve().parent


def _command() -> str:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    use = pythonw if pythonw.is_file() else exe
    app = APP_DIR / "app.py"
    # --open-app: after startup, open the Edge/Chrome --app desktop window
    return f'"{use}" "{app}" --open-app'


def ensure_installed() -> bool:
    if os.name != "nt":
        return False
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, _command())
    return True


def uninstall() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
    except FileNotFoundError:
        pass
    return True


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
        print("autostart entry removed")
    else:
        ensure_installed()
        print("autostart registered:", _command())
