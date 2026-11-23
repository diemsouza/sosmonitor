#!/usr/bin/env python3
"""
SOSMonitor: lightweight server monitor for hosts without root.

Measures CPU, memory, disk, load average and uptime and sends alerts by webhook
and/or email when a threshold is crossed. Standard library only; psutil is
used when installed.

    python3 sosmonitor.py              one check (needs a config: run `install` first)
    python3 sosmonitor.py test         print a test alert and send it to the enabled channels
    python3 sosmonitor.py config       open the config in your editor (creates it if missing)
    python3 sosmonitor.py install      schedule in crontab and create the `sosmonitor` command
                                       (--no-shortcut: only the crontab)
    python3 sosmonitor.py uninstall    remove from crontab and remove the command
    python3 sosmonitor.py --version    print the version
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import site
import smtplib
import socket
import ssl
import subprocess
import sys
import sysconfig
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

VERSION = "0.4.0"
SCRIPT = Path(__file__).resolve()
CRON_MARK = "# sosmonitor"
LOG_FILE = "sosmonitor.log"
STATE_FILE = "sosmonitor_state.json"
LOCK_FILE = ".sosmonitor.lock"
PROC = "/proc"  # Linux process info; tests point it at a fake tree
LOG_MAX_BYTES = 5_000_000
TIMEOUT = 15
METRIC_LABELS = {"cpu": "CPU", "memory": "Memory", "disk": "Disk", "reboot": "Reboot"}
HYSTERESIS = 5  # points a value must fall below a level before it drops
BOOT_TOLERANCE = 120  # seconds boot_time may drift (NTP, sleep) without being a reboot
TASK_NAME = "SOSMonitor"  # Windows Task Scheduler
TOP_TIMEOUT = 5  # seconds a top-processes lookup may take, at most
TOP_NAME_WIDTH = 20

DEFAULTS: Dict[str, Any] = {
    "server_name": "SOSMONITOR",
    "check_interval_minutes": 1,
    "disk_path": "/",
    "send_every_check": True,
    "sustain_checks": 1,
    "thresholds": {
        "70": "{metric} at {value}%",
        "80": "{metric} at {value}% (WARNING)",
        "90": "{metric} at {value}% (CRITICAL)",
        "100": "{metric} at {value}% (AT LIMIT)",
    },
    "recovery_message": "{metric} at {value}% (BACK TO NORMAL)",
    "reboot_message": "{server} restarted (up for {value})",
    "top_processes": {"enabled": True, "count": 3},
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
    """Read-only: keys missing from the file fall back to DEFAULTS, and the
    file is never rewritten, so an old config keeps working after an update."""
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
# METRICS BACKEND (everything that differs with psutil or /proc)
# ============================================================
# Each public function picks psutil when installed, else reads /proc (Linux).
# To support a new backend, change only this section.

def _psutil_cpu_counters() -> Optional[Tuple[float, float]]:
    try:
        times = psutil.cpu_times()
        # guest times are already counted inside user/nice
        total = sum(v for k, v in times._asdict().items() if not k.startswith("guest"))
        idle = times.idle + getattr(times, "iowait", 0.0)
        return total - idle, total
    except (OSError, AttributeError):
        return None


def _proc_cpu_counters() -> Optional[Tuple[float, float]]:
    try:
        with open(f"{PROC}/stat", encoding="utf-8") as file:
            values = [int(x) for x in file.readline().split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values[:8])
        return total - idle, total
    except (OSError, ValueError, IndexError):
        return None


def read_cpu_counters() -> Optional[Tuple[float, float]]:
    """Cumulative (busy, total) CPU time of the whole system, or None."""
    return _psutil_cpu_counters() if psutil else _proc_cpu_counters()


def _psutil_memory() -> dict:
    memory = psutil.virtual_memory()
    return {
        "percent": round(memory.percent, 1),
        "total_bytes": memory.total,
        "available_bytes": memory.available,
        "used_bytes": memory.used,
    }


def _proc_memory() -> Optional[dict]:
    try:
        info: Dict[str, int] = {}
        with open(f"{PROC}/meminfo", encoding="utf-8") as file:
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


def get_memory_usage() -> Optional[dict]:
    return _psutil_memory() if psutil else _proc_memory()


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


def _proc_uptime() -> Optional[dict]:
    try:
        with open(f"{PROC}/uptime", encoding="utf-8") as file:
            seconds = float(file.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    try:
        with open(f"{PROC}/sys/kernel/random/boot_id", encoding="utf-8") as file:
            boot_id = file.read().strip() or None
    except OSError:
        boot_id = None
    return {"seconds": int(seconds), "boot_time": int(time.time() - seconds), "boot_id": boot_id}


def _psutil_uptime() -> Optional[dict]:
    try:
        boot_time = psutil.boot_time()
    except (OSError, AttributeError):
        return None
    return {"seconds": max(0, int(time.time() - boot_time)), "boot_time": int(boot_time),
            "boot_id": None}


def get_uptime() -> Optional[dict]:
    """Linux reads /proc (its boot_id changes on every boot); elsewhere psutil."""
    return _proc_uptime() or (_psutil_uptime() if psutil else None)


def _psutil_top_memory(deadline: float) -> Tuple[List[Tuple[str, int]], int]:
    """([(name, rss_bytes)], total memory in bytes)."""
    rows: List[Tuple[str, int]] = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        if time.monotonic() > deadline:
            break
        info = proc.info
        if info.get("memory_info") is not None and _clean_name(info.get("name")):
            rows.append((_clean_name(info["name"]), info["memory_info"].rss))
    return rows, psutil.virtual_memory().total


def _proc_rss() -> Dict[int, Tuple[str, int]]:
    """{pid: (name, resident bytes)} from /proc (Linux). Needs no `ps`, so it
    also works on busybox/Alpine. Empty when /proc is missing."""
    rss: Dict[int, Tuple[str, int]] = {}
    try:
        entries = os.listdir(PROC)
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError):
        return rss
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"{PROC}/{entry}/statm", encoding="utf-8") as file:
                pages = int(file.read().split()[1])
            with open(f"{PROC}/{entry}/stat", encoding="utf-8", errors="replace") as file:
                text = file.read()
            rss[int(entry)] = (text[text.index("(") + 1:text.rindex(")")], pages * page_size)
        except (OSError, ValueError, IndexError):
            continue  # process gone or not readable
    return rss


def _proc_top_memory(deadline: float) -> Tuple[List[Tuple[str, int]], int]:
    rows: List[Tuple[str, int]] = []
    for name, size in _proc_rss().values():
        if time.monotonic() > deadline:
            break
        if _clean_name(name):
            rows.append((_clean_name(name), size))
    return rows, (get_memory_usage() or {}).get("total_bytes", 0)


def top_memory(count: int) -> List[dict]:
    """Largest processes by RSS (instantaneous)."""
    deadline = time.monotonic() + TOP_TIMEOUT
    rows, total = (_psutil_top_memory if psutil else _proc_top_memory)(deadline)
    if not total:
        return []
    rows.sort(key=lambda row: row[1], reverse=True)
    return [{"name": name, "percent": round(100.0 * rss / total, 1), "rss_bytes": rss}
            for name, rss in rows[:count]]


def _proc_cpu_ticks() -> Dict[int, Tuple[str, int]]:
    """{pid: (name, utime+stime in clock ticks)} from /proc (Linux)."""
    ticks: Dict[int, Tuple[str, int]] = {}
    for entry in os.listdir(PROC):
        if not entry.isdigit():
            continue
        try:
            with open(f"{PROC}/{entry}/stat", encoding="utf-8", errors="replace") as file:
                text = file.read()
            name = text[text.index("(") + 1:text.rindex(")")]
            fields = text[text.rindex(")") + 2:].split()
            ticks[int(entry)] = (name, int(fields[11]) + int(fields[12]))
        except (OSError, ValueError, IndexError):
            continue  # process gone or not readable
    return ticks


def _psutil_top_cpu(deadline: float) -> List[Tuple[str, float]]:
    rows: List[Tuple[str, float]] = []
    procs = []
    for proc in psutil.process_iter(["name"]):
        try:
            proc.cpu_percent(None)
            procs.append(proc)
        except psutil.Error:
            continue
    time.sleep(1)
    for proc in procs:
        if time.monotonic() > deadline:
            break
        try:
            rows.append((_clean_name(proc.info.get("name")), proc.cpu_percent(None)))
        except psutil.Error:
            continue
    return rows


def _proc_top_cpu() -> List[Tuple[str, float]]:
    first = _proc_cpu_ticks()
    started = time.monotonic()
    time.sleep(1)
    second = _proc_cpu_ticks()
    elapsed = max(time.monotonic() - started, 0.001)
    hertz = os.sysconf("SC_CLK_TCK")
    return [(_clean_name(name), 100.0 * (ticks - first[pid][1]) / hertz / elapsed)
            for pid, (name, ticks) in second.items() if pid in first]


def top_cpu(count: int) -> List[dict]:
    """Biggest CPU consumers over a 1-second window (not `ps %cpu`, which is a
    lifetime average)."""
    deadline = time.monotonic() + TOP_TIMEOUT
    rows = _psutil_top_cpu(deadline) if psutil else _proc_top_cpu()
    rows = [(name, percent) for name, percent in rows if name and percent > 0]
    rows.sort(key=lambda row: row[1], reverse=True)
    return [{"name": name, "percent": round(percent, 1)} for name, percent in rows[:count]]


# ============================================================
# METRICS
# ============================================================

def _valid_sample(sample: Any) -> bool:
    return (isinstance(sample, dict)
            and all(isinstance(sample.get(key), (int, float)) for key in ("busy", "total")))


def measure_cpu(previous: Optional[dict]) -> Tuple[Optional[float], Optional[dict]]:
    """
    CPU % averaged over the whole time since the previous run, from cumulative
    counters kept in the state file. Without a usable previous sample (first
    run, deleted state, counters reset by a reboot) it falls back to a 1-second
    sample. Returns (percent, new sample); (None, None) when CPU is unavailable.
    """
    current = read_cpu_counters()
    if current is None:
        return None, None
    sample = {"busy": current[0], "total": current[1], "time": time.time()}

    if _valid_sample(previous):
        busy = current[0] - previous["busy"]
        total = current[1] - previous["total"]
        if total > 0 and busy >= 0:
            return round(min(100.0, 100.0 * busy / total), 1), sample

    time.sleep(1)
    second = read_cpu_counters()
    if second is None:
        return None, None
    busy, total = second[0] - current[0], second[1] - current[1]
    percent = round(min(100.0, max(0.0, 100.0 * busy / total)), 1) if total > 0 else 0.0
    return percent, {"busy": second[0], "total": second[1], "time": time.time()}


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


def collect_metrics(cfg: dict, cpu: Optional[float] = None) -> dict:
    readings = {
        "cpu": {"percent": cpu} if cpu is not None else None,
        "memory": get_memory_usage(),
        "disk": get_disk_usage(cfg["disk_path"]),
        "load": get_load_average(),
        "uptime": get_uptime(),
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


def _valid_boot(boot: Any) -> Optional[dict]:
    if (isinstance(boot, dict) and isinstance(boot.get("time"), (int, float))
            and isinstance(boot.get("id"), (str, type(None)))):
        return {"id": boot["id"], "time": boot["time"]}
    return None


def load_state(cfg: dict) -> dict:
    """{"levels": ..., "pending": ..., "cpu_sample": ..., "boot": ...}. A state
    file from before 0.2.0 holds only the flat levels; it is read as `levels`."""
    try:
        raw = json.loads(file_in(cfg, STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    if not {"levels", "pending", "cpu_sample", "boot"} & raw.keys():
        raw = {"levels": raw}  # old flat format
    levels = raw.get("levels") if isinstance(raw.get("levels"), dict) else {}
    pending = raw.get("pending") if isinstance(raw.get("pending"), dict) else {}
    return {
        "levels": {k: v for k, v in levels.items() if isinstance(v, int)},
        "pending": {k: v for k, v in pending.items()
                    if isinstance(v, dict) and isinstance(v.get("level"), int)
                    and isinstance(v.get("count"), int)},
        "cpu_sample": raw.get("cpu_sample") if _valid_sample(raw.get("cpu_sample")) else None,
        "boot": _valid_boot(raw.get("boot")),
    }


def save_state(cfg: dict, state: dict) -> None:
    path = file_in(cfg, STATE_FILE)
    tmp = path.with_name(path.name + ".tmp")
    data = {key: value for key, value in state.items() if value is not None}
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as error:
        print(f"state write failed: {error}", file=sys.stderr)


def sustain_checks(cfg: dict) -> int:
    try:
        return max(1, int(cfg["sustain_checks"]))
    except (TypeError, ValueError):
        return 1


def build_alerts(cfg: dict, metrics: dict, previous_state: dict,
                 pending: Optional[dict] = None) -> Tuple[List[dict], dict]:
    """
    `previous_state` is the last level per metric. An alert goes out only when
    a metric changes threshold level. Rising is immediate (or, with
    sustain_checks N > 1, after N consecutive checks at that level or higher,
    counted in `pending`, which is updated in place). A level is kept until the
    value falls HYSTERESIS points below it, so a value hovering around a
    threshold does not flap; dropping is never delayed.
    """
    messages = dict(cfg["thresholds"])
    needed = sustain_checks(cfg)
    if pending is None:
        pending = {}
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

        if current > previous:
            streak = pending.get(name)
            if streak and current >= streak["level"]:
                streak["count"] += 1
            else:
                streak = pending[name] = {"level": current, "count": 1}
            # Kept after firing: if delivery fails the level does not advance
            # and the next check fires again at once. Once it advances,
            # current == previous and the entry is dropped below.
            if streak["count"] < needed:
                current = previous
        else:
            pending.pop(name, None)

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


def check_reboot(cfg: dict, previous: Optional[dict],
                 metrics: dict) -> Tuple[Optional[dict], Optional[dict]]:
    """
    (alert, boot to remember). The first run only records the boot, so an
    update never sends a false reboot alert. Linux compares boot_id exactly;
    elsewhere boot_time must move by more than BOOT_TOLERANCE seconds. With no
    uptime reading (macOS/Windows without psutil) nothing happens.
    """
    uptime = metrics.get("uptime")
    if not uptime:
        return None, previous
    current = {"id": uptime.get("boot_id"), "time": uptime["boot_time"]}
    if previous is None:
        return None, current

    if previous["id"] and current["id"]:
        rebooted = previous["id"] != current["id"]
    else:
        rebooted = abs(current["time"] - previous["time"]) > BOOT_TOLERANCE
    if not rebooted:
        return None, current

    seconds = uptime["seconds"]
    return {
        "metric": "reboot",
        "value": seconds,
        "threshold": 0,
        "previous_threshold": 0,
        "message": format_message(cfg["reboot_message"], cfg["server_name"], "reboot",
                                  human_uptime(seconds)),
    }, current


# ============================================================
# TOP PROCESSES (only collected when there is an alert)
# ============================================================

def _clean_name(name: Any) -> str:
    """Command name only, never arguments (they can hold passwords and tokens)."""
    parts = os.path.basename(str(name or "").strip()).split()
    return parts[0][:TOP_NAME_WIDTH] if parts else ""


def collect_top_processes(cfg: dict, alerts: List[dict]) -> List[dict]:
    """Top consumers for the metrics that alerted (CPU and memory only), as one
    flat list with a `metric` on each row. Every failure is swallowed: the
    alert goes out without the list."""
    settings = cfg["top_processes"]
    if not settings.get("enabled") or not alerts:
        return []
    try:
        count = max(1, int(settings.get("count", 3)))
    except (TypeError, ValueError):
        count = 3

    finders = {"cpu": top_cpu, "memory": top_memory}
    found: List[dict] = []
    done = set()
    for alert in alerts:
        name = alert["metric"]
        if name in finders and alert["threshold"] > 0 and name not in done:
            done.add(name)
            try:
                rows = finders[name](count)
            except Exception:  # hidepid, no ps, permissions, anything
                rows = []
            found += [{"metric": name, **row} for row in rows]
    return found


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


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def top_process_lines(top: List[dict]) -> List[str]:
    groups: Dict[str, List[dict]] = {}
    for row in top:
        groups.setdefault(row["metric"], []).append(row)
    lines: List[str] = []
    for metric, rows in groups.items():
        lines += ["", f"Top processes by {'CPU' if metric == 'cpu' else 'memory'}:"]
        width = max(len(row["name"]) for row in rows)
        for number, row in enumerate(rows, 1):
            detail = f"{row['percent']}%"
            if "rss_bytes" in row:
                detail += f" ({human_bytes(row['rss_bytes'])})"
            lines.append(f"  {number}. {row['name']:<{width}}  {detail}")
    return lines


def build_message(payload: dict) -> Tuple[str, str]:
    """
    Human-readable (title, body) of the alerts; ("", "") when there are none.
    The ONLY place this text is built: email, webhook message fields and `test`.
    The title names just the most severe alert (highest level, then highest
    value) plus a "(+N more)" counter; the body lists them all.
    """
    alerts = payload["alerts"]
    if not alerts:
        return "", ""

    worst = max(alerts, key=lambda a: (a["threshold"], a["value"]))
    title = f"[SOSMonitor] {payload['server']}: {worst['message']}"
    if len(alerts) > 1:
        title += f" (+{len(alerts) - 1} more)"

    lines = [f"Server: {payload['server']}", f"Time: {payload['timestamp']}",
             "", "Alerts:"]
    lines += [f"  - {alert['message']}" for alert in alerts]
    lines += top_process_lines(payload.get("top_processes", []))
    lines += ["", "Current metrics:", "  " + " | ".join(metric_items(payload["metrics"]))]
    return title, "\n".join(lines) + "\n"


def send_email(email: dict, payload: dict) -> Tuple[bool, str]:
    recipients = email["to"] if isinstance(email["to"], list) else [email["to"]]
    recipients = [r for r in recipients if r]
    sender = email["from"] or email["username"]

    if not email["smtp_host"] or not recipients or not sender:
        return False, "smtp not configured (smtp_host, from/username and to)"

    subject, body = build_message(payload)
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


def human_uptime(seconds: Any) -> str:
    """3d 4h 12m; leading zero units are dropped, under a minute is 0m."""
    minutes = max(0, int(seconds)) // 60
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = ([f"{days}d"] if days else []) + ([f"{hours}h"] if days or hours else [])
    return " ".join(parts + [f"{minutes}m"])


def metric_items(metrics: dict) -> List[str]:
    items = []
    for name, data in metrics.items():
        if name == "uptime":
            items.append(f"UPTIME {human_uptime(data['seconds'])}")
        elif name == "load":
            items.append(f"LOAD 1m={data['1m']} 5m={data['5m']} "
                         f"15m={data['15m']} CPUs={data['cpu_count']}")
        else:
            items.append(f"{name.upper()} {int(data['percent'] + 0.5)}%")
    return items


def metric_lines(metrics: dict) -> List[str]:
    """One line per metric, for the console summary."""
    lines = []
    for item in metric_items(metrics):
        label, rest = item.split(" ", 1)
        lines.append(f"{label:<8} {rest}")
    return lines


def build_payload(cfg: dict, metrics: dict, alerts: list,
                  top_processes: Optional[List[dict]] = None) -> dict:
    payload = {
        "server": cfg["server_name"],
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "timestamp": now_iso(),
        "metrics": metrics,
        "alerts": alerts,
    }
    if top_processes:
        payload["top_processes"] = top_processes
    payload["message_title"], payload["message_body"] = build_message(payload)
    return payload


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
    state = load_state(cfg)
    cpu_percent, cpu_sample = measure_cpu(state["cpu_sample"])
    metrics = collect_metrics(cfg, cpu_percent)
    pending = dict(state["pending"])
    alerts, levels = build_alerts(cfg, metrics, state["levels"], pending)
    reboot, boot = check_reboot(cfg, state["boot"], metrics)
    if reboot:
        alerts.append(reboot)
    payload = build_payload(cfg, metrics, alerts, collect_top_processes(cfg, alerts))
    delivered, results = deliver(cfg, payload)

    write_log(cfg, {
        "timestamp": payload["timestamp"],
        "server": cfg["server_name"],
        "metrics": metrics,
        "alerts": alerts,
        "delivery": results,
        "duration_seconds": round(time.monotonic() - started, 2),
    })

    # Two write rules: levels and boot advance only if a channel succeeded (a
    # failed alert is retried next run); the CPU sample and pending counters
    # are saved on every run, since they are not tied to delivery.
    save_state(cfg, {
        "levels": levels if delivered else state["levels"],
        "pending": pending,
        "cpu_sample": cpu_sample or state["cpu_sample"],
        "boot": boot if delivered or not reboot else state["boot"],
    })

    print(f"SOSMonitor | {cfg['server_name']} | {payload['timestamp']}")
    for line in metric_lines(metrics):
        print(f"  {line}")
    for alert in alerts:
        print(f"  ALERT: {alert['message']}")
    for line in top_process_lines(payload.get("top_processes", [])):
        print(f"  {line}" if line else "")
    for channel, result in results.items():
        print(f"  {channel}: {result}")


def run_test(cfg: dict) -> int:
    alert = {
        "metric": "test",
        "value": 0,
        "threshold": 0,
        "previous_threshold": 0,
        "message": "SOSMonitor test",
    }
    # Ask for the top lists too, so `test` shows whether they work on this host.
    probe = [{"metric": m, "threshold": 1} for m in ("cpu", "memory")]
    payload = build_payload(cfg, collect_metrics(cfg, measure_cpu(None)[0]), [alert],
                            collect_top_processes(cfg, probe))
    payload["test"] = True

    print(payload["message_title"])
    print()
    print(payload["message_body"], end="")

    if not (cfg["webhook"]["enabled"] or cfg["email"]["enabled"]):
        print('No channel enabled. Set "enabled": true in webhook or email in config.json.')
        return 2

    _, results = deliver(cfg, payload, force=True)
    failed = False
    for channel, result in results.items():
        print(f"{channel}: {result}")
        failed |= not (result.startswith("HTTP 2") or result.startswith("sent"))
    return 1 if failed else 0


# ============================================================
# PLATFORM SERVICES (what depends on the operating system)
# ============================================================
# Config location, lock, scheduler (crontab vs Task Scheduler), the
# `sosmonitor` command (POSIX only) and the config editor.

# --- config location --------------------------------------------------

def installed_as_package() -> bool:
    """True when this file runs from an installed package (pip, pipx, uv)."""
    return any(part in ("site-packages", "dist-packages") for part in SCRIPT.parts)


def user_config_dir() -> Path:
    """Per-user config folder, which survives package upgrades."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "SOSMonitor"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "sosmonitor"


