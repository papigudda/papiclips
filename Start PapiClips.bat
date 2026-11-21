@echo off
setlocal
cd /d "%~dp0"
title PapiClips - AI Stream Clipper ^& Viral Shorts Generator
color 06

if not exist ".venv\Scripts\python.exe" (
    echo ================================================================
    echo  PapiClips is not installed yet.
    echo  Running automatic installer first...
    echo ================================================================
    echo.
    call "%~dp0install.bat"
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [ERROR] PapiClips installation could not be completed.
    echo Please review the error messages above.
    pause
    exit /b 1
)

cls
echo ================================================================
echo           PapiClips - AI Stream Clipper ^& Viral Shorts
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
start "" cmd /c "ping -n 3 127.0.0.1 >nul && start http://127.0.0.1:8765/app"

:: Start the Python studio server
".venv\Scripts\python.exe" -m studio.server

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] PapiClips server stopped with exit code %errorlevel%.
)

pause
