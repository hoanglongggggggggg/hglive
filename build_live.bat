@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Build HGLIVE.exe (onefile) ===
python -m PyInstaller --noconfirm --clean --onefile --windowed --name HGLIVE ^
  --exclude-module tkinter --exclude-module unittest --exclude-module pydoc_data ^
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtQuick --exclude-module PySide6.QtQml --exclude-module PySide6.Qt3DCore ^
  --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtCharts ^
  --exclude-module PySide6.QtDataVisualization --exclude-module PySide6.QtPdf ^
  --exclude-module PySide6.QtDesigner ^
  --exclude-module numpy --exclude-module scipy --exclude-module pandas ^
  --exclude-module gmssl ^
  hglive_app.py
if errorlevel 1 (echo BUILD LOI & pause & exit /b 1)
rmdir /s /q build 2>nul
echo.
echo Xong: dist\HGLIVE.exe
pause