def resolve_config_path(explicit: Optional[str]) -> Path:
    """-c wins; then a config.json next to the script (every existing install
    keeps working); otherwise the user config folder, where `install` and
    `config` create a new one. Nothing is ever moved or copied."""
    if explicit:
        return Path(explicit)
    beside = SCRIPT.parent / "config.json"
    return beside if beside.exists() else user_config_dir() / "config.json"


# --- lock -------------------------------------------------------------

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


# --- step summary (install / uninstall) -------------------------------

# A step is (label, status, detail). Statuses: created, ok, already (nothing
# to do), manual (the user must act, nothing broke) and failed.
Step = Tuple[str, str, str]

MACOS_HINT = ("macOS blocks crontab until your terminal has Full Disk Access.\n"
              "System Settings > Privacy & Security > Full Disk Access, enable\n"
              "your terminal, restart it, then run: sosmonitor {action}")


def print_summary(steps: List[Step]) -> None:
    """One row per step, in columns. Extra detail lines sit under the status."""
    for label, status, detail in steps:
        first, *rest = (detail or "").split("\n")
        print(f"{label:<11}{status:<10}{first}".rstrip())
        for line in rest:
            print(" " * 11 + line)


def exit_code(steps: List[Step]) -> int:
    """Only a real failure is an error. `manual` and `already` are not."""
    return 1 if any(status == "failed" for _, status, _ in steps) else 0


