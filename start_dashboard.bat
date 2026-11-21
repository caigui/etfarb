@echo off
setlocal EnableExtensions
chcp 936 >nul
title ETFarb Launcher (8102)

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "BACKEND=%ROOT%\backend"

echo ========================================
echo   ETFarb ETF 套利看板
echo   端口 8102 · 后端托管静态页 + 快照预热
echo ========================================
echo.

REM ---------- [1/4] Python 解释器：项目 .venv 优先，否则用 PATH 里的 python ----------
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM ---------- [2/4] 清理环境变量（照抄 lofarb-master/start_dashboard.bat 的踩坑经验） ----------
REM PYTHONPATH：防止继承 QMT / WorkBuddy 等外部路径，导致 import 串味或旧版 defusedxml 覆盖 venv
set "PYTHONPATH="
REM 代理：本机 VPN 若设了 HTTP(S)_PROXY，requests 会被劫持，行情抓取全部 ProxyError 失败
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
set "ALL_PROXY="
set "all_proxy="

REM ---------- [2.5/4] 钉住共享主库路径（必须！已实测：不设直接 FileNotFoundError 起不来） ----------
REM 原因一：本项目目录叫 lofarb-master，而 core/db.py 的兄弟目录回退只认 "LOFarb" -> 匹配不到
REM 原因二：祖先目录扫描 database/arb_master.db 时，可能先命中无关的空库
REM 故显式指向 LOFarb 的真实主库（相对路径，换机器 clone 布局不变即可用）
set "ARB_MASTER_DB=%ROOT%\..\lofarb-master\database\arb_master.db"
if not exist "%ARB_MASTER_DB%" (
  echo [WARN] 未找到共享主库: %ARB_MASTER_DB%
  echo [WARN] 请确认 LOFarb 目录名，或手动设置 ARB_MASTER_DB 为真实路径
  set "ARB_MASTER_DB="
) else (
  echo [信息] 共享主库: %ARB_MASTER_DB%
)

REM ---------- [3/4] 释放端口 8102：上次没关干净的残留进程先强杀 ----------
REM 用 Get-NetTCPConnection 取 OwningProcess 比 netstat 解析稳，且不会误杀无关进程
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8102 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "for($i=0;$i -lt 5;$i++){ if(-not (Get-NetTCPConnection -LocalPort 8102 -State Listen -ErrorAction SilentlyContinue)){ break }; Start-Sleep -Seconds 1 }"

REM ---------- [4/4] 启动后端（前端 index.html 由后端同端口托管，无需 npm） ----------
REM 端口可作第一个参数传入，不传默认 8102。首次启动会预热快照，前 1~3 分钟数据可能不全。
echo [启动] 后端 %BACKEND%\app.py 8102
start "ETFarb Backend" cmd /k "cd /d %BACKEND% && %PY% app.py 8102"

echo 等待 3 秒后打开浏览器...
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8102/"

echo.
echo ========================================
echo   已启动: http://127.0.0.1:8102/
echo   停止：在 "ETFarb Backend" 窗口按 Ctrl+C
echo ========================================
timeout /t 3 /nobreak >nul
