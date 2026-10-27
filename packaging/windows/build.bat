@echo off
REM Build dist\FileBridge.exe with PyInstaller (run from a Windows machine with Python 3.8+)
cd /d "%~dp0\..\.."
py -m pip install --upgrade pyinstaller || goto :err
py -m PyInstaller --noconfirm --onefile --console --name FileBridge --icon assets\icon.ico --version-file packaging\windows\version_info.txt filebridge.py || goto :err
echo.
echo Built dist\FileBridge.exe
goto :eof
:err
echo Build failed.
exit /b 1