def _tilde(path: Any) -> str:
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


# --- scheduler (crontab; schtasks on Windows) -------------------------

def _is_permission_error(message: str) -> bool:
    lowered = message.lower()
    return "not permitted" in lowered or "permission denied" in lowered


def crontab_read() -> Tuple[str, str]:
    """(state, text). States: "ok" (text is the current crontab, empty when the
    user has none), "unavailable" (no `crontab` command), "denied" (permission
    error, text is its message) and "error" (anything else). Never raises."""
    if not shutil.which("crontab"):
        return "unavailable", ""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except OSError as error:
        return ("denied" if isinstance(error, PermissionError) else "error"), str(error)
    if result.returncode == 0:
        return "ok", result.stdout
    message = result.stderr.strip()
    if _is_permission_error(message):
        return "denied", message
    if not message or "no crontab" in message.lower():  # the user has no crontab yet
        return "ok", ""
    return "error", message


def crontab_write(lines: List[str]) -> Tuple[bool, str]:
    """(success, error message from crontab). Never raises."""
    text = "\n".join(lines) + ("\n" if lines else "")
    try:
        result = subprocess.run(["crontab", "-"], input=text, text=True,
                                capture_output=True)
    except OSError as error:
        return False, str(error)
    if result.returncode == 0:
        return True, ""
    return False, result.stderr.strip() or f"crontab exited with code {result.returncode}"


