@echo off
title TradingAgents Server
cd /d "%~dp0"

:: ── Tunnel selection ──────────────────────────────────────────────────────────
:: Usage:  start_server.bat [cloudflare|ngrok]
:: Default: cloudflare  (quick tunnel — no account needed, random URL)
::          ngrok       — uses ngrok static domain (requires paid plan)
set TUNNEL=%1
if /i "%TUNNEL%"=="" set TUNNEL=cloudflare
if /i "%TUNNEL%"=="cf"  set TUNNEL=cloudflare

echo ============================================
echo  TradingAgents Server  [tunnel: %TUNNEL%]
echo ============================================

:: Kill any existing server on port 8000
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING" 2^>nul') do (
    taskkill /PID %%a /F >nul 2>&1
)

:: Kill any existing tunnel processes
taskkill /IM cloudflared.exe /F >nul 2>&1
taskkill /IM ngrok.exe       /F >nul 2>&1

echo Starting API server on http://localhost:8000 ...
start "TradingAgents API" /min "C:\Users\HP\.local\bin\uv.exe" run python api_server.py

:: Give the server a moment to bind
timeout /t 5 /nobreak >nul

:: ── Cloudflare quick tunnel ───────────────────────────────────────────────────
if /i "%TUNNEL%"=="cloudflare" (
    where cloudflared >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo.
        echo  [!] cloudflared not found.
        echo      Install with:  winget install Cloudflare.cloudflared
        echo      Then re-run this script.
        echo.
        goto :end
    )
    echo Starting Cloudflare quick tunnel...
    echo  ^(URL will appear in the "cloudflared" window — look for trycloudflare.com^)
    start "cloudflared" /min cloudflared tunnel --url http://localhost:8000
    echo.
    echo  Local  : http://localhost:8000
    echo  Public : see the "cloudflared" console window for the *.trycloudflare.com URL
    goto :end
)

:: ── ngrok tunnel ─────────────────────────────────────────────────────────────
if /i "%TUNNEL%"=="ngrok" (
    where ngrok >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo.
        echo  [!] ngrok not found in PATH.
        echo.
        goto :end
    )
    echo Starting ngrok tunnel...
    start "ngrok" /min ngrok start trading
    echo.
    echo  Local  : http://localhost:8000
    echo  Public : https://postlike-johnathan-tushed.ngrok-free.app  ^(if plan allows^)
    goto :end
)

echo  [!] Unknown tunnel: %TUNNEL%  -- valid values are: cloudflare  ngrok

:end
echo.
echo Both windows are minimised. Run stop_server.bat to shut everything down.
echo ============================================
pause
