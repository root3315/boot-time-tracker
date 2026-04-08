#!/usr/bin/env python3
"""Track and log system boot times over time."""

import os
import sys
import json
import sqlite3
import platform
import subprocess
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import mean, median, stdev
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


DATA_DIR = Path.home() / ".local" / "share" / "boot-time-tracker"
DB_FILE = DATA_DIR / "boot_times.db"
CONFIG_FILE = DATA_DIR / "config.json"
ALERT_LOG_FILE = DATA_DIR / "alert_log.json"

logger = logging.getLogger("boot_time_tracker")


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_default_config():
    return {
        "boot_time_threshold_seconds": 120,
        "alert_on_slow_boot": True,
        "retention_days": 365,
        "alert_methods": ["log"],
        "alert_email_smtp_host": "localhost",
        "alert_email_smtp_port": 25,
        "alert_email_from": "",
        "alert_email_to": "",
        "alert_email_use_tls": False,
        "alert_email_username": "",
        "alert_email_password": "",
        "alert_cooldown_seconds": 0,
        "alert_command": "",
    }


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            stored = json.load(f)
        config = get_default_config()
        config.update(stored)
        return config
    return get_default_config()


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def init_db():
    conn = sqlite3.connect(str(DB_FILE))
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS boot_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boot_time TEXT NOT NULL,
            duration_seconds REAL,
            kernel_version TEXT,
            hostname TEXT,
            is_slow INTEGER DEFAULT 0,
            notes TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS boot_phases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boot_record_id INTEGER,
            phase_name TEXT NOT NULL,
            duration_seconds REAL NOT NULL,
            FOREIGN KEY (boot_record_id) REFERENCES boot_records(id)
        )
    """)
    conn.commit()
    return conn


def detect_os():
    system = platform.system().lower()
    if system == "linux":
        return "linux"
    elif system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    return "unknown"


def get_boot_time_linux():
    result = subprocess.run(
        ["systemctl", "show", "kernel", "--property=InactiveExitTimestamp"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0 and result.stdout.strip():
        ts_str = result.stdout.split("=", 1)[1].strip()
        if ts_str and ts_str != "n/a":
            try:
                dt = datetime.strptime(ts_str, "%a %Y-%m-%d %H:%M:%S %Z")
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

    result = subprocess.run(
        ["who", "-b"], capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0 and result.stdout.strip():
        lines = result.stdout.strip().split("\n")
        for line in lines:
            if "system boot" in line.lower():
                parts = line.split()
                date_str = " ".join(parts[-3:])
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
                    return dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass

    uptime_seconds = get_uptime_seconds_linux()
    if uptime_seconds is not None:
        return datetime.now(timezone.utc) - timedelta(seconds=uptime_seconds)

    return None


def get_uptime_seconds_linux():
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.readline().split()[0])
    except (IOError, OSError, IndexError):
        return None


def get_boot_time_macos():
    result = subprocess.run(
        ["sysctl", "-n", "kern.boottime"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0 and result.stdout.strip():
        output = result.stdout.strip()
        import re
        match = re.search(r"sec = (\d+)", output)
        if match:
            return datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
    return None


def get_boot_time_windows():
    result = subprocess.run(
        ["powershell", "-Command",
         "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime"],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0 and result.stdout.strip():
        ts_str = result.stdout.strip()
        try:
            dt = datetime.strptime(ts_str, "%m/%d/%Y %I:%M:%S %p")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def get_boot_time():
    os_name = detect_os()
    if os_name == "linux":
        return get_boot_time_linux()
    elif os_name == "macos":
        return get_boot_time_macos()
    elif os_name == "windows":
        return get_boot_time_windows()
    return None


def estimate_boot_duration_linux():
    phases = {}
    try:
        result = subprocess.run(
            ["systemd-analyze"], capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            output = result.stdout + result.stderr
            import re
            kernel_match = re.search(r"kernel:\s*([\d.]+)(ms|s|min)", output)
            userspace_match = re.search(r"userspace:\s*([\d.]+)(ms|s|min)", output)
            if kernel_match:
                val = float(kernel_match.group(1))
                unit = kernel_match.group(2)
                phases["kernel"] = to_seconds(val, unit)
            if userspace_match:
                val = float(userspace_match.group(1))
                unit = userspace_match.group(2)
                phases["userspace"] = to_seconds(val, unit)

            total_match = re.search(
                r"Startup finished in\s+([\d.]+)(ms|s|min)", output
            )
            if total_match:
                val = float(total_match.group(1))
                unit = total_match.group(2)
                phases["total"] = to_seconds(val, unit)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    uptime = get_uptime_seconds_linux()
    if uptime is not None and uptime < 600:
        phases["total"] = uptime

    return phases


def to_seconds(value, unit):
    if unit == "ms":
        return value / 1000.0
    elif unit == "s":
        return value
    elif unit == "min":
        return value * 60.0
    return value


def record_boot(conn, boot_time, phases, config):
    cursor = conn.cursor()
    kernel_version = platform.release()
    hostname = platform.node()

    duration = phases.get("total")
    threshold = config.get("boot_time_threshold_seconds", 120)
    is_slow = 1 if duration and duration > threshold else 0

    cursor.execute("""
        INSERT INTO boot_records
            (boot_time, duration_seconds, kernel_version, hostname, is_slow, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        boot_time.isoformat() if boot_time else None,
        duration,
        kernel_version,
        hostname,
        is_slow,
        "recorded automatically"
    ))
    record_id = cursor.lastrowid

    for phase_name, phase_duration in phases.items():
        if phase_name != "total":
            cursor.execute("""
                INSERT INTO boot_phases (boot_record_id, phase_name, duration_seconds)
                VALUES (?, ?, ?)
            """, (record_id, phase_name, phase_duration))

    conn.commit()
    return record_id


