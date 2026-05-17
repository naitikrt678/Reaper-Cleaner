@echo off
echo ==========================================
echo REAPER Media Cleaner - EXE Build Script
echo ==========================================

echo.
echo Clearing PyInstaller cache...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q *.spec 2>nul

echo.
echo Installing dependencies...
pip install -r requirements.txt

echo.
echo Building executable with PyInstaller...
pyinstaller --noconfirm --clean --onefile --windowed --name "ReaperCleaner" reaper_cleaner.py

echo.
echo Build complete! The executable is located in the 'dist' directory.
pause
