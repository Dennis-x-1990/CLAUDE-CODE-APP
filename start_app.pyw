"""Silent launcher for the Claude Code WebUI desktop app.

Runs under pythonw (no console window ever). Boots the server if it is not
already up, then opens the control page as an Edge/Chrome --app window.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PORT = 9020

BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def server_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def start_server() -> None:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    use = str(pythonw if pythonw.is_file() else exe)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [use, str(APP_DIR / "app.py")],
            cwd=str(APP_DIR),
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def open_window() -> None:
    url = f"http://127.0.0.1:{PORT}/"
    for exe in BROWSERS:
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, "--app=" + url])
                return
            except Exception:
                continue
    try:
        os.startfile(url)
    except Exception:
        pass


def main() -> None:
    if not server_up():
        start_server()
        deadline = time.time() + 25
        while time.time() < deadline and not server_up():
            time.sleep(0.3)
    open_window()


if __name__ == "__main__":
    main()
