@echo off
rem Sends the prop_alpha status report, then the opportunity scan on real OddsPapi odds, to Telegram. Run every 4 hours by the Windows Task Scheduler task
rem "prop_alpha\Telegram status". Output is appended to .prop_alpha_state\status.log (gitignored).
cd /d "%~dp0.."
if not exist ".prop_alpha_state" mkdir ".prop_alpha_state"
rem Bet-recording replies are polled every 10 minutes by scripts\ledger_poll.cmd (task "prop_alpha\Bet ledger poll"),
rem never here: two pollers at once could record the same reply twice.
".venv\Scripts\python.exe" -m prop_alpha.alerts.status --send >> ".prop_alpha_state\status.log" 2>&1
".venv\Scripts\python.exe" -m prop_alpha.alerts.scan --odds oddspapi --send >> ".prop_alpha_state\status.log" 2>&1
