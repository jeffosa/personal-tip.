@echo off
rem Records bets from Telegram replies and answers each one. Run every 10 minutes by the Windows Task Scheduler task
rem "prop_alpha\Bet ledger poll". It is the only poller: two pollers at once could record a reply twice.
rem Output is appended to .prop_alpha_state\ledger.log (gitignored).
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
".venv\Scripts\python.exe" -m prop_alpha.alerts.ledger --poll >> ".prop_alpha_state\ledger.log" 2>&1
