@echo off
REM Launch the supervisor detached and hidden, so it survives the terminal that
REM started it. Safe to run repeatedly and safe to run at logon: the supervisor
REM takes a lock and exits immediately if one is already running, and exits
REM after building the index once the library is complete.
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0supervise.ps1"