def crontab_failure(message: str, action: str) -> str:
    """Detail text for a failed crontab call; adds the macOS fix when it applies."""
    message = message[len("crontab: "):] if message.startswith("crontab: ") else message
    detail = f"crontab: {message}"
    if sys.platform == "darwin" and _is_permission_error(message):
        detail += "\n" + MACOS_HINT.format(action=action)
    return detail


def _is_entry_point(path: Path) -> bool:
    try:
        return path.is_file() and path.resolve() != SCRIPT.resolve()
    except OSError:
        return False


def _short_name_works() -> bool:
    """True when `sosmonitor` typed in this shell runs our command: a package
    entry point or our own link, not some other program with that name."""
    found = shutil.which("sosmonitor")
    if not found:
        return False
    try:
        return _is_entry_point(Path(found)) or Path(found).resolve() == SCRIPT.resolve()
    except OSError:
        return False


def find_entry_point() -> Optional[Path]:
    """The `sosmonitor` executable a package install (pip, pipx, uv) created, as
    an absolute path, on PATH or not. None when not running from a package. A
    link that points at this script (the curl-install shortcut) doesn't count."""
    if not installed_as_package():
        return None
    found = shutil.which("sosmonitor")
    if found and _is_entry_point(Path(found)):
        return Path(os.path.abspath(found))
    folders = [Path(sys.executable).parent, Path(site.getuserbase()) /
               ("Scripts" if sys.platform == "win32" else "bin")]
    scripts = sysconfig.get_path("scripts")
    if scripts:
        folders.insert(0, Path(scripts))
    for folder in folders:
        for name in ("sosmonitor", "sosmonitor.exe"):
            if _is_entry_point(folder / name):
                return Path(os.path.abspath(folder / name))
    return None


