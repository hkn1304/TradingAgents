@echo off
title Stop TradingAgents
cd /d "%~dp0"

echo ============================================
echo  Stopping TradingAgents Server + Tunnels
echo ============================================

:: Kill Cloudflare tunnel
taskkill /IM cloudflared.exe /F >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [OK] cloudflared stopped.
) else (
    echo [--] cloudflared was not running.
)

:: Kill ngrok (in case it was used instead)
taskkill /IM ngrok.exe /F >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [OK] ngrok stopped.
) else (
    echo [--] ngrok was not running.
)

:: Kill server on port 8000
set KILLED=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING" 2^>nul') do (
    taskkill /PID %%a /F >nul 2>&1
    set KILLED=1
)
if "%KILLED%"=="1" (
    echo [OK] API server stopped.
) else (
    echo [--] API server was not running.
)

:: Close named console windows
taskkill /FI "WINDOWTITLE eq TradingAgents API" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq cloudflared"        /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq ngrok"              /F >nul 2>&1

echo.
echo All done.
echo ============================================
timeout /t 2 /nobreak >nul