def cleanup_old_records(conn, retention_days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM boot_phases WHERE boot_record_id IN "
                   "(SELECT id FROM boot_records WHERE boot_time < ?)", (cutoff,))
    cursor.execute("DELETE FROM boot_records WHERE boot_time < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount


def load_alert_history():
    if ALERT_LOG_FILE.exists():
        with open(ALERT_LOG_FILE, "r") as f:
            return json.load(f)
    return {"alerts": []}


def save_alert_history(history):
    with open(ALERT_LOG_FILE, "w") as f:
        json.dump(history, f, indent=2)


def is_alert_cooldown_active(config):
    cooldown = config.get("alert_cooldown_seconds", 0)
    if cooldown <= 0:
        return False
    history = load_alert_history()
    if not history["alerts"]:
        return False
    last_alert_time = history["alerts"][-1].get("timestamp", "")
    try:
        last_dt = datetime.fromisoformat(last_alert_time)
        now = datetime.now(timezone.utc)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        elapsed = (now - last_dt).total_seconds()
        return elapsed < cooldown
    except (ValueError, TypeError):
        return False


def record_alert(boot_record_id, duration, threshold, method, details=""):
    history = load_alert_history()
    history["alerts"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "boot_record_id": boot_record_id,
        "duration_seconds": duration,
        "threshold_seconds": threshold,
        "method": method,
        "details": details,
    })
    max_entries = 1000
    if len(history["alerts"]) > max_entries:
        history["alerts"] = history["alerts"][-max_entries:]
    save_alert_history(history)


def send_alert_log(boot_record_id, duration, threshold, hostname):
    message = (
        f"SLOW BOOT DETECTED\n"
        f"  Hostname:  {hostname}\n"
        f"  Boot time: {duration:.1f}s\n"
        f"  Threshold: {threshold:.1f}s\n"
        f"  Over by:   {duration - threshold:.1f}s\n"
        f"  Recorded:  {datetime.now(timezone.utc).isoformat()}\n"
    )
    print(message)
    record_alert(boot_record_id, duration, threshold, "log", message.strip())
    return True


