@echo off
setlocal EnableExtensions
rem ---------------------------------------------------------------------------
rem run_hub.cmd - start the CabiNet hub on this Windows PC. LITE MODE ONLY.
rem
rem   deploy\windows\run_hub.cmd            (double-click, or from any prompt)
rem   deploy\windows\run_hub.cmd --no-seed  (extra flags pass straight through)
rem
rem Reads deploy\windows\hub.env (copy hub.env.example) for:
rem   HUB_IP=<this PC's LAN address>   required - the router-reserved address
rem   CABINET_PYTHON=<path\python.exe> optional - a specific Python 3.11+
rem
rem Why lite only: the DHCP/DNS/NTP/TFTP services that make the full-mode
rem 192.168.50.2 address work are Linux-only. On Windows your router hands out
rem addresses and DHCP option 43 points the machines here. Full story, firewall
rem rule and router recipe: deploy\WINDOWS_HUB.md
rem ---------------------------------------------------------------------------
set "HERE=%~dp0"
for %%I in ("%HERE%..\..") do set "ROOT=%%~fI"

if exist "%HERE%hub.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%HERE%hub.env") do set "%%A=%%B"
)
if not defined HUB_IP (
    echo run_hub: HUB_IP is not set.
    echo   copy deploy\windows\hub.env.example to deploy\windows\hub.env and put
    echo   this PC's LAN address in it ^(the one you reserved on your router^).
    exit /b 2
)

rem --- pick a Python 3.11+ ------------------------------------------------------
set "PY="
if defined CABINET_PYTHON if exist "%CABINET_PYTHON%" set "PY=%CABINET_PYTHON%"
if not defined PY (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo run_hub: no Python 3.11+ found. Install it from python.org ^(tick "py launcher"^)
    echo   or set CABINET_PYTHON=^<full path to python.exe^> in deploy\windows\hub.env.
    exit /b 2
)
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 || (
    echo run_hub: %PY% is older than 3.11 - set CABINET_PYTHON in hub.env.
    exit /b 2
)

rem the hub logs emoji; a cp1252 console would otherwise crash the logger
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo CabiNet hub ^(lite^) on http://%HUB_IP%:8081  -  Python: %PY%
echo   first time only, in an ADMIN prompt, so the machines can reach it:
echo   netsh advfirewall firewall add rule name="CabiNet hub" dir=in action=allow protocol=TCP localport=8081
echo   stop with Ctrl+C.
echo.
cd /d "%ROOT%\G2S"
%PY% -u g2s_host.py --harvest --host-base http://%HUB_IP%:8081 --log-dir logs %*
