@echo off
title TradingAgents Server
cd /d "%~dp0"

echo ============================================
echo  TradingAgents Server
echo ============================================

:: Kill any old server on port 8000
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING" 2^>nul') do taskkill /PID %%a /F >nul 2>&1

echo Starting API server on http://localhost:8000 ...
start "TradingAgents API" /min "C:\Users\HP\.local\bin\uv.exe" run python api_server.py

timeout /t 5 /nobreak > nul

:: Start ngrok tunnel (optional)
where ngrok >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo Starting ngrok tunnel...
    start "ngrok" /min ngrok http --url=postlike-johnathan-tushed.ngrok-free.dev 8000
    echo Public URL: https://postlike-johnathan-tushed.ngrok-free.dev
) else (
    echo ngrok not found in PATH -- skipping tunnel.
)

echo.
echo Server running at http://localhost:8000
echo Close the minimised "TradingAgents API" window to stop the server.
echo ============================================
pause