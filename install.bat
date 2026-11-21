@echo off
setlocal enabledelayedexpansion
title PapiClips - Automated Setup & Installer
color 0A

echo ================================================================
echo        PapiClips - Automated Installer for Windows
echo        Created by @papig.udda (instagram.com/papig.udda)
echo ================================================================
echo.

:: 1. Check Python
echo [1/6] Checking Python installation...
where python >nul 2>nul
if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Python is not installed or not in your system PATH!
    echo Please install Python 3.10 or newer from python.org.
    echo IMPORTANT: Make sure to CHECK "Add python.exe to PATH" during installation.
    echo.
    echo Opening python.org download page...
    start https://www.python.org/downloads/
    pause
    exit /b 1
)

python --version
echo [OK] Python is installed.
echo.

:: 2. Check FFmpeg
echo [2/6] Checking FFmpeg installation...
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo [NOTICE] FFmpeg was not detected in PATH.
    echo Attempting to install FFmpeg automatically via Windows Package Manager (winget)...
    winget install Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    if %errorlevel% neq 0 (
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
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
    if %errorlevel% neq 0 (
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
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
if %errorlevel% neq 0 (
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
python -m studio.fetch_model en
echo.

echo ================================================================
echo                 PapiClips Setup is Complete!
echo ================================================================
echo.
echo You can now launch PapiClips anytime by double-clicking:
echo   --> "Start PapiClips.bat"
echo.
echo The app will automatically launch in your web browser!
echo.
pause
