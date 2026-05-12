@echo off
:: Registers TradingAgents to start automatically at Windows login (no admin needed).
:: Run this file ONCE. To uninstall: schtasks /delete /tn "TradingAgentsServer" /f

set TASK_NAME=TradingAgentsServer
set UV_EXE=C:\Users\HP\.local\bin\uv.exe
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%

echo Registering startup task "%TASK_NAME%"...

schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

:: Write a Task XML with WorkingDirectory set correctly
set XML_PATH=%TEMP%\ta_task.xml
(
echo ^<?xml version="1.0" encoding="UTF-16"?^>
echo ^<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"^>
echo   ^<Triggers^>
echo     ^<LogonTrigger^>^<Enabled^>true^</Enabled^>^</LogonTrigger^>
echo   ^</Triggers^>
echo   ^<Principals^>
echo     ^<Principal id="Author"^>^<LogonType^>InteractiveToken^</LogonType^>^<RunLevel^>LeastPrivilege^</RunLevel^>^</Principal^>
echo   ^</Principals^>
echo   ^<Settings^>
echo     ^<MultipleInstancesPolicy^>IgnoreNew^</MultipleInstancesPolicy^>
echo     ^<DisallowStartIfOnBatteries^>false^</DisallowStartIfOnBatteries^>
echo     ^<StopIfGoingOnBatteries^>false^</StopIfGoingOnBatteries^>
echo     ^<Hidden^>true^</Hidden^>
echo     ^<ExecutionTimeLimit^>PT0S^</ExecutionTimeLimit^>
echo   ^</Settings^>
echo   ^<Actions^>
echo     ^<Exec^>
echo       ^<Command^>%UV_EXE%^</Command^>
echo       ^<Arguments^>run python api_server.py^</Arguments^>
echo       ^<WorkingDirectory^>%SCRIPT_DIR%^</WorkingDirectory^>
echo     ^</Exec^>
echo   ^</Actions^>
echo ^</Task^>
) > "%XML_PATH%"

:: Import the task
schtasks /create /tn "%TASK_NAME%" /xml "%XML_PATH%" /f
del "%XML_PATH%" >nul 2>&1

if %ERRORLEVEL% EQU 0 (
    echo.
    echo SUCCESS: TradingAgents will auto-start at next login ^(background, no window^).
    echo Task:    %TASK_NAME%
    echo Uninstall: schtasks /delete /tn "%TASK_NAME%" /f
) else (
    echo.
    echo ERROR: Task creation failed. Try running as Administrator.
)
echo.
pause