def send_alert_email(boot_record_id, duration, threshold, hostname, config):
    smtp_host = config.get("alert_email_smtp_host", "localhost")
    smtp_port = config.get("alert_email_smtp_port", 25)
    from_addr = config.get("alert_email_from", "")
    to_addr = config.get("alert_email_to", "")
    use_tls = config.get("alert_email_use_tls", False)
    username = config.get("alert_email_username", "")
    password = config.get("alert_email_password", "")

    if not from_addr or not to_addr:
        logger.warning("Email alerting configured but from/to addresses are empty.")
        return False

    subject = f"[boot-time-tracker] Slow boot on {hostname}: {duration:.1f}s"
    body = (
        f"Slow boot detected on {hostname}\n"
        f"\n"
        f"  Boot duration: {duration:.1f}s\n"
        f"  Threshold:     {threshold:.1f}s\n"
        f"  Over by:       {duration - threshold:.1f}s\n"
        f"  Time:          {datetime.now(timezone.utc).isoformat()}\n"
    )

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        import smtplib
        server = smtplib.SMTP(smtp_host, smtp_port)
        if use_tls:
            server.starttls()
        if username and password:
            server.login(username, password)
        server.sendmail(from_addr, to_addr, msg.as_string())
        server.quit()
        record_alert(boot_record_id, duration, threshold, "email",
                     f"Sent to {to_addr}")
        return True
    except Exception as exc:
        logger.error(f"Failed to send email alert: {exc}")
        record_alert(boot_record_id, duration, threshold, "email",
                     f"FAILED: {exc}")
        return False


def send_alert_command(boot_record_id, duration, threshold, hostname, config):
    alert_cmd = config.get("alert_command", "")
    if not alert_cmd:
        return False

    env = os.environ.copy()
    env["BOOT_DURATION"] = str(duration)
    env["BOOT_THRESHOLD"] = str(threshold)
    env["BOOT_HOSTNAME"] = hostname
    env["BOOT_RECORD_ID"] = str(boot_record_id)

    try:
        result = subprocess.run(
            alert_cmd, shell=True, env=env,
            capture_output=True, text=True, timeout=30
        )
        status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
        record_alert(boot_record_id, duration, threshold, "command",
                     f"cmd='{alert_cmd}' status={status}")
        return result.returncode == 0
    except Exception as exc:
        logger.error(f"Failed to run alert command: {exc}")
        record_alert(boot_record_id, duration, threshold, "command",
                     f"FAILED: {exc}")
        return False


def handle_slow_boot_alert(conn, boot_record_id, duration, config):
    if not config.get("alert_on_slow_boot", True):
        return

    threshold = config.get("boot_time_threshold_seconds", 120)
    if duration <= threshold:
        return

    if is_alert_cooldown_active(config):
        logger.info("Alert suppressed by cooldown.")
        return

    hostname = platform.node()
    methods = config.get("alert_methods", ["log"])
    if isinstance(methods, str):
        methods = [methods]

    for method in methods:
        method = method.strip().lower()
        if method == "log":
            send_alert_log(boot_record_id, duration, threshold, hostname)
        elif method == "email":
            send_alert_email(boot_record_id, duration, threshold, hostname, config)
        elif method == "command":
            send_alert_command(boot_record_id, duration, threshold, hostname, config)


