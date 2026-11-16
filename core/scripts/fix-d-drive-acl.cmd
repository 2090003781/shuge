@echo off
rem ============================================================================
rem  fix-d-drive-acl.cmd
rem
rem  Fix: DSH file tools (write/edit) fail on drive D with
rem       "SetFileSecurityW EACCES (Win32 5)".
rem
rem  Cause (measured 2026-09-15): DSH stages writes to a sibling temp dir and
rem  copies the original file's DACL onto the staged file
rem  (dsh-fs-local: copyFileDaclWin32 -> SetFileSecurityW). That needs WRITE_DAC
rem  on the file. D:\ grants only "Authenticated Users: Modify" (no WRITE_DAC),
rem  so overwriting an EXISTING file always fails while creating a NEW file works.
rem  Account FIREFLY\20900 is not an Administrator -> cannot change the DACL
rem  itself, so this must be run elevated once.
rem
rem  Safety: only ADDS one inheritable Allow FullControl ACE. Deletes nothing,
rem  changes no owner, does not recurse into existing children (inheritance
rem  handles them forward). Rollback:
rem      icacls D:\ /remove:g "FIREFLY\20900"
rem
rem  NOTE: this file is intentionally pure ASCII. A .cmd containing non-ASCII
rem  bytes is parsed as GBK by cmd.exe and the commands get mangled.
rem ============================================================================
setlocal
set "TARGET_USER=FIREFLY\20900"
set "LOG=%TEMP%\fix-d-drive-acl.log"
echo [%DATE% %TIME%] start > "%LOG%"

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [!] Not elevated. Requesting UAC elevation ^(click Yes^)...
  echo [%DATE% %TIME%] not elevated, re-launching with RunAs >> "%LOG%"
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo [%DATE% %TIME%] elevated OK >> "%LOG%"

echo.
echo   === BEFORE: permissions on D:\ ===
icacls D:\
icacls D:\ >> "%LOG%" 2>&1

echo.
echo   === Granting inheritable FullControl to %TARGET_USER% on D:\ ===
icacls D:\ /grant "%TARGET_USER%:(OI)(CI)F"
icacls D:\ /grant "%TARGET_USER%:(OI)(CI)F" >> "%LOG%" 2>&1

echo.
echo   === AFTER: permissions on D:\  ^(expect a %TARGET_USER% line^) ===
icacls D:\
icacls D:\ >> "%LOG%" 2>&1

echo.
if errorlevel 1 (
  echo   [FAIL] icacls returned an error. See %LOG%
) else (
  echo   [OK] ACE applied. Expect: "%TARGET_USER%:(OI)(CI)(F)" listed above.
)
echo   Log file: %LOG%
echo.
echo   Next: tell the agent "done" -- it will re-test DACL writes from its side.
echo.
echo   Rollback ^(elevated^): icacls D:\ /remove:g "%TARGET_USER%"
echo.
pause
