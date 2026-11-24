# SOSMonitor

[![PyPI](https://img.shields.io/pypi/v/sosmonitor)](https://pypi.org/project/sosmonitor/)
[![Python](https://img.shields.io/pypi/pyversions/sosmonitor)](https://pypi.org/project/sosmonitor/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

> Sometimes simple is more than enough.

A tiny server monitor that watches **CPU, memory, disk, load average and uptime** and tells you when something crosses a limit or the server reboots, by **webhook** (n8n, Make, Zapier, your own endpoint...) and/or **email**.

No agent, no paid service, no dashboard, no root. One Python file run by cron. It fits any server that just needs a simple "hey, the disk is at 90%" alert, and it was born on locked-down hosts (Cloudways and similar).

One file (`sosmonitor.py`), Python 3.8+ standard library only (`psutil` optional), scheduled in your own crontab, writing only next to its config. One alert per level change, with a margin so a value hovering around a threshold doesn't flood your inbox.

## Install

Pick by what your host lets you do: the **package** if you can install software (EC2, a VPS), the **single file** if you can't (managed hosts).

```bash
# Package
sudo apt install pipx        # Debian/Ubuntu. Fedora/Amazon Linux: sudo dnf install pipx
pipx install sosmonitor
pipx ensurepath && exec $SHELL

# Single file (no pip, no root)
mkdir ~/sosmonitor && cd ~/sosmonitor
curl -fsSLO https://github.com/diemsouza/sosmonitor/raw/main/sosmonitor.py
python3 sosmonitor.py install     # this first call replaces `sosmonitor install`
```

Then, either way:

```bash
sosmonitor install           # 1. creates the config, schedules cron, creates the command
sosmonitor config            # 2. opens the config: set server_name, webhook and/or email
sosmonitor test              # 3. prints a test alert and sends it to the enabled channels
```

`install` is safe to run again and ends with a summary of each step (`created`, `ok`, `already`, `manual` or `failed`). On macOS and Windows, CPU and memory need `psutil`: `pipx install "sosmonitor[psutil]"`.

## Commands

| Command | What it does |
| --- | --- |
| `sosmonitor` (or `run`) | One check. This is what cron calls. Never creates a config |
| `sosmonitor test` | Prints a test alert, sends it to every enabled channel and shows the result |
| `sosmonitor config` | Creates the config if missing and opens it in your editor, then checks the JSON |
| `sosmonitor install` | Creates the config if missing, adds the crontab entry and the `sosmonitor` command (`--no-shortcut` skips the command) |
| `sosmonitor uninstall` | Removes the crontab entry, the command and its PATH lines |
| `sosmonitor --version` | Prints the version ([CHANGELOG](CHANGELOG.md)) |

Add `-c /path/to.json` to any command to use another config. Before `install` on a single-file download, use `python3 sosmonitor.py ...`.

## config.json

Created by `install` or `config` (`chmod 600`, since it holds passwords and tokens):

```json
{
  "server_name": "MY-SERVER",
  "check_interval_minutes": 1,
  "disk_path": "/",
  "send_every_check": true,
  "sustain_checks": 1,
  "thresholds": {
    "cpu": {
      "80": "{metric} at {value}%",
      "95": "{metric} at {value}% (CRITICAL)"
    },
    "memory": {
      "80": "{metric} at {value}%",
      "95": "{metric} at {value}% (CRITICAL)"
    },
    "disk": {
      "95": "{metric} at {value}%",
      "98": "{metric} at {value}% (ALMOST FULL)"
    }
  },
  "recovery_message": "{metric} at {value}% (BACK TO NORMAL)",
  "reboot_message": "{server} restarted (up for {value})",
  "top_processes": { "enabled": true, "count": 3 },
  "webhook": { "enabled": false, "url": "", "token": "", "token_header": "X-Monitor-Token" },
  "email": {
    "enabled": false, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
    "username": "", "password": "", "from": "", "to": ["you@example.com"]
  }
}
```

| Key | What it does |
| --- | --- |
| `server_name` | Name shown in alerts and in the email subject |
| `check_interval_minutes` | How often cron runs the check (used by `install`, 1 to 59) |
| `disk_path` | Partition to measure (`/`, `/home`, `C:\` ...) |
| `send_every_check` | `true`: the webhook receives the metrics on every check. `false`: only when there is an alert. Email is only sent for alerts |
| `sustain_checks` | Consecutive checks a metric must stay at or above a level before alerting. `1` (default) alerts at once; try `3` on noisy servers. Recovery is never delayed |
| `thresholds` | Levels in % and the message for each, **separately for `cpu`, `memory` and `disk`**. Each metric has its own complete list; nothing is shared or inherited between them. A metric with no entry never alerts. Your object **replaces** the defaults, so include every metric and level you want. The old flat form (`{"70": "message"}`) still works and applies the same list to all three |
| `recovery_message` | Sent when a metric goes back below the lowest level |
| `reboot_message` | Sent on the first check after the server restarts. `{value}` is the uptime, like `12m` or `3d 4h 12m` |
| `top_processes` | `enabled` and `count`: list the top CPU or memory consumers, only when that metric alerts |
| `webhook` | `url` receives a JSON `POST`. `token`, if set, goes in the header named by `token_header` |
| `email` | SMTP settings. Port 465 uses SSL; other ports use STARTTLS when offered. For Gmail use an [app password](https://myaccount.google.com/apppasswords). `to` is a list |

**Why does disk have higher numbers?** CPU and memory spikes come and go, so 80 is an early heads-up and 95 means act now. Disk grows slowly and almost never comes back down by itself, so levels in between only add emails along the way, and a full disk *is* the failure: ext4 reserves 5% of blocks for root, so applications start breaking before the number reaches 100. 95 and 98 leave room to act without the noise.

Placeholders: `{server}`, `{metric}` (CPU, Memory, Disk) and `{value}` (rounded %, or the uptime in `reboot_message`). Messages can be in any language. Keys missing from your file use their default.

## How alerts work

SOSMonitor remembers the last level of each metric (in `sosmonitor_state.json`) and **only alerts when that level changes**:

- **Going up is immediate.** 83% is level 80, 96% is level 95.
- **Going down needs a 5-point margin**, so a value hovering around a threshold doesn't alternate alerts and recoveries.
- **CPU is an average since the previous check**, which matches your host's graph and ignores a burst from another cron job. The first check after install or a reboot takes a 1-second sample.
- **Reboots** alert once: the first check after the server restarts says it started and for how long. It never fires on the first run after installing or updating.

Example with CPU levels 80 and 95:

| Check | Value | Level | Alert |
| --- | --- | --- | --- |
| 1 | 75% | 0 | – |
| 2 | 81% | 80 | **"CPU at 81%"** |
| 3 | 77% | 80 | – (still within 80 − 5) |
| 4 | 96% | 95 | **"CPU at 96% (CRITICAL)"** |
| 5 | 91% | 95 | – (still within 95 − 5) |
| 6 | 88% | 80 | **"CPU at 88%"** |
| 7 | 70% | 0 | **"CPU at 70% (BACK TO NORMAL)"** |

Load average and uptime are collected and sent on every check but have no thresholds, so they never alert by themselves.

## What is sent

Every alert goes to every enabled channel; if all fail, it is retried on the next check. The title shows **only the most severe alert** plus a counter; the body lists them all:

```
[SOSMonitor] MY-SERVER: Memory at 96% (CRITICAL) (+1 more)

Alerts:
  - Memory at 96% (CRITICAL)
  - CPU at 83%

Top processes by memory:
  1. php-fpm  8.2% (1.3 GB)
  2. mysqld   6.1% (1.0 GB)

Current metrics:
  CPU 83% | MEMORY 96% | DISK 54% | UPTIME 3d 4h 12m
```

Top processes are looked up only when a CPU or memory alert goes out, by command name only, never arguments. If the host hides them, the alert is sent without the list.

The webhook receives this JSON:

```json
{
  "server": "MY-SERVER",
  "hostname": "web-01",
  "timestamp": "2026-11-19T20:15:00-03:00",
  "metrics": {
    "cpu": { "percent": 83.0 },
    "memory": { "percent": 61.2, "total_bytes": 4100000000, "available_bytes": 1590000000, "used_bytes": 2510000000 },
    "disk": { "percent": 47.9, "total_bytes": 80000000000, "used_bytes": 38300000000, "free_bytes": 41700000000, "path": "/" },
    "load": { "1m": 1.2, "5m": 0.9, "15m": 0.7, "cpu_count": 2, "1m_per_cpu": 0.6 },
    "uptime": { "seconds": 273120, "boot_time": 1763400000, "boot_id": "5d0f1c2e-3b4a-4c6d-8e7f-0a1b2c3d4e5f" }
  },
  "alerts": [{ "metric": "cpu", "value": 83.0, "threshold": 80, "previous_threshold": 0,
               "message": "CPU at 83%" }],
  "top_processes": [{ "metric": "cpu", "name": "php-fpm", "percent": 62.5 }],
  "message_title": "[SOSMonitor] MY-SERVER: CPU at 83%",
  "message_body": "Server: MY-SERVER\n..."
}
```

- `message_title` and `message_body` are the email subject and body, ready to forward to WhatsApp, Telegram or Slack. Without alerts, `alerts` is empty and both are `""`, so in n8n you can filter on `message_title != ""`.
- `top_processes` is a flat list with a `metric` (`cpu` or `memory`) on each item, present only when there is something to report. Memory items also have `rss_bytes`.
- A reboot is an alert with `"metric": "reboot"` and `"threshold": 0`; its `value` is the uptime in seconds. `uptime` has no threshold. `boot_id` is `null` where the OS has none.
- The `test` command fills the message fields and adds `"test": true`.

## Platforms

| | CPU / memory | Disk / load | Uptime / reboot alert | Scheduling | Dependencies |
| --- | --- | --- | --- | --- | --- |
| **Linux** | Native (`/proc`) | Native | Native | User crontab | None |
| **macOS** | Needs `psutil` | Native | Needs `psutil` | User crontab | `pipx install "sosmonitor[psutil]"` |
| **Windows** | Needs `psutil` | Disk native, load via `psutil` | Needs `psutil` | Task Scheduler (`install` prints the `schtasks` command) | `pipx install "sosmonitor[psutil]"` |

Without `psutil`, the metrics it provides are skipped and nothing breaks.

## Files created (always next to the config in use)

| File | Content |
| --- | --- |
| `sosmonitor.log` | One JSON line per check (metrics, alerts, delivery result). Rotates to `.log.1` at 5 MB |
| `sosmonitor_state.json` | Last level of each metric, the `sustain_checks` counters, the CPU counters for the next average and the last seen boot (id and time) |
| `.sosmonitor.lock` | Prevents two checks from running at the same time |

## Updating

Package: `pipx upgrade sosmonitor`. Single file: back up and re-download it (`cp sosmonitor.py sosmonitor.py.bak`, then the same `curl -fsSLO` as above). Then run `sosmonitor --version && sosmonitor test`.

Your config, log and state are never touched. Read the [CHANGELOG](CHANGELOG.md) for the versions in between: each entry has a "Config changes" line. New keys work from their default; to use one, add it by hand (`config.example.json` is the reference).

## FAQ

**Where does the config live?** Order: `-c file`, then a `config.json` next to `sosmonitor.py` if one already exists (existing installs keep working), otherwise `~/.config/sosmonitor/config.json` (`$XDG_CONFIG_HOME`, or `%APPDATA%\SOSMonitor` on Windows). The log, state and lock always live next to the config in use. Existing files are never moved or copied, and the user folder survives package upgrades.

**How does the `sosmonitor` command work?** With the single-file install on Linux/macOS, `install` links `~/.local/bin/sosmonitor` to the script and adds that folder to `PATH` in your shell startup files. Open a new session or run the `export` line it prints; the full path it prints always works. If a package install already provides the command, `install` marks it `already` and touches nothing. `uninstall` removes only what `install` created. On Windows, create `sosmonitor.cmd` in a folder on your `PATH` with `@py "C:\path\to\sosmonitor.py" %*`.

**`pip install` says `externally-managed-environment`.** That is PEP 668 on Debian, Ubuntu, Fedora and most current server images. Use `pipx`, a virtualenv, or `pip install --user --break-system-packages sosmonitor`.

**macOS: `crontab: Operation not permitted`.** Give your terminal Full Disk Access (System Settings > Privacy & Security > Full Disk Access), restart it and run `install` again. The config and command are already in place.

**My host has no `crontab`, only a cron panel.** `install` marks the schedule `manual`, prints the exact command to paste in the panel and exits 0.

**Does it run in Docker?** Not yet. Inside a container `/proc` reports the host's CPU, memory and load, not the container's limits, so the numbers would describe the host. Run it on the host.

**Windows, Alpine?** Windows is untested; it should work (`install` prints the `schtasks` commands). Alpine, musl and busybox work with no dependency: everything is read from `/proc`, with no `ps`.

**How do I roll back?** Single file: `mv sosmonitor.py.bak sosmonitor.py`. Package: `pipx install --force sosmonitor==VERSION`. Old versions are on the [Releases](https://github.com/diemsouza/sosmonitor/releases) page. The config, log and state stay as they were.

## Development

```bash
python3 -m unittest discover tests
```

The tests use only the standard library and never touch your real config.

## License

[MIT](LICENSE)
