@echo off
rem ============================================================================
rem  whisper-stack: start the containers and open the dictation UI.
rem
rem  Usage:  start.bat            start (images are built on first run)
rem          start.bat --build    rebuild images and recreate containers
rem                               (use after changing code or dependencies)
rem ============================================================================
setlocal EnableDelayedExpansion

set "APP_URL=http://localhost:8501"
set "BACKEND_HEALTH=http://localhost:8000/health"
set "FRONTEND_HEALTH=http://localhost:8501/_stcore/health"
rem First start downloads the model, so allow up to 10 minutes.
set "WAIT_SECONDS=600"

rem Run from the folder this script lives in, wherever it is launched from.
cd /d "%~dp0"

rem podman-compose is installed as a uv tool; make sure its folder is on PATH.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where podman >nul 2>&1 || (
    echo [ERROR] podman not found. Install Podman Desktop / Podman CLI first.
    goto :fail
)
where podman-compose >nul 2>&1 || (
    echo [ERROR] podman-compose not found. Install it with:
    echo         uv tool install podman-compose
    goto :fail
)

echo [1/4] Checking Podman machine...
podman info >nul 2>&1
if errorlevel 1 (
    echo       Starting Podman machine...
    podman machine start
    if errorlevel 1 (
        echo [ERROR] Could not start the Podman machine.
        goto :fail
    )
)

echo [2/4] Starting containers...
if /i "%~1"=="--build" (
    podman-compose up -d --build --force-recreate
) else (
    podman-compose up -d
)
if errorlevel 1 (
    echo [ERROR] podman-compose up failed. See the output above.
    goto :fail
)

echo [3/4] Waiting for backend and frontend to become healthy...
set /a elapsed=0
:wait_loop
curl.exe -sf "%BACKEND_HEALTH%" >nul 2>&1 && curl.exe -sf "%FRONTEND_HEALTH%" >nul 2>&1 && goto :ready
if !elapsed! geq %WAIT_SECONDS% (
    echo [ERROR] Services not healthy after %WAIT_SECONDS%s. Check the logs:
    echo         podman-compose logs --tail 50 backend
    goto :fail
)
set /a mod=elapsed %% 30
if !elapsed! gtr 0 if !mod! equ 0 echo       still waiting ^(!elapsed!s^) - the first start downloads the model...
rem ping is used as a sleep that also works when stdin is redirected.
ping -n 3 127.0.0.1 >nul
set /a elapsed+=2
goto :wait_loop

:ready
echo [4/4] Opening %APP_URL% in your default browser...
start "" "%APP_URL%"

echo.
echo whisper-stack is running.
echo   UI:      %APP_URL%
echo   Backend: http://localhost:8000  (API docs: http://localhost:8000/docs)
echo   Stop:    stop.bat
endlocal
exit /b 0

:fail
echo.
pause
endlocal
exit /b 1