def cron_command(config_path: Path) -> str:
    """Always absolute paths: cron's PATH can't be relied on."""
    entry = find_entry_point()
    if entry:
        return f'"{entry}" -c "{config_path}" run'
    return f'"{sys.executable}" "{SCRIPT}" -c "{config_path}" run'


def schedule_step(cfg: dict, config_path: Path) -> Tuple[str, str]:
    """Adds (or keeps) the crontab line. Returns (status, detail)."""
    minutes = min(59, max(1, int(cfg["check_interval_minutes"])))
    command = cron_command(config_path)

    state, current = crontab_read()
    if state == "unavailable":
        if os.name == "nt":
            task = command.replace('"', '\\"')
            return "manual", ("no crontab on Windows. Schedule it with Task Scheduler:\n"
                              f'  schtasks /Create /SC MINUTE /MO {minutes} /TN {TASK_NAME} /TR "{task}" /F')
        return "manual", ("crontab not available. Add this to your panel's cron manager "
                          f"(every {minutes} minute(s)):\n  {command}")
    if state != "ok":
        return "failed", crontab_failure(current, "install")

    line = f"*/{minutes} * * * * {command} >/dev/null 2>&1 {CRON_MARK}"
    existing = current.splitlines()
    if [entry for entry in existing if CRON_MARK in entry] == [line]:
        return "already", f"every {minutes} minute(s)"

    kept = [entry for entry in existing if CRON_MARK not in entry]
    written, error = crontab_write(kept + [line])
    if not written:
        return "failed", crontab_failure(error, "install")
    return "ok", f"every {minutes} minute(s)"


