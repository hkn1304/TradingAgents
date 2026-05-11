@echo off
title XAGUSD Analyser Server
cd /d "%~dp0"

echo ============================================
echo  XAGUSD Silver Analyser — Local Server
echo ============================================
echo.

:: Check Python is available
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: Python not found in PATH.
    echo Please install Python from https://python.org
    pause
    exit /b 1
)

echo [1/3] Installing / updating dependencies...
echo       (this may take 1-2 minutes on first run)
echo.
pip install fastapi "uvicorn[standard]" pandas numpy requests apscheduler python-dotenv pywebpush
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: pip install failed. Check your internet connection.
    pause
    exit /b 1
)

echo.
echo [2/3] Starting FastAPI server on port 8000...
echo       PC browser: http://localhost:8000
echo.

:: Start ngrok with fixed static domain
where ngrok >nul 2>&1
if %ERRORLEVEL%==0 (
    echo [3/3] Starting ngrok tunnel with static domain...
    start "ngrok tunnel" cmd /k "timeout /t 3 /nobreak >nul && ngrok http 8000 --domain=postlike-johnathan-tushed.ngrok-free.dev"
) else (
    echo [3/3] ngrok not found.
    echo       Install from https://ngrok.com/download
)

echo.
echo ============================================
echo  Server starting... (Ctrl+C to stop)
echo.
echo  PC:     http://localhost:8000
echo  Mobile: https://postlike-johnathan-tushed.ngrok-free.dev
echo ============================================
echo.

python -m uvicorn api_server:app --host 0.0.0.0 --port 8000

pause
