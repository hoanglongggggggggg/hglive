@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (py -3 hglive.py %* & goto :end)
where python >nul 2>nul || (echo Khong tim thay Python trong PATH. & pause & exit /b 1)
python hglive.py %*
:end
if errorlevel 1 pause
