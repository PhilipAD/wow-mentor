@echo off
rem Runs the wowui command from this folder. Works from an unzipped download or a git clone, no install needed.
rem Uses "py -3" first (it skips the Microsoft Store stub), then "python". Set WOWMENTOR_PYTHON to force one.
setlocal
set "WM_FILE=%~dp0wowui"
if defined WOWMENTOR_PYTHON goto custom
py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto trypython
py -3 -X utf8 "%WM_FILE%" %*
exit /b %errorlevel%
:trypython
python -X utf8 "%WM_FILE%" %*
exit /b %errorlevel%
:custom
"%WOWMENTOR_PYTHON%" -X utf8 "%WM_FILE%" %*
exit /b %errorlevel%
