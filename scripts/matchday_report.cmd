@echo off
rem One-off matchday check (task "prop_alpha\Matchday report", 2026-10-10 12:15 and 13:35 UTC): sends a Telegram report on
rem whether the scan, the prediction log and the T-60 team-sheet capture ran. Read-only; deletes its own task after the
rem 13:35 UTC run. Output is appended to .prop_alpha_state\matchday_report.log (gitignored).
setlocal
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
".venv\Scripts\python.exe" scripts\matchday_report.py >> ".prop_alpha_state\matchday_report.log" 2>&1
exit /b %ERRORLEVEL%
