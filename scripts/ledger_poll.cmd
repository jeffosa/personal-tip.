@echo off
rem Records bets from Telegram replies and answers each one. Run every 10 minutes by the Windows Task Scheduler task
rem "prop_alpha\Bet ledger poll". It is the only poller: two pollers at once could record a reply twice.
rem It then settles open bets whose match is final in the database (the 4-hourly task ingests results first) and
rem sends the new results. It is the only writer of placed_bets.jsonl, so settling lives here, not in the 4-hourly task.
rem Output is appended to .prop_alpha_state\ledger.log (gitignored).
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
".venv\Scripts\python.exe" -m prop_alpha.alerts.ledger --poll >> ".prop_alpha_state\ledger.log" 2>&1
".venv\Scripts\python.exe" -m prop_alpha.alerts.ledger --settle --send >> ".prop_alpha_state\ledger.log" 2>&1
