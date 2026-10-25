@echo off
rem One-off matchday bookmaker probe (Windows Task Scheduler task "prop_alpha\Odds probe", maintainer 2026-10-21): which
rem OddsPapi bookmakers quote player tackles or fouls for League One, League Two and the English Championship, about an
rem hour before the 15:00 UTC kickoffs of Saturday 31 October (first run 24 Oct: late and cut short). The report goes to Telegram and, with every raw reply, to
rem .prop_alpha_state\odds_probe\<stamp>\; output is appended to .prop_alpha_state\odds_probe.log. Exits 1 if it failed.
rem The task deletes itself once expired (2026-11-01 14:00 UTC).
setlocal
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
set "LOG=.prop_alpha_state\odds_probe.log"
echo [%date% %time%] odds probe>> "%LOG%"
".venv\Scripts\python.exe" scripts\odds_probe.py --send >> "%LOG%" 2>&1 || (echo [%date% %time%] STEP FAILED: odds probe>> "%LOG%" & exit /b 1)
exit /b 0