def cmd_install(cfg: dict, config_path: Path, shortcut: bool = True,
                created: bool = False, explicit: Optional[str] = None) -> int:
    """Runs every step even if one fails, then prints one summary."""
    steps: List[Step] = [("Config", "created" if created else "already", str(config_path))]
    notes: List[str] = []
    if shortcut and os.name != "nt":
        entry = find_entry_point()
        if entry:  # a package install already provides the command
            steps.append(("Command", "already", f"sosmonitor ({_tilde(entry)})"))
            if not _short_name_works():
                notes.append("`sosmonitor` is installed but your shell can't find it yet. "
                             "Run `pipx ensurepath` and open a new session, or use the "
                             f"full path:\n  {_quote(str(entry))}")
        else:
            status, detail, extra = install_shortcut()
            steps.append(("Command", status, detail))
            notes += extra
    status, detail = schedule_step(cfg, config_path)
    steps.append(("Schedule", status, detail))

    print_summary(steps)
    next_steps = []
    if created:
        next_steps.append(f"Set server_name, webhook and/or email with:\n  "
                          f"{_edit_command(explicit, config_path)}")
        next_steps.append(f"Then check the channels with:\n  "
                          f"{_edit_command(explicit, config_path, 'test')}")
    next_steps += notes
    if any(s == "failed" and "Full Disk Access" not in d for _, s, d in steps):
        next_steps.append("Fix the error above, then run install again:\n  "
                          f"{_edit_command(explicit, config_path, 'install')}")
    if next_steps:
        print("\nNext:\n" + "\n".join(f"- {text}" for text in next_steps))
    return exit_code(steps)


