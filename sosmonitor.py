#!/usr/bin/env python3
"""
SOSMonitor: lightweight server monitor for hosts without root.

Measures CPU, memory, disk and load average and sends alerts by webhook
and/or email when a threshold is crossed. Standard library only; psutil is
used when installed.

    python3 sosmonitor.py              one check (creates config.json if missing)
    python3 sosmonitor.py test         send a test alert to the enabled channels
    python3 sosmonitor.py install      schedule in crontab
    python3 sosmonitor.py uninstall    remove from crontab
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import psutil  # optional
except ImportError:
    psutil = None

SCRIPT = Path(__file__).resolve()
CRON_MARK = "# sosmonitor"
LOG_FILE = "sosmonitor.log"
STATE_FILE = "sosmonitor_state.json"
LOCK_FILE = ".sosmonitor.lock"
LOG_MAX_BYTES = 5_000_000
TIMEOUT = 15
METRIC_LABELS = {"cpu": "CPU", "memory": "Memory", "disk": "Disk"}
HYSTERESIS = 5  # points a value must fall below a level before it drops
TASK_NAME = "SOSMonitor"  # Windows Task Scheduler

DEFAULTS: Dict[str, Any] = {
    "server_name": "SOSMONITOR",
    "check_interval_minutes": 1,
    "disk_path": "/",
    "send_every_check": True,
    "thresholds": {
        "70": "{metric} at {value}%",
        "80": "{metric} at {value}% (WARNING)",
        "90": "{metric} at {value}% (CRITICAL)",
        "100": "{metric} at {value}% (AT LIMIT)",
    },
    "recovery_message": "{metric} at {value}% (BACK TO NORMAL)",
    "webhook": {"enabled": False, "url": "", "token": "", "token_header": "X-Monitor-Token"},
    "email": {
        "enabled": False,
        "smtp_host": "",
        "smtp_port": 587,
        "username": "",
        "password": "",
        "from": "",
        "to": [],
    },
}


# ============================================================
# CONFIG
# ============================================================

def deep_merge(base: dict, override: dict) -> dict:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict) and key != "thresholds":
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_template(server_name: str) -> dict:
    template = deepcopy(DEFAULTS)
    template["server_name"] = server_name
    return template


def create_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = build_template(socket.gethostname().split(".")[0].upper())
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # holds SMTP password / webhook token
    except OSError:
        pass


def load_config(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid JSON in {path}: {error}")
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} must contain a JSON object")

    cfg = deep_merge(DEFAULTS, raw)

    try:
        cfg["thresholds"] = sorted(
            (int(level), str(message)) for level, message in cfg["thresholds"].items()
        )
    except (AttributeError, TypeError, ValueError):
        raise SystemExit(
            'thresholds must be an object like {"70": "message", "80": "message"}'
        )

    cfg["_dir"] = path.parent
    return cfg


def file_in(cfg: dict, name: str) -> Path:
    return cfg["_dir"] / name


# ============================================================
# METRICS
# ============================================================

def _read_proc_stat() -> Optional[Tuple[int, int]]:
    try:
        with open("/proc/stat", encoding="utf-8") as file:
            values = [int(x) for x in file.readline().split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return idle, sum(values[:8])
    except (OSError, ValueError, IndexError):
        return None


def get_cpu_usage(sample_seconds: float = 1.0) -> Optional[float]:
    if psutil:
        return round(psutil.cpu_percent(interval=sample_seconds), 1)

    first = _read_proc_stat()
    if first is None:
        return None
    time.sleep(sample_seconds)
    second = _read_proc_stat()
    if second is None:
        return None

    idle = second[0] - first[0]
    total = second[1] - first[1]
    return round(100.0 * (1 - idle / total), 1) if total > 0 else 0.0


def get_memory_usage() -> Optional[dict]:
    if psutil:
        memory = psutil.virtual_memory()
        return {
            "percent": round(memory.percent, 1),
            "total_bytes": memory.total,
            "available_bytes": memory.available,
            "used_bytes": memory.used,
        }

    try:
        info: Dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as file:
            for line in file:
                key, value = line.split(":", 1)
                info[key] = int(value.split()[0]) * 1024

        total = info["MemTotal"]
        available = info.get(
            "MemAvailable",
            info["MemFree"] + info.get("Buffers", 0) + info.get("Cached", 0),
        )
        used = total - available
        return {
            "percent": round(used / total * 100, 1),
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
        }
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def get_disk_usage(path: str) -> Optional[dict]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None

    # Same formula as `df`: used / (used + free)
    denominator = usage.used + usage.free
    return {
        "percent": round(usage.used / denominator * 100, 1) if denominator else 0.0,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "path": path,
    }


def get_load_average() -> Optional[dict]:
    try:
        load_1, load_5, load_15 = (psutil or os).getloadavg()
    except (AttributeError, OSError):
        return None

    cpu_count = os.cpu_count() or 1
    return {
        "1m": round(load_1, 2),
        "5m": round(load_5, 2),
        "15m": round(load_15, 2),
        "cpu_count": cpu_count,
        "1m_per_cpu": round(load_1 / cpu_count, 2),
    }


def collect_metrics(cfg: dict) -> dict:
    cpu = get_cpu_usage()
    readings = {
        "cpu": {"percent": cpu} if cpu is not None else None,
        "memory": get_memory_usage(),
        "disk": get_disk_usage(cfg["disk_path"]),
        "load": get_load_average(),
    }
    return {name: data for name, data in readings.items() if data is not None}


# ============================================================
# ALERTS
# ============================================================

class _KeepMissing(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def format_message(template: str, server: str, metric: str, value: Any) -> str:
    if isinstance(value, float):
        value = int(value + 0.5)
    fields = _KeepMissing(
        server=server, metric=METRIC_LABELS.get(metric, metric), value=value
    )
    try:
        return template.format_map(fields)
    except (ValueError, IndexError, AttributeError):
        return template


def load_state(cfg: dict) -> dict:
    try:
        return json.loads(file_in(cfg, STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(cfg: dict, state: dict) -> None:
    path = file_in(cfg, STATE_FILE)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as error:
        print(f"state write failed: {error}", file=sys.stderr)


def build_alerts(cfg: dict, metrics: dict, previous_state: dict) -> Tuple[List[dict], dict]:
    """
    An alert goes out only when a metric changes threshold level. Rising is
    immediate; a level is kept until the value falls HYSTERESIS points below
    it, so a value hovering around a threshold does not flap.
    """
    messages = dict(cfg["thresholds"])
    alerts: List[dict] = []
    new_state = dict(previous_state)

    for name, data in metrics.items():
        value = data.get("percent")  # load has no percentage, so no alerts
        if value is None:
            continue

        previous = previous_state.get(name, 0)
        current = max((level for level in messages if value >= level), default=0)
        if current < previous:
            current = max((level for level in messages
                           if level <= previous and value >= level - HYSTERESIS), default=0)

        if current != previous:
            template = messages[current] if current else cfg["recovery_message"]
            alerts.append({
                "metric": name,
                "value": value,
                "threshold": current,
                "previous_threshold": previous,
                "message": format_message(template, cfg["server_name"], name, value),
            })

        new_state[name] = current

    return alerts, new_state


# ============================================================
# NOTIFICATIONS
# ============================================================

def send_webhook(webhook: dict, payload: dict) -> Tuple[bool, str]:
    if not webhook["url"]:
        return False, "webhook url not set"

    headers = {"Content-Type": "application/json"}
    if webhook["token"]:
        headers[webhook["token_header"] or "X-Monitor-Token"] = webhook["token"]

    request = urllib.request.Request(
        webhook["url"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}: {error.reason}"
    except (urllib.error.URLError, OSError) as error:
        return False, f"connection error: {error}"


def format_email(payload: dict) -> Tuple[str, str]:
    messages = [alert["message"] for alert in payload["alerts"]]
    subject = f"[SOSMonitor] {payload['server']}: " + "; ".join(messages)

    lines = [f"Server: {payload['server']} ({payload['hostname']})",
             f"Time:   {payload['timestamp']}", ""]
    lines += [f"- {message}" for message in messages]
    lines += ["", "Current metrics:"]
    lines += [f"  {line}" for line in metric_lines(payload["metrics"])]
    return subject, "\n".join(lines) + "\n"


def send_email(email: dict, payload: dict) -> Tuple[bool, str]:
    recipients = email["to"] if isinstance(email["to"], list) else [email["to"]]
    recipients = [r for r in recipients if r]
    sender = email["from"] or email["username"]

    if not email["smtp_host"] or not recipients or not sender:
        return False, "smtp not configured (smtp_host, from/username and to)"

    subject, body = format_email(payload)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    host, port = email["smtp_host"], int(email["smtp_port"])
    context = ssl.create_default_context()

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)

        with server:
            server.ehlo()
            if port != 465 and server.has_extn("starttls"):
                server.starttls(context=context)
                server.ehlo()
            if email["username"] and email["password"]:
                server.login(email["username"], email["password"])
            server.send_message(message)

        return True, f"sent to {len(recipients)} recipient(s)"
    except (smtplib.SMTPException, OSError) as error:
        return False, f"smtp error: {error}"


def deliver(cfg: dict, payload: dict, force: bool = False) -> Tuple[bool, Dict[str, str]]:
    """
    Returns (delivered, results). With alerts, delivered is True when at least
    one channel succeeded (or none is enabled). Without alerts it is always True.
    """
    alerts = payload["alerts"]
    results: Dict[str, str] = {}
    attempted = successes = 0

    if cfg["webhook"]["enabled"] and (alerts or cfg["send_every_check"] or force):
        attempted += 1
        ok, results["webhook"] = send_webhook(cfg["webhook"], payload)
        successes += ok

    if cfg["email"]["enabled"] and alerts:
        attempted += 1
        ok, results["email"] = send_email(cfg["email"], payload)
        successes += ok

    return (not alerts or successes > 0 or attempted == 0), results


# ============================================================
# RUN
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def metric_lines(metrics: dict) -> List[str]:
    lines = []
    for name, data in metrics.items():
        if name == "load":
            lines.append(f"LOAD     1m={data['1m']} 5m={data['5m']} "
                         f"15m={data['15m']} CPUs={data['cpu_count']}")
        else:
            lines.append(f"{name.upper():<8} {int(data['percent'] + 0.5)}%")
    return lines


def build_payload(cfg: dict, metrics: dict, alerts: list) -> dict:
    return {
        "server": cfg["server_name"],
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "timestamp": now_iso(),
        "metrics": metrics,
        "alerts": alerts,
    }


def write_log(cfg: dict, record: dict) -> None:
    path = file_in(cfg, LOG_FILE)
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as error:
        print(f"log write failed: {error}", file=sys.stderr)


def run_once(cfg: dict) -> None:
    started = time.monotonic()
    metrics = collect_metrics(cfg)
    alerts, new_state = build_alerts(cfg, metrics, load_state(cfg))
    payload = build_payload(cfg, metrics, alerts)
    delivered, results = deliver(cfg, payload)

    write_log(cfg, {
        "timestamp": payload["timestamp"],
        "server": cfg["server_name"],
        "metrics": metrics,
        "alerts": alerts,
        "delivery": results,
        "duration_seconds": round(time.monotonic() - started, 2),
    })

    # If every channel failed, keep the old state so the next run retries.
    if delivered:
        save_state(cfg, new_state)

    print(f"SOSMonitor | {cfg['server_name']} | {payload['timestamp']}")
    for line in metric_lines(metrics):
        print(f"  {line}")
    for alert in alerts:
        print(f"  ALERT: {alert['message']}")
    for channel, result in results.items():
        print(f"  {channel}: {result}")


def run_test(cfg: dict) -> int:
    if not (cfg["webhook"]["enabled"] or cfg["email"]["enabled"]):
        print('No channel enabled. Set "enabled": true in webhook or email in config.json.')
        return 2

    alert = {
        "metric": "test",
        "value": 0,
        "threshold": 0,
        "previous_threshold": 0,
        "message": "SOSMonitor test",
    }
    payload = build_payload(cfg, collect_metrics(cfg), [alert])
    payload["test"] = True

    _, results = deliver(cfg, payload, force=True)
    failed = False
    for channel, result in results.items():
        print(f"{channel}: {result}")
        failed |= not (result.startswith("HTTP 2") or result.startswith("sent"))
    return 1 if failed else 0


def acquire_lock(cfg: dict):
    """Returns a handle to keep alive, or None if another run holds the lock."""
    handle = open(file_in(cfg, LOCK_FILE), "w")
    try:
        try:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # Windows
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


# ============================================================
# CRON
# ============================================================

def crontab_read() -> Optional[str]:
    """Current crontab text, or None when crontab is unavailable."""
    if not shutil.which("crontab"):
        return None
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def crontab_write(lines: List[str]) -> None:
    text = "\n".join(lines) + ("\n" if lines else "")
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def cmd_install(cfg: dict, config_path: Path) -> int:
    minutes = min(59, max(1, int(cfg["check_interval_minutes"])))
    command = f'"{sys.executable}" "{SCRIPT}" -c "{config_path}" run'

    current = crontab_read()
    if current is None:
        if os.name == "nt":
            task = command.replace('"', '\\"')
            print("No crontab on Windows. Schedule it with Task Scheduler:\n"
                  f'  schtasks /Create /SC MINUTE /MO {minutes} /TN {TASK_NAME} /TR "{task}" /F')
        else:
            print("crontab not available. Add this to your panel's cron manager "
                  f"(every {minutes} minute(s)):\n  {command}")
        return 1

    kept = [line for line in current.splitlines() if CRON_MARK not in line]
    crontab_write(kept + [f"*/{minutes} * * * * {command} >/dev/null 2>&1 {CRON_MARK}"])
    print(f"Cron installed: runs every {minutes} minute(s)")
    return 0


def cmd_uninstall() -> int:
    current = crontab_read()
    if current is None and os.name == "nt":
        print(f"No crontab on Windows. Remove the task with:\n  schtasks /Delete /TN {TASK_NAME} /F")
        return 0
    lines = (current or "").splitlines()
    kept = [line for line in lines if CRON_MARK not in line]

    if len(kept) == len(lines):
        print("No cron entry found")
        return 0

    crontab_write(kept)
    print("Cron entry removed")
    return 0


# ============================================================
# MAIN
# ============================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="sosmonitor", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "test", "install", "uninstall"])
    parser.add_argument("-c", "--config", default=str(SCRIPT.parent / "config.json"),
                        help="path to config.json (default: next to the script)")
    args = parser.parse_args(argv)

    if args.command == "uninstall":
        return cmd_uninstall()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        create_config(config_path)
        print(f"Config created: {config_path}\n"
              "Edit it (server_name, thresholds, webhook, email) and run again.")
        return 0

    cfg = load_config(config_path)

    if args.command == "test":
        return run_test(cfg)
    if args.command == "install":
        return cmd_install(cfg, config_path)

    lock = acquire_lock(cfg)
    if lock is None:
        print("Another SOSMonitor run is active, skipping.", file=sys.stderr)
        return 0
    try:
        run_once(cfg)
    finally:
        if hasattr(lock, "close"):
            lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
