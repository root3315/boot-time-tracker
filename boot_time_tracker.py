#!/usr/bin/env python3
"""Track and log system boot times over time."""

import os
import sys
import json
import sqlite3
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev


DATA_DIR = Path.home() / ".local" / "share" / "boot-time-tracker"
DB_FILE = DATA_DIR / "boot_times.db"
CONFIG_FILE = DATA_DIR / "config.json"


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_default_config():
    return {
        "boot_time_threshold_seconds": 120,
        "alert_on_slow_boot": True,
        "retention_days": 365,
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
        from datetime import timedelta
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
            from datetime import timezone as tz
            return datetime.fromtimestamp(int(match.group(1)), tz=tz.utc)
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
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM boot_phases WHERE boot_record_id IN "
                   "(SELECT id FROM boot_records WHERE boot_time < ?)", (cutoff,))
    cursor.execute("DELETE FROM boot_records WHERE boot_time < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount


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

        print_stats(conn)

    elif sys.argv[1] == "stats":
        print_stats(conn)

    elif sys.argv[1] == "config":
        if len(sys.argv) == 4 and sys.argv[2] == "set":
            key = sys.argv[3]
            value = sys.argv[4] if len(sys.argv) > 4 else None
            if value is None:
                print("Usage: boot_time_tracker.py config set <key> <value>")
                sys.exit(1)
            try:
                config[key] = float(value)
            except ValueError:
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

    else:
        print("Usage: boot_time_tracker.py [stats|config|clear]")
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
