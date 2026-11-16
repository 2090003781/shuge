@echo off
chcp 65001 >nul
cd /d D:\core-notes
python -B _vault_verify.py
echo.
echo (ÍË³öÂë %ERRORLEVEL%)
pause