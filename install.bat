@echo off
setlocal
cd /d "%~dp0"
title Claude Code WebUI - Installer

echo ============================================
echo   Claude Code WebUI - 安装器
echo ============================================
echo.

REM ── 1. dependencies (only if missing) ────────────────
python -c "import fastapi, uvicorn, multipart, pydantic" >nul 2>&1
if errorlevel 1 (
  echo [1/4] 安装 Python 依赖...
  python -m pip install -q -r requirements.txt python-multipart pywinpty pillow
) else (
  echo [1/4] Python 依赖已就绪
)

REM ── 2. hooks into ~/.claude/settings.json ────────────
echo [2/4] 安装 Claude Code hooks（终端会话接入页面）...
python install_hooks.py >nul
echo       完成

REM ── 3. login autostart (server + app window) ─────────
echo [3/4] 注册开机自启（后台服务，无弹窗）...
python install_autostart.py >nul
echo       完成

REM ── 4. desktop + start menu shortcuts ────────────────
echo [4/4] 创建桌面 / 开始菜单快捷方式...
python install_app.py
echo.

echo ============================================
echo   安装完成！
echo   桌面双击 “Claude Code WebUI” 即可使用；
echo   终端里的 claude 照常用，页面自动同步。
echo ============================================
timeout /t 4 >nul