def uninstall_cron() -> Tuple[str, str]:
    """Removes the crontab line. Returns (status, detail)."""
    state, current = crontab_read()
    if state == "unavailable":
        if os.name == "nt":
            return "manual", f"no crontab on Windows. Remove the task with:\n  schtasks /Delete /TN {TASK_NAME} /F"
        return "manual", f"crontab not available. Remove the line ending in `{CRON_MARK}` from your panel's cron manager"
    if state != "ok":
        return "failed", crontab_failure(current, "uninstall")

    lines = current.splitlines()
    kept = [line for line in lines if CRON_MARK not in line]
    if len(kept) == len(lines):
        return "already", "no cron entry found"

    written, error = crontab_write(kept)
    if not written:
        return "failed", crontab_failure(error, "uninstall")
    return "ok", "cron entry removed"


def cmd_uninstall() -> int:
    steps: List[Step] = [("Cron entry",) + uninstall_cron()]
    # With a package entry point and no link of ours, there is nothing we created.
    if os.name != "nt" and not (find_entry_point() and not _own_link()):
        steps += remove_shortcut()
    print_summary(steps)
    if any(s == "failed" and "Full Disk Access" not in d for _, s, d in steps):
        print("\nNext:\n- Fix the error above, then run uninstall again.")
    return exit_code(steps)


# --- the `sosmonitor` command (Linux/macOS, curl install) -------------

STARTUP_FILES = (".bashrc", ".bash_profile", ".profile", ".zshrc")
PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'


def bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def _own_link() -> bool:
    link = bin_dir() / "sosmonitor"
    try:
        return link.is_symlink() and link.resolve() == SCRIPT.resolve()
    except OSError:
        return False


def add_path_block(home: Path) -> List[Path]:
    """Appends the PATH block to every existing startup file (or a new .bashrc)."""
    files = [home / name for name in STARTUP_FILES if (home / name).is_file()]
    files = files or [home / ".bashrc"]
    changed = []
    for path in files:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if any(line.strip() == CRON_MARK for line in text.splitlines()):
            continue
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(f"{text}{CRON_MARK}\n{PATH_LINE}\n", encoding="utf-8")
        changed.append(path)
    return changed


def remove_path_block(home: Path) -> List[Path]:
    """Removes the marker line and the PATH line right after it."""
    changed = []
    for name in STARTUP_FILES:
        path = home / name
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        kept, i = [], 0
        while i < len(lines):
            if lines[i].strip() == CRON_MARK:
                i += 2 if i + 1 < len(lines) and lines[i + 1].strip() == PATH_LINE else 1
                continue
            kept.append(lines[i])
            i += 1
        if len(kept) != len(lines):
            path.write_text("".join(kept), encoding="utf-8")
            changed.append(path)
    return changed


