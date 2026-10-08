@echo off
rem Every 10 minutes (Windows Task Scheduler task "prop_alpha\Bet ledger poll"): record bets from Telegram replies and
rem answer each one, capture closing prices for kickoffs with open bets or alerts, record the T-60 team sheets, then settle open bets whose match
rem is final in the database (the 4-hourly task ingests results first). It is the only poller (two pollers at once
rem could record a reply twice) and the only writer of placed_bets.jsonl. Output is appended to
rem .prop_alpha_state\ledger.log (gitignored). Every step runs; the script exits 1 if any failed (audit 2026-10-05).
rem Last, the run is reported to prop_alpha.alerts.task_health with the names of the failed steps, which sends a Telegram
rem alert after 2 failing runs in a row and flags another task that has stopped running.
setlocal
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
set "LOG=.prop_alpha_state\ledger.log"
set "PY=.venv\Scripts\python.exe"
set RC=0
set "FAILED="
"%PY%" -m prop_alpha.alerts.ledger --poll >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% poll"& echo [%date% %time%] STEP FAILED: poll>> "%LOG%")
"%PY%" -m prop_alpha.alerts.closing >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% closing-prices"& echo [%date% %time%] STEP FAILED: closing prices>> "%LOG%")
"%PY%" -m prop_alpha.alerts.lineup_capture >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% lineups"& echo [%date% %time%] STEP FAILED: lineups>> "%LOG%")
"%PY%" -m prop_alpha.alerts.ledger --settle --send >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% settle"& echo [%date% %time%] STEP FAILED: settle>> "%LOG%")
"%PY%" -m prop_alpha.alerts.task_health --task ledger --failed "%FAILED%" >> "%LOG%" 2>&1 || (set RC=1& echo [%date% %time%] STEP FAILED: task health>> "%LOG%")
exit /b %RC%