def print_stats(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM boot_records")
    total = cursor.fetchone()[0]

    if total == 0:
        print("No boot records found yet.")
        return

    cursor.execute("SELECT duration_seconds FROM boot_records "
                   "WHERE duration_seconds IS NOT NULL ORDER BY boot_time")
    durations = [row[0] for row in cursor.fetchall()]

    cursor.execute("SELECT boot_time, duration_seconds FROM boot_records "
                   "ORDER BY boot_time DESC LIMIT 5")
    recent = cursor.fetchall()

    print(f"\n{'='*50}")
    print(f"Boot Time Statistics")
    print(f"{'='*50}")
    print(f"Total records: {total}")

    if durations:
        avg_boot = mean(durations)
        med_boot = median(durations)
        std_boot = stdev(durations) if len(durations) > 1 else 0
        min_boot = min(durations)
        max_boot = max(durations)

        print(f"Average boot time: {avg_boot:.1f}s")
        print(f"Median boot time:  {med_boot:.1f}s")
        print(f"Std deviation:     {std_boot:.1f}s")
        print(f"Min boot time:     {min_boot:.1f}s")
        print(f"Max boot time:     {max_boot:.1f}s")

    print(f"\nRecent boots:")
    for bt, dur in recent:
        dur_str = f"{dur:.1f}s" if dur else "N/A"
        print(f"  {bt} -> {dur_str}")
    print(f"{'='*50}\n")


def print_alerts(conn):
    history = load_alert_history()
    alerts = history.get("alerts", [])
    if not alerts:
        print("No slow boot alerts recorded.")
        return

    print(f"\n{'='*50}")
    print(f"Slow Boot Alerts ({len(alerts)} total)")
    print(f"{'='*50}")
    for alert in alerts[-20:]:
        ts = alert.get("timestamp", "unknown")
        dur = alert.get("duration_seconds", 0)
        thresh = alert.get("threshold_seconds", 0)
        method = alert.get("method", "?")
        details = alert.get("details", "")
        print(f"  [{ts}] {dur:.1f}s (threshold {thresh:.1f}s) via {method}")
        if details and details != "N/A":
            short = details[:80]
            print(f"    {short}")
    print(f"{'='*50}\n")


def main():
    ensure_data_dir()
    config = load_config()
    conn = init_db()

    if len(sys.argv) < 2:
        boot_time = get_boot_time()
        if boot_time is None:
            print("Could not detect boot time.")
            sys.exit(1)

        print(f"Detected boot time: {boot_time.isoformat()}")

        phases = {}
        if detect_os() == "linux":
            phases = estimate_boot_duration_linux()

        if phases:
            print(f"Boot phases: {phases}")

        record_id = record_boot(conn, boot_time, phases, config)
        print(f"Recorded boot with ID: {record_id}")

        removed = cleanup_old_records(conn, config.get("retention_days", 365))
        if removed > 0:
            print(f"Cleaned up {removed} old records.")

        duration = phases.get("total")
        if duration is not None:
            handle_slow_boot_alert(conn, record_id, duration, config)

        print_stats(conn)

    elif sys.argv[1] == "stats":
        print_stats(conn)

    elif sys.argv[1] == "alerts":
        print_alerts(conn)

    elif sys.argv[1] == "config":
        if len(sys.argv) >= 4 and sys.argv[2] == "set":
            key = sys.argv[3]
            value = sys.argv[4] if len(sys.argv) > 4 else None
            if value is None:
                print("Usage: boot_time_tracker.py config set <key> <value>")
                sys.exit(1)
            try:
                if "." in value:
                    config[key] = float(value)
                else:
                    config[key] = int(value)
            except ValueError:
                if value.lower() in ("true", "false"):
                    config[key] = value.lower() == "true"
                else:
                    config[key] = value
            save_config(config)
            print(f"Config updated: {key} = {config[key]}")
        else:
            print("Current config:")
            for k, v in config.items():
                print(f"  {k}: {v}")

    elif sys.argv[1] == "clear":
        cursor = conn.cursor()
        cursor.execute("DELETE FROM boot_phases")
        cursor.execute("DELETE FROM boot_records")
        conn.commit()
        print("All boot records cleared.")

    elif sys.argv[1] == "clear-alerts":
        save_alert_history({"alerts": []})
        print("All alert history cleared.")

    else:
        print("Usage: boot_time_tracker.py [stats|alerts|config|clear|clear-alerts]")
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
