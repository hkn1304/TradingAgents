@echo off
title Stop TradingAgents
cd /d "%~dp0"

echo ============================================
echo  Stopping TradingAgents Server + ngrok
echo ============================================

:: Kill ngrok
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

:: Also close the named console windows if still open
taskkill /FI "WINDOWTITLE eq TradingAgents API" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq ngrok" /F >nul 2>&1

echo.
echo All done.
echo ============================================
timeout /t 2 /nobreak >nul