def shell_finds_command() -> bool:
    """Best effort: does a fresh login or interactive bash find `sosmonitor`?"""
    if not shutil.which("bash"):
        return False
    for flag in ("-lc", "-ic"):
        try:
            result = subprocess.run(["bash", flag, "command -v sosmonitor"],
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return True
    return False


def install_shortcut() -> Tuple[str, str, List[str]]:
    """Links ~/.local/bin/sosmonitor to the script and puts that folder on PATH.
    Returns (status, detail, next-step notes)."""
    folder = bin_dir()
    link = folder / "sosmonitor"
    shown = f"sosmonitor ({_tilde(link)})"
    try:
        SCRIPT.chmod(SCRIPT.stat().st_mode | 0o111)
        folder.mkdir(parents=True, exist_ok=True)
        linked = _own_link()
        if not linked:
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                return "manual", (f"{_tilde(link)} exists and is not a link; left it untouched.\n"
                                  f"Run the script with its full path: {SCRIPT}"), []
            link.symlink_to(SCRIPT)
    except OSError as error:
        return "failed", f"could not create {_tilde(link)}: {error}", []

    on_path = {os.path.normpath(os.path.expanduser(entry))
               for entry in os.environ.get("PATH", "").split(os.pathsep) if entry}
    if os.path.normpath(str(folder)) in on_path:
        return ("already" if linked else "ok"), shown, []

    status, detail = ("already" if linked else "ok"), shown
    try:
        changed = add_path_block(Path.home())
    except OSError as error:
        return "manual", (f"{shown}\ncould not update shell startup files: {error}\n"
                          f"Add this line to your startup file yourself: {PATH_LINE}"), []
    if changed:
        status = "ok"
        detail += "\nadded ~/.local/bin to PATH in: " + ", ".join(p.name for p in changed)
    if not shell_finds_command():
        detail += ("\ncould not confirm that a new shell finds `sosmonitor`; this host's shell "
                   f"may not load startup files. Use the full path: {link}")
    return status, detail, ["Open a new session to use `sosmonitor`, or run this to use it right now:\n"
                            f"  {PATH_LINE}"]


def remove_shortcut() -> List[Step]:
    """Removes our link and PATH block. Returns one step for each."""
    link = bin_dir() / "sosmonitor"
    steps: List[Step] = []
    try:
        if _own_link():
            link.unlink()
            steps.append(("Command", "ok", f"removed {_tilde(link)}"))
        else:
            steps.append(("Command", "already", "no link of ours to remove"))
    except OSError as error:
        steps.append(("Command", "failed", f"could not remove {_tilde(link)}: {error}"))
    try:
        changed = remove_path_block(Path.home())
        steps.append(("PATH block", "ok" if changed else "already",
                      "removed from: " + ", ".join(p.name for p in changed) if changed
                      else "no PATH block found"))
    except OSError as error:
        steps.append(("PATH block", "failed", f"could not update shell startup files: {error}"))
    return steps


# --- config editor ----------------------------------------------------

def pick_editor() -> Optional[List[str]]:
    """$VISUAL, then $EDITOR, then nano, then vi (notepad on Windows)."""
    for var in ("VISUAL", "EDITOR"):
        try:
            parts = shlex.split(os.environ.get(var, ""))
        except ValueError:
            continue
        if parts and shutil.which(parts[0]):
            return parts
    for name in (("notepad",) if sys.platform == "win32" else ("nano", "vi")):
        if shutil.which(name):
            return [name]
    return None


def _quote(text: str) -> str:
    return f'"{text}"' if sys.platform == "win32" else shlex.quote(text)


def _config_problem(path: Path) -> Optional[str]:
    """Why the config can't be loaded, or None when it is fine."""
    try:
        load_config(path)
    except SystemExit as error:
        return str(error)
    except (OSError, UnicodeDecodeError) as error:
        return f"cannot read {path}: {error}"
    return None


def cmd_config(path: Path) -> int:
    """Creates the config if missing and opens it in an editor. Without an
    interactive terminal or an editor it only prints the path and a command to
    copy, so it can never hang."""
    if not path.exists():
        create_config(path)
        print(f"Config created: {path}")

    editor = pick_editor()
    shown = editor or ["notepad" if sys.platform == "win32" else "nano"]
    command = " ".join(_quote(part) for part in shown + [str(path)])
    if not (editor and sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"Config file: {path}\nOpen it with:\n  {command}")
        return 0

    for attempt in (1, 2):
        try:
            subprocess.run(editor + [str(path)])
        except OSError as error:
            print(f"Could not start {editor[0]}: {error}\nOpen it with:\n  {command}")
            return 1
        problem = _config_problem(path)
        if problem is None:
            print(f"Config OK: {path}")
            return 0
        print(f"Invalid config: {problem}", file=sys.stderr)
        if attempt == 2:
            break
        try:
            answer = input("Reopen the editor? [Y/n] ")
        except EOFError:
            break
        if answer.strip().lower().startswith("n"):
            break
    return 1


def _edit_command(explicit: Optional[str], path: Path, command: str = "config") -> str:
    """A command as the user can type it (`-c` only when the config is not at
    its default place). Runnable as printed in the current shell."""
    if _short_name_works():
        base = "sosmonitor"
    elif find_entry_point():
        base = _quote(str(find_entry_point()))
    elif _own_link():
        base = _quote(str(bin_dir() / "sosmonitor"))
    else:
        base = f"python3 {_quote(str(SCRIPT))}"
    return f"{base} -c {_quote(str(path))} {command}" if explicit else f"{base} {command}"


# ============================================================
# MAIN
# ============================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="sosmonitor", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "test", "config", "install", "uninstall"])
    parser.add_argument("-c", "--config", default=None,
                        help="path to config.json (default: next to the script, or the "
                             "user config folder when installed as a package)")
    parser.add_argument("--no-shortcut", action="store_true",
                        help="install: don't create the sosmonitor command or touch PATH")
    parser.add_argument("--version", action="version", version=f"SOSMonitor {VERSION}")
    args = parser.parse_args(argv)

    if args.command == "uninstall":
        return cmd_uninstall()

    config_path = resolve_config_path(args.config).resolve()
    if args.command == "config":
        return cmd_config(config_path)
    created = False
    if not config_path.exists():
        if args.command != "install":
            hint = "python3 sosmonitor.py install" if not args.config else \
                f"python3 sosmonitor.py -c {_quote(str(config_path))} install"
            print(f"No config file found. Run: {hint}", file=sys.stderr)
            return 1
        try:
            create_config(config_path)
        except OSError as error:
            print_summary([("Config", "failed", f"{config_path}: {error}")])
            return 1
        created = True

    cfg = load_config(config_path)

    if args.command == "test":
        return run_test(cfg)
    if args.command == "install":
        return cmd_install(cfg, config_path, shortcut=not args.no_shortcut,
                           created=created, explicit=args.config)

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
