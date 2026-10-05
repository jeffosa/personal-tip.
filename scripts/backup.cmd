@echo off
rem Once a day (Windows Task Scheduler task "prop_alpha\Daily backup"): a verified pg_dump of the working database plus
rem the ledger and alert state files, into PROP_ALPHA_BACKUP_DIR (process environment, then .env; default
rem .prop_alpha_state\backups\daily, on this machine's disk), keeping the newest 14. Then the newest backup is pushed
rem to the private GitHub repository in PROP_ALPHA_BACKUP_GIT_REMOTE (scripts\backup_push.py), the off-machine copy.
rem Output is appended to .prop_alpha_state\backup.log (gitignored). The script exits 1 if a step failed.
rem Last, the run is reported to prop_alpha.alerts.task_health, which sends a Telegram alert after 2 failing runs in a row
rem (the task retries a failed run) and flags another task that has stopped running.
setlocal
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
set "LOG=.prop_alpha_state\backup.log"
set "PY=.venv\Scripts\python.exe"
set RC=0
set "FAILED="
"%PY%" scripts\backup.py --keep 14 >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% backup"& echo [%date% %time%] STEP FAILED: backup>> "%LOG%")
"%PY%" scripts\backup_push.py --keep 14 >> "%LOG%" 2>&1 || (set RC=1& set "FAILED=%FAILED% github-push"& echo [%date% %time%] STEP FAILED: github push>> "%LOG%")
"%PY%" -m prop_alpha.alerts.task_health --task backup --failed "%FAILED%" >> "%LOG%" 2>&1 || (set RC=1& echo [%date% %time%] STEP FAILED: task health>> "%LOG%")
exit /b %RC%
