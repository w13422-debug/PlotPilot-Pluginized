@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

set "PLOTPILOT_MODE=launch"
if not "%~2"=="" goto :usage
if "%~1"=="" goto :run
if /I "%~1"=="--check" set "PLOTPILOT_MODE=check"& goto :run
if /I "%~1"=="--self-test-owned-cleanup" set "PLOTPILOT_MODE=self-test-cleanup"& goto :run
if /I "%~1"=="--self-test-owned-cleanup-mismatch" set "PLOTPILOT_MODE=self-test-mismatch"& goto :run
if /I "%~1"=="--self-test-owned-cleanup-exited" set "PLOTPILOT_MODE=self-test-exited"& goto :run
if /I "%~1"=="--self-test-owned-capture-failure" set "PLOTPILOT_MODE=self-test-capture-failure"& goto :run
goto :usage

:run
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-webui.ps1" -Mode "%PLOTPILOT_MODE%"
set "PLOTPILOT_EXIT=%ERRORLEVEL%"
if not "%PLOTPILOT_EXIT%"=="0" if /I "%PLOTPILOT_MODE%"=="launch" pause
exit /b %PLOTPILOT_EXIT%

:usage
echo Usage: start-webui.cmd [--check^|--self-test-owned-cleanup^|--self-test-owned-cleanup-mismatch^|--self-test-owned-cleanup-exited^|--self-test-owned-capture-failure]
exit /b 2
