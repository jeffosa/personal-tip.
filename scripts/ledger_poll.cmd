@echo off
rem Every 10 minutes (Windows Task Scheduler task "prop_alpha\Bet ledger poll"): record bets from Telegram replies and
rem answer each one, capture closing prices for kickoffs with open bets or alerts, then settle open bets whose match
rem is final in the database (the 4-hourly task ingests results first). It is the only poller (two pollers at once
rem could record a reply twice) and the only writer of placed_bets.jsonl. Output is appended to
rem .prop_alpha_state\ledger.log (gitignored). Every step runs; the script exits 1 if any failed (audit 2026-10-05).
setlocal
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
set "LOG=.prop_alpha_state\ledger.log"
set "PY=.venv\Scripts\python.exe"
set RC=0
"%PY%" -m prop_alpha.alerts.ledger --poll >> "%LOG%" 2>&1 || (set RC=1& echo [%date% %time%] STEP FAILED: poll>> "%LOG%")
"%PY%" -m prop_alpha.alerts.closing >> "%LOG%" 2>&1 || (set RC=1& echo [%date% %time%] STEP FAILED: closing prices>> "%LOG%")
"%PY%" -m prop_alpha.alerts.ledger --settle --send >> "%LOG%" 2>&1 || (set RC=1& echo [%date% %time%] STEP FAILED: settle>> "%LOG%")
exit /b %RC%
