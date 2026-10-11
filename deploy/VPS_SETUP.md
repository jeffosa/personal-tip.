# Moving the scheduled tasks to an Ubuntu server

Maintainer decision 4 (2026-10-11): run the scheduled tasks on an always-on host instead of the laptop, after making
the Windows-only scheduling portable and testing a restore on that host. Until the cut-over below, the laptop stays the
only host (plugged in, tasks set to wake it).

**Status: written, not run [U].** The code runs on Ubuntu in CI (GitHub Actions, `ubuntu-24.04`, the full suite and the
PostgreSQL stage), but no server has been set up, no restore has been run on one and no timer has fired.

What is portable now:
- `scripts/run_task.py status|ledger|backup`: the three scheduled tasks, same steps, order, failed-step names and logs as
  the `.cmd` scripts (`tests/test_run_task.py` fails if they drift apart).
- `deploy/systemd/`: a service and a timer per task (00/04/08/12/16/20 UTC; every 10 minutes; 02:30 UTC daily).
- `scripts/backup.py` finds `pg_dump`, `pg_restore` and `psql` on PATH (or `PROP_ALPHA_PG_BIN`), and records each table's
  row count in the manifest.
- `scripts/restore_backup.py`: verifies a backup against its manifest, restores it into new databases (never over one),
  checks the row counts and copies the state files (never over one).

## 1. Server

Ubuntu 24.04 LTS, 2 vCPU, 4 GB RAM, 40 GB disk or more (the databases are about 100 MB, the backups keep 14 days on disk
plus `odds_archive/`). A European region keeps latency to the vendors low. Then, as root:

```bash
timedatectl set-timezone Etc/UTC
apt update && apt -y upgrade && apt -y install git unattended-upgrades ufw
ufw allow OpenSSH && ufw --force enable          # nothing else listens publicly; PostgreSQL stays on localhost
adduser --disabled-password --gecos "" propalpha
install -d -o propalpha -g propalpha /opt/prop_alpha
```

Log in with an SSH key only (`PasswordAuthentication no` in `/etc/ssh/sshd_config`).

## 2. PostgreSQL 18 and Python 3.14

```bash
apt -y install postgresql-common && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
apt -y install postgresql-18
add-apt-repository -y ppa:deadsnakes/ppa && apt -y install python3.14 python3.14-venv
sudo -u postgres psql -c "\password postgres"     # choose a password; it goes only into ~propalpha/.pgpass
```

As `propalpha`: create `~/.pgpass` with the line `localhost:5432:*:postgres:<password>` and `chmod 600 ~/.pgpass`.
The password is never put in a URL, file or command in the repository.

## 3. Code and settings

As `propalpha`:

```bash
git clone https://github.com/jeffosa/personal-tip..git /opt/prop_alpha
cd /opt/prop_alpha
python3.14 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
.venv/bin/python -m pytest tests -q -p no:cacheprovider
```

The test count must equal `EXPECTED_TESTS` in `ci/bootstrap.sh`, with nothing skipped. The maintainer copies `.env` from the laptop by hand (`scp`), then
`chmod 600 .env`. The backup push needs a git credential for the backup repository only: a fine-grained GitHub token
limited to `prop_alpha-backups` (Contents: read and write), stored with `git config --global credential.helper store`
(the file is `~/.git-credentials`, mode 600).

## 4. Test a restore (throwaway first)

```bash
git clone --single-branch --branch backup/prop_alpha_<newest stamp> <PROP_ALPHA_BACKUP_GIT_REMOTE> ~/restore
.venv/bin/python scripts/restore_backup.py ~/restore/prop_alpha_<stamp> --database prop_alpha_restore_test \
    --pilot-database prop_alpha_pilot_restore_test --no-state
dropdb -w -h localhost -U postgres prop_alpha_restore_test && dropdb -w -h localhost -U postgres prop_alpha_pilot_restore_test
```

It must end with `restore complete`, and each database line must say `match the backup`. A backup made before
2026-10-11 has no counts in its manifest; take a fresh one on the laptop first (`python scripts/backup.py`, then
`python scripts/backup_push.py`).

## 5. Cut-over (only one host may run the tasks)

Two hosts at once would poll Telegram twice (recording a reply twice) and send every alert twice. In this order:

1. On the laptop, disable the three tasks (Task Scheduler, or `schtasks /Change /TN "prop_alpha\<name>" /DISABLE` for
   `Telegram status`, `Bet ledger poll` and `Daily backup`), wait for any running one to finish, then run
   `python scripts/backup.py` and `python scripts/backup_push.py`.
2. On the server, clone that newest backup branch and restore it for real:
   `.venv/bin/python scripts/restore_backup.py ~/restore/prop_alpha_<stamp>` (creates `prop_alpha`, `prop_alpha_pilot`
   and `.prop_alpha_state/`).
3. Run each task once by hand and read its log: `.venv/bin/python scripts/run_task.py ledger` and
   `.venv/bin/python scripts/run_task.py status` (`.prop_alpha_state/ledger.log`, `status.log`).
4. Install and start the timers, as root:

   ```bash
   cp /opt/prop_alpha/deploy/systemd/prop-alpha-* /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now prop-alpha-status.timer prop-alpha-ledger.timer prop-alpha-backup.timer
   systemctl list-timers 'prop-alpha-*'
   ```

Runs show in `journalctl -u prop-alpha-status` and in the task logs; Telegram failure alerts work as before. Retries:
the backup is retried 30 minutes later up to 3 times, as on Windows; the status and ledger runs are not retried (a
re-run would send the same messages again) and the next scheduled run follows. task_health's messages still name the
Windows tasks (`prop_alpha\Telegram status`, ...); read them as the matching timer.

## 6. Rollback to the laptop

`systemctl disable --now prop-alpha-status.timer prop-alpha-ledger.timer prop-alpha-backup.timer` on the server, run
`run_task.py backup` there, restore that backup on the laptop into new databases and state (the laptop's are now out of
date; rename the old ones first), then re-enable the three Windows tasks.

## Not covered

- Nothing notices that the server itself is down (task_health runs on it). An outside heartbeat service would **[N]**.
- The laptop's `.cmd` scripts and `run_hidden.py` stay until the cut-over has run for a while; then they can go.
