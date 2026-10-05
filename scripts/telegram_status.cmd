@echo off
rem Every 4 hours (Windows Task Scheduler task "prop_alpha\Telegram status"): ingest finished results, refresh the
rem exchange rate, send the status report, then the opportunity scan on real OddsPapi odds. Output is appended to
rem .prop_alpha_state\status.log (gitignored).
rem Every step runs even if an earlier one failed (a scan on slightly stale history still beats no scan), but each
rem failure is logged and the script exits 1 if any step failed, so the task's Last Run Result shows it (audit
rem 2026-10-05). The scan exits 3 when it ran without real bookmaker odds.
rem Bet-recording replies are polled every 10 minutes by scripts\ledger_poll.cmd (task "prop_alpha\Bet ledger poll"),
rem never here: two pollers at once could record the same reply twice.
setlocal
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
set "LOG=.prop_alpha_state\status.log"
set "PY=.venv\Scripts\python.exe"
set RC=0
rem Results first: the scan prices from current history, and the ledger poll settles bets from it.
"%PY%" -m prop_alpha.ingestion.results >> "%LOG%" 2>&1 || (set RC=1& echo [%date% %time%] STEP FAILED: results>> "%LOG%")
rem The book-currency exchange rate (a no-op when the bankroll is in the books' currency); the scan reads the cache.
"%PY%" -m prop_alpha.alerts.fx --refresh >> "%LOG%" 2>&1 || (set RC=1& echo [%date% %time%] STEP FAILED: fx refresh>> "%LOG%")
"%PY%" -m prop_alpha.alerts.status --send >> "%LOG%" 2>&1 || (set RC=1& echo [%date% %time%] STEP FAILED: status report>> "%LOG%")
"%PY%" -m prop_alpha.alerts.scan --odds oddspapi --send >> "%LOG%" 2>&1 || (set RC=1& echo [%date% %time%] STEP FAILED: scan>> "%LOG%")
exit /b %RC%
