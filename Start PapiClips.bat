@echo off
setlocal
title PapiClips - AI Stream Clipper & Viral Shorts Generator
color 06

if not exist ".venv\Scripts\python.exe" (
    echo [NOTICE] PapiClips is not installed yet.
    echo Running installer first...
    echo.
    call install.bat
    if %errorlevel% neq 0 (
        echo [ERROR] Installation did not finish.
        pause
        exit /b 1
    )
)

cls
echo ================================================================
echo           PapiClips - AI Stream Clipper & Viral Shorts
echo           Created by @papig.udda (instagram.com/papig.udda)
echo ================================================================
echo.
echo  * Starting PapiClips server on http://127.0.0.1:8765/app ...
echo  * Opening PapiClips in your default web browser ...
echo.
echo  KEEP THIS WINDOW OPEN while using PapiClips.
echo  To shut down, simply close this window or press Ctrl+C.
echo ================================================================
echo.

:: Launch browser in background after 2 seconds
start /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8765/app"

:: Start the Python studio server
.venv\Scripts\python.exe -m studio.server
pause
