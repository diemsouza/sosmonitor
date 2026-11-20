#!/usr/bin/env python3
"""
SOSMonitor: lightweight server monitor for hosts without root.

Measures CPU, memory, disk and load average and sends alerts by webhook
and/or email when a threshold is crossed. Standard library only; psutil is
used when installed.

    python3 sosmonitor.py              one check (creates config.json if missing)
    python3 sosmonitor.py test         print a test alert and send it to the enabled channels
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

VERSION = "0.2.0"
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
# METRICS
# ============================================================

def read_cpu_counters() -> Optional[Tuple[float, float]]:
    """Cumulative (busy, total) CPU time of the whole system, or None."""
    if psutil:
        try:
            times = psutil.cpu_times()
            # guest times are already counted inside user/nice
            total = sum(v for k, v in times._asdict().items() if not k.startswith("guest"))
            idle = times.idle + getattr(times, "iowait", 0.0)
            return total - idle, total
        except (OSError, AttributeError):
            return None

    try:
        with open("/proc/stat", encoding="utf-8") as file:
            values = [int(x) for x in file.readline().split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values[:8])
        return total - idle, total
    except (OSError, ValueError, IndexError):
        return None


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


def collect_metrics(cfg: dict, cpu: Optional[float] = None) -> dict:
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
    """{"levels": ..., "pending": ..., "cpu_sample": ...}. A state file from
    before 0.2.0 holds only the flat levels; it is read as `levels`."""
    try:
        raw = json.loads(file_in(cfg, STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    if "levels" not in raw and "pending" not in raw and "cpu_sample" not in raw:
        raw = {"levels": raw}  # old flat format
    levels = raw.get("levels") if isinstance(raw.get("levels"), dict) else {}
    pending = raw.get("pending") if isinstance(raw.get("pending"), dict) else {}
    return {
        "levels": {k: v for k, v in levels.items() if isinstance(v, int)},
        "pending": {k: v for k, v in pending.items()
                    if isinstance(v, dict) and isinstance(v.get("level"), int)
                    and isinstance(v.get("count"), int)},
        "cpu_sample": raw.get("cpu_sample") if _valid_sample(raw.get("cpu_sample")) else None,
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


# ============================================================
# TOP PROCESSES (only collected when there is an alert)
# ============================================================

def _clean_name(name: Any) -> str:
    """Command name only, never arguments (they can hold passwords and tokens)."""
    parts = os.path.basename(str(name or "").strip()).split()
    return parts[0][:TOP_NAME_WIDTH] if parts else ""


def top_memory(count: int) -> List[dict]:
    """Largest processes by RSS (instantaneous)."""
    deadline = time.monotonic() + TOP_TIMEOUT
    rows: List[Tuple[str, int]] = []

    if psutil:
        total = psutil.virtual_memory().total
        for proc in psutil.process_iter(["name", "memory_info"]):
            if time.monotonic() > deadline:
                break
            info = proc.info
            if info.get("memory_info") is not None and _clean_name(info.get("name")):
                rows.append((_clean_name(info["name"]), info["memory_info"].rss))
    else:
        result = subprocess.run(["ps", "-eo", "pid,comm,%mem,rss", "--sort=-rss"],
                                capture_output=True, text=True, timeout=3)
        if result.returncode != 0:
            return []
        total = (get_memory_usage() or {}).get("total_bytes", 0)
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or not fields[-1].isdigit():
                continue
            name = _clean_name(" ".join(fields[1:-2]))
            if name:
                rows.append((name, int(fields[-1]) * 1024))

    if not total:
        return []
    rows.sort(key=lambda row: row[1], reverse=True)
    return [{"name": name, "percent": round(100.0 * rss / total, 1), "rss_bytes": rss}
            for name, rss in rows[:count]]


def _proc_cpu_ticks() -> Dict[int, Tuple[str, int]]:
    """{pid: (name, utime+stime in clock ticks)} from /proc (Linux)."""
    ticks: Dict[int, Tuple[str, int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as file:
                text = file.read()
            name = text[text.index("(") + 1:text.rindex(")")]
            fields = text[text.rindex(")") + 2:].split()
            ticks[int(entry)] = (name, int(fields[11]) + int(fields[12]))
        except (OSError, ValueError, IndexError):
            continue  # process gone or not readable
    return ticks


def top_cpu(count: int) -> List[dict]:
    """Biggest CPU consumers over a 1-second window (not `ps %cpu`, which is a
    lifetime average)."""
    deadline = time.monotonic() + TOP_TIMEOUT
    rows: List[Tuple[str, float]] = []

    if psutil:
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
    else:
        first = _proc_cpu_ticks()
        started = time.monotonic()
        time.sleep(1)
        second = _proc_cpu_ticks()
        elapsed = max(time.monotonic() - started, 0.001)
        hertz = os.sysconf("SC_CLK_TCK")
        for pid, (name, ticks) in second.items():
            if pid in first:
                rows.append((_clean_name(name), 100.0 * (ticks - first[pid][1]) / hertz / elapsed))

    rows = [(name, percent) for name, percent in rows if name and percent > 0]
    rows.sort(key=lambda row: row[1], reverse=True)
    return [{"name": name, "percent": round(percent, 1)} for name, percent in rows[:count]]


def collect_top_processes(cfg: dict, alerts: List[dict]) -> Dict[str, List[dict]]:
    """Top consumers for the metrics that alerted (CPU and memory only).
    Every failure is swallowed: the alert goes out without the list."""
    settings = cfg["top_processes"]
    if not settings.get("enabled") or not alerts:
        return {}
    try:
        count = max(1, int(settings.get("count", 3)))
    except (TypeError, ValueError):
        count = 3

    finders = {"cpu": top_cpu, "memory": top_memory}
    found: Dict[str, List[dict]] = {}
    for alert in alerts:
        name = alert["metric"]
        if name in finders and alert["threshold"] > 0 and name not in found:
            try:
                rows = finders[name](count)
            except Exception:  # hidepid, no ps, permissions, anything
                rows = []
            if rows:
                found[name] = rows
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


def top_process_lines(top: dict) -> List[str]:
    lines: List[str] = []
    for metric, rows in top.items():
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
    lines += top_process_lines(payload.get("top_processes", {}))
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


def metric_items(metrics: dict) -> List[str]:
    items = []
    for name, data in metrics.items():
        if name == "load":
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
                  top_processes: Optional[dict] = None) -> dict:
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

    # Two write rules: levels advance only if a channel succeeded (a failed
    # alert is retried next run); the CPU sample and pending counters are
    # saved on every run, since they are not tied to delivery.
    save_state(cfg, {
        "levels": levels if delivered else state["levels"],
        "pending": pending,
        "cpu_sample": cpu_sample or state["cpu_sample"],
    })

    print(f"SOSMonitor | {cfg['server_name']} | {payload['timestamp']}")
    for line in metric_lines(metrics):
        print(f"  {line}")
    for alert in alerts:
        print(f"  ALERT: {alert['message']}")
    for line in top_process_lines(payload.get("top_processes", {})):
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


def install_cron(cfg: dict, config_path: Path) -> int:
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


def cmd_install(cfg: dict, config_path: Path, shortcut: bool = True) -> int:
    code = install_cron(cfg, config_path)
    if shortcut and os.name != "nt":
        install_shortcut()
    return code


def uninstall_cron() -> None:
    current = crontab_read()
    if current is None and os.name == "nt":
        print(f"No crontab on Windows. Remove the task with:\n  schtasks /Delete /TN {TASK_NAME} /F")
        return
    lines = (current or "").splitlines()
    kept = [line for line in lines if CRON_MARK not in line]

    if len(kept) == len(lines):
        print("No cron entry found")
        return

    crontab_write(kept)
    print("Cron entry removed")


def cmd_uninstall() -> int:
    uninstall_cron()
    if os.name != "nt":
        remove_shortcut()
    return 0


# ============================================================
# SHORTCUT (the `sosmonitor` command, Linux/macOS)
# ============================================================

STARTUP_FILES = (".bashrc", ".bash_profile", ".profile", ".zshrc")
PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'


def bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


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


def install_shortcut() -> None:
    folder = bin_dir()
    link = folder / "sosmonitor"
    try:
        SCRIPT.chmod(SCRIPT.stat().st_mode | 0o111)
        folder.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            print(f"WARNING: {link} already exists and is not a link; left it untouched. "
                  f"Run the script with its full path: {SCRIPT}")
            return
        link.symlink_to(SCRIPT)
    except OSError as error:
        print(f"WARNING: could not create the sosmonitor command: {error}")
        return

    on_path = {os.path.normpath(os.path.expanduser(entry))
               for entry in os.environ.get("PATH", "").split(os.pathsep) if entry}
    if os.path.normpath(str(folder)) in on_path:
        print("Command installed: sosmonitor")
        return

    try:
        changed = add_path_block(Path.home())
    except OSError as error:
        print(f"WARNING: could not update shell startup files: {error}")
        changed = []
    if changed:
        print("Added ~/.local/bin to PATH in: " + ", ".join(p.name for p in changed))

    if shell_finds_command():
        print(f"Command installed: sosmonitor ({link})")
    else:
        print("WARNING: could not confirm that a new shell finds `sosmonitor`; this host's "
              f"shell may not load startup files. Use the full path instead: {link}")
    print("Open a new session to use `sosmonitor`, or run this to use it right now:\n"
          f"  {PATH_LINE}")


def remove_shortcut() -> None:
    link = bin_dir() / "sosmonitor"
    try:
        if link.is_symlink() and link.resolve() == SCRIPT.resolve():
            link.unlink()
            print(f"Command removed: {link}")
        changed = remove_path_block(Path.home())
    except OSError as error:
        print(f"WARNING: could not remove the sosmonitor command: {error}")
        return
    if changed:
        print("Removed the PATH line from: " + ", ".join(p.name for p in changed))


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
    parser.add_argument("--no-shortcut", action="store_true",
                        help="install: don't create the sosmonitor command or touch PATH")
    parser.add_argument("--version", action="version", version=f"SOSMonitor {VERSION}")
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
        return cmd_install(cfg, config_path, shortcut=not args.no_shortcut)

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
