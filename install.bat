@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title PapiClips - Automated Setup ^& Installer
color 0A

echo ================================================================
echo        PapiClips - Automated Installer for Windows
echo        Created by @papig.udda (instagram.com/papig.udda)
echo ================================================================
echo.

:: 1. Check Python
echo [1/6] Checking Python installation...
set "PYTHON_EXE="

:: Prefer Python 3.12, 3.11, or 3.10 via py launcher
py -3.12 --version >nul 2>&1
if !errorlevel! equ 0 (
    set "PYTHON_EXE=py -3.12"
    goto :python_ready
)
py -3.11 --version >nul 2>&1
if !errorlevel! equ 0 (
    set "PYTHON_EXE=py -3.11"
    goto :python_ready
)
py -3.10 --version >nul 2>&1
if !errorlevel! equ 0 (
    set "PYTHON_EXE=py -3.10"
    goto :python_ready
)

:: Check standard Windows user install path for Python 3.12
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :python_ready
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto :python_ready
)
if exist "C:\Python312\python.exe" (
    set "PYTHON_EXE=C:\Python312\python.exe"
    goto :python_ready
)

:: Check system python on PATH (ensure not 3.14+)
python -c "import sys; exit(0 if (3, 10) <= sys.version_info < (3, 14) else 1)" >nul 2>&1
if !errorlevel! equ 0 (
    set "PYTHON_EXE=python"
    goto :python_ready
)

where python >nul 2>&1
if !errorlevel! equ 0 (
    echo [WARNING] Default python might be Python 3.14+ which lacks pre-built wheels for AI packages.
    set "PYTHON_EXE=python"
    goto :python_ready
)

:python_not_found
color 0C
echo [ERROR] A compatible Python version (3.10 to 3.12) was not found!
echo PapiClips requires Python 3.10, 3.11, or 3.12.
echo.
echo Please install Python 3.12 from python.org:
echo https://www.python.org/downloads/release/python-31210/
echo IMPORTANT: Check the box "Add python.exe to PATH" during installation.
echo.
start https://www.python.org/downloads/release/python-31210/
pause
exit /b 1

:python_ready
for /f "delims=" %%v in ('!PYTHON_EXE! --version 2^>^&1') do set "PY_VER=%%v"
echo [OK] Using Python: !PY_VER!
echo.

:: 2. Check FFmpeg
echo [2/6] Checking FFmpeg installation...
where ffmpeg >nul 2>&1
if !errorlevel! neq 0 (
    echo [NOTICE] FFmpeg was not detected in PATH.
    echo Attempting to install FFmpeg automatically via Windows Package Manager winget...
    winget install Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    if !errorlevel! neq 0 (
        echo [WARNING] Automatic FFmpeg installation skipped. If rendering fails,
        echo please install ffmpeg from https://ffmpeg.org/download.html and add it to PATH.
    ) else (
        echo [OK] FFmpeg installed successfully via winget!
    )
) else (
    echo [OK] FFmpeg is already installed.
)
echo.

:: 3. Setup Virtual Environment
echo [3/6] Setting up Python virtual environment (.venv)...
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment with !PYTHON_EXE!...
    !PYTHON_EXE! -m venv .venv
    if !errorlevel! neq 0 (
        color 0C
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
) else (
    echo [OK] Virtual environment already exists.
)
echo.

:: 4. Install Dependencies
echo [4/6] Installing required Python libraries...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if !errorlevel! neq 0 (
    color 0C
    echo [ERROR] Failed to install requirements. Please check your internet connection.
    pause
    exit /b 1
)
echo [OK] All dependencies installed successfully.
echo.

:: 5. Setup Configuration
echo [5/6] Checking configuration (.env)...
if not exist ".env" (
    echo Creating .env file from .env.example template...
    copy .env.example .env >nul
    echo [OK] Created fresh .env configuration file.
) else (
    echo [OK] .env configuration already present.
)
echo.

:: 6. Pre-fetch Speech Model
echo [6/6] Downloading high-precision speech model (English)...
echo (This takes a moment on first install and ensures offline speech recognition)
".venv\Scripts\python.exe" -m studio.fetch_model en
echo.

echo ================================================================
echo                 PapiClips Setup is Complete!
echo ================================================================
echo.
echo You can now launch PapiClips anytime by double-clicking:
echo   --^> "Start PapiClips.bat"
echo.
echo The app will automatically launch in your web browser!
echo.
pause
