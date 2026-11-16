@echo off
chcp 65001 >nul
cd /d D:\core-notes
python -B _unlock_and_verify.py
echo.
echo (ÍË³öÂë %ERRORLEVEL%)
pause