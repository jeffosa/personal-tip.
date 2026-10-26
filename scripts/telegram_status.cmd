@echo off
rem Every 4 hours (Windows Task Scheduler task "prop_alpha\Telegram status"): ingest finished results, refresh the
rem exchange rate, send the status report, then the opportunity scan on real OddsPapi odds. Output is appended to
rem .prop_alpha_state\status.log (gitignored).
rem Every step runs even if an earlier one failed (a scan on slightly stale history still beats no scan), but each
rem failure is logged and the script exits 1 if any step failed, so the task's Last Run Result shows it (audit
rem 2026-10-05). The scan exits 3 when it ran without real bookmaker odds; that is reported as the step scan-no-odds.
rem The pilot leagues (Championship, Scottish Premiership and Championship; maintainer, 2026-10-11) follow the same pattern: their
rem results go into the pilot database first (pilot-results), then alerts.ladder_scan scans them. It exits 3 without real
rem odds (pilot-scan-no-odds) and 4 when a league was skipped because its results were missing (pilot-scan-stale). It talks
rem to the chat only when a pilot match is in the window or something failed.
rem Last, the run is reported to prop_alpha.alerts.task_health with the names of the failed steps, which sends a Telegram
rem alert after 2 failing runs in a row and flags another task that has stopped running.
rem Bet-recording replies are polled every 10 minutes by scripts\ledger_poll.cmd (task "prop_alpha\Bet ledger poll"),
rem never here: two pollers at once could record the same reply twice.
setlocal
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
set "LOG=.prop_alpha_state\status.log"
set "PY=.venv\Scripts\python.exe"
set RC=0
set "FAILED="
rem Results first: the scan prices from current history, and the ledger poll settles bets from it.
"%PY%" -m prop_alpha.ingestion.results >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% results"& echo [%date% %time%] STEP FAILED: results>> "%LOG%")
rem The pilot database, from its own results job: the pilot scan prices from it.
"%PY%" -m prop_alpha.ingestion.results --pilot >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% pilot-results"& echo [%date% %time%] STEP FAILED: pilot results>> "%LOG%")
rem Lockbox outcomes, captured once each, 72 hours after kickoff (captures only: it never scores; 2026-10-26).
"%PY%" scripts\gate2_prospective.py --capture-only >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% lockbox-capture"& echo [%date% %time%] STEP FAILED: lockbox capture>> "%LOG%")
rem The book-currency exchange rate (a no-op when the bankroll is in the books' currency); the scan reads the cache.
"%PY%" -m prop_alpha.alerts.fx --refresh >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% fx-refresh"& echo [%date% %time%] STEP FAILED: fx refresh>> "%LOG%")
"%PY%" -m prop_alpha.alerts.status --send >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% status-report"& echo [%date% %time%] STEP FAILED: status report>> "%LOG%")
"%PY%" -m prop_alpha.alerts.scan --odds oddspapi --send >> "%LOG%" 2>&1
set SCAN_RC=%ERRORLEVEL%
if "%SCAN_RC%"=="3" (set RC=1& set "FAILED=%FAILED% scan-no-odds"& echo [%date% %time%] STEP FAILED: scan, no real odds>> "%LOG%") else if "%SCAN_RC%"=="4" (set RC=1& set "FAILED=%FAILED% scan-stale"& echo [%date% %time%] STEP FAILED: scan, League One/Two results not current>> "%LOG%") else if not "%SCAN_RC%"=="0" (set RC=1& set "FAILED=%FAILED% scan"& echo [%date% %time%] STEP FAILED: scan>> "%LOG%")
"%PY%" -m prop_alpha.alerts.ladder_scan --odds oddspapi --send >> "%LOG%" 2>&1
set PILOT_RC=%ERRORLEVEL%
if "%PILOT_RC%"=="3" (set RC=1& set "FAILED=%FAILED% pilot-scan-no-odds"& echo [%date% %time%] STEP FAILED: pilot scan, no real odds>> "%LOG%") else if "%PILOT_RC%"=="4" (set RC=1& set "FAILED=%FAILED% pilot-scan-stale"& echo [%date% %time%] STEP FAILED: pilot scan, a league skipped as stale>> "%LOG%") else if not "%PILOT_RC%"=="0" (set RC=1& set "FAILED=%FAILED% pilot-scan"& echo [%date% %time%] STEP FAILED: pilot scan>> "%LOG%")
"%PY%" -m prop_alpha.alerts.task_health --task status --failed "%FAILED%" >> "%LOG%" 2>&1 || (set RC=1& echo [%date% %time%] STEP FAILED: task health>> "%LOG%")
exit /b %RC%
