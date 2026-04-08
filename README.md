# boot-time-tracker

Track and log system boot times over time. Because sometimes you need to know if that "quick reboot" actually was quick.

## What it does

Records each boot with timestamp, duration, kernel version, and hostname. Stores everything in SQLite so you don't need any external dependencies. Works on Linux, macOS, and Windows (though Linux gets the most detailed phase breakdown via `systemd-analyze`).

Detects slow boots and can notify you via log output, email, or a custom command.

## Quick start

```bash
python3 boot_time_tracker.py
```

Run it after boot (or stick it in a cron job / systemd service / startup script) and it'll log the current boot.

## Commands

| Command | What it does |
|---|---|
| `python3 boot_time_tracker.py` | Detect and record current boot |
| `python3 boot_time_tracker.py stats` | Show stats — avg, median, min, max, recent boots |
| `python3 boot_time_tracker.py alerts` | Show slow boot alert history |
| `python3 boot_time_tracker.py config` | Print current config |
| `python3 boot_time_tracker.py config set <key> <value>` | Change a config value |
| `python3 boot_time_tracker.py clear` | Wipe all records |
| `python3 boot_time_tracker.py clear-alerts` | Wipe alert history |

## Config options

- `boot_time_threshold_seconds` — what counts as a "slow" boot (default: 120)
- `alert_on_slow_boot` — enable/disable slow boot alerts (default: true)
- `retention_days` — how long to keep records before cleanup (default: 365)
- `alert_methods` — list of alert channels: `"log"`, `"email"`, `"command"` (default: `["log"]`)
- `alert_cooldown_seconds` — minimum seconds between alerts to avoid spam (default: 0)
- `alert_email_smtp_host` — SMTP server host (default: localhost)
- `alert_email_smtp_port` — SMTP server port (default: 25)
- `alert_email_from` — sender email address
- `alert_email_to` — recipient email address
- `alert_email_use_tls` — use STARTTLS (default: false)
- `alert_email_username` — SMTP login username
- `alert_email_password` — SMTP login password
- `alert_command` — shell command to run on slow boot (env vars: `BOOT_DURATION`, `BOOT_THRESHOLD`, `BOOT_HOSTNAME`, `BOOT_RECORD_ID`)

## Alert examples

**Log only (default):** prints a warning to stdout and writes to `~/.local/share/boot-time-tracker/alert_log.json`.

**Email notification:**

```bash
python3 boot_time_tracker.py config set alert_methods email
python3 boot_time_tracker.py config set alert_email_from tracker@example.com
python3 boot_time_tracker.py config set alert_email_to admin@example.com
python3 boot_time_tracker.py config set alert_email_smtp_host smtp.example.com
python3 boot_time_tracker.py config set alert_email_smtp_port 587
python3 boot_time_tracker.py config set alert_email_use_tls true
```

**Custom command** — send a desktop notification on Linux:

```bash
python3 boot_time_tracker.py config set alert_methods command
python3 boot_time_tracker.py config set alert_command 'notify-send "Slow boot" "Boot took ${BOOT_DURATION}s (threshold: ${BOOT_THRESHOLD}s)"'
```

**Multiple methods at once:**

```bash
python3 boot_time_tracker.py config set alert_methods '["log", "email", "command"]'
```

**Cooldown** — only alert once per hour even if multiple slow boots happen:

```bash
python3 boot_time_tracker.py config set alert_cooldown_seconds 3600
```

## Where data lives

- Database: `~/.local/share/boot-time-tracker/boot_times.db`
- Config: `~/.local/share/boot-time-tracker/config.json`
- Alert log: `~/.local/share/boot-time-tracker/alert_log.json`

All are created automatically on first run.

## Auto-start setup

If you want this to run on every boot automatically, drop a systemd service in:

```ini
[Unit]
Description=Boot Time Tracker
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /path/to/boot_time_tracker.py

[Install]
WantedBy=multi-user.target
```

Enable with `systemctl enable --now boot-time-tracker.service`.

## Notes

- On Linux, it tries `systemctl show kernel` first, then falls back to `who -b`, then `/proc/uptime`.
- Boot phase breakdown (kernel vs userspace) only works if `systemd-analyze` is available.
- No external Python packages needed — stdlib only. That's why requirements.txt is basically empty.
- Old records get cleaned up automatically based on `retention_days`.

## Why I built this

Machine was feeling sluggish after an update. Wanted to track whether reboots were actually getting slower or if it was just placebo. Turned out kernel updates were adding ~15s to boot. Nice to have numbers instead of vibes.
