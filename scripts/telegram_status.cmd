@echo off
rem Sends the prop_alpha status report to Telegram. Run every 4 hours by the Windows Task Scheduler task
rem "prop_alpha\Telegram status". Output is appended to .prop_alpha_state\status.log (gitignored).
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
".venv\Scripts\python.exe" -m prop_alpha.alerts.status --send >> ".prop_alpha_state\status.log" 2>&1
