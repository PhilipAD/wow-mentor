@echo off
rem Double-click this file (after downloading the ZIP from GitHub and unzipping it) to install WoW Mentor on Windows.
rem It runs install.ps1 from this folder and installs a copy under %LOCALAPPDATA%\WoWMentor, so you can delete the
rem unzipped folder afterwards. No admin rights needed. Windows may show a blue "Windows protected your PC" box:
rem click "More info", then "Run anyway". Extra options for setup: set WOWMENTOR_SETUP_ARGS first (see install.ps1).
setlocal
set "WM_HERE=%~dp0"
set "WOWMENTOR_SRC=%WM_HERE%"
echo Installing WoW Mentor ...
echo.
rem The script is read and piped in (like the one-line installer), so the PowerShell execution policy does not apply.
powershell -NoProfile -Command "$p = Join-Path $env:WM_HERE 'install.ps1'; $env:WOWMENTOR_SRC = $env:WM_HERE.TrimEnd('\'); & ([scriptblock]::Create([IO.File]::ReadAllText($p))); exit $global:WoWMentorExitCode"
set "WM_CODE=%errorlevel%"
echo.
if not defined WOWMENTOR_NO_PAUSE pause
exit /b %WM_CODE%
