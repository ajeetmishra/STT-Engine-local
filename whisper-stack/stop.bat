@echo off
rem ============================================================================
rem  whisper-stack: stop and remove the containers (the model cache is kept).
rem
rem  Usage:  stop.bat              podman-compose down
rem          stop.bat --machine    also stop the Podman machine (frees its RAM)
rem ============================================================================
setlocal

cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where podman-compose >nul 2>&1 || (
    echo [ERROR] podman-compose not found. Install it with:
    echo         uv tool install podman-compose
    goto :fail
)

podman info >nul 2>&1
if errorlevel 1 (
    echo Podman machine is not running - nothing to stop.
    goto :done
)

echo Stopping containers...
podman-compose down
if errorlevel 1 (
    echo [ERROR] podman-compose down failed. See the output above.
    goto :fail
)

if /i "%~1"=="--machine" (
    echo Stopping Podman machine...
    podman machine stop
)

:done
echo.
echo whisper-stack stopped. The model cache volume was kept; start.bat brings it back.
endlocal
exit /b 0

:fail
echo.
pause
endlocal
exit /b 1
