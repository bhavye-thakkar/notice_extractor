@echo off
rem ===================================================================
rem  Public Notice Extractor - double-click this file to start.
rem
rem  Deliberately a .bat and not the .py: double-clicking a .py needs a
rem  working file association, and when anything goes wrong the console
rem  closes before you can read the error.  This finds Python itself and
rem  always stops so you can see what happened.
rem ===================================================================
title Public Notice Extractor
cd /d "%~dp0.."

rem -- find a Python -------------------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (
    echo.
    echo   Python is not installed, or not on PATH.
    echo.
    echo   Install Python 3.10 or newer from https://www.python.org/downloads/
    echo   and TICK "Add python.exe to PATH" on the first screen.
    echo.
    pause
    exit /b 2
)

rem -- first run? install the packages -------------------------------
%PY% -c "import cv2, numpy, PIL, pymupdf" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   First run - installing the Python packages.  This takes a few minutes.
    echo.
    %PY% -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo.
        echo   The install failed.  Check the messages above.
        pause
        exit /b 1
    )
)

echo Starting Public Notice Extractor...
%PY% -m notice_extractor.main
if errorlevel 1 (
    echo.
    echo   It stopped with an error.  For a full setup check, run:
    echo       %PY% -m notice_extractor.main --doctor
    echo.
    pause
)
