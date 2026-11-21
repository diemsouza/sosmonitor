# SOSMonitor

> Sometimes simple is more than enough.

A tiny server monitor that watches **CPU, memory, disk and load average** and tells you when something crosses a limit, by **webhook** (n8n, Make, Zapier, your own endpoint...) and/or **email**.

No agent to install, no paid monitoring service, no dashboard to maintain, no root. One Python file run by cron. It fits any server that just needs a simple "hey, the disk is at 90%" alert, and it was born on locked-down hosts (Cloudways and similar) where you can't install anything else.

- **One file:** `sosmonitor.py`. Python 3.8+ standard library only (`psutil` is optional). Install it with a single `curl`, or from PyPI with `pipx`.
- **No root:** it is scheduled in your own user's crontab.
- **Self-contained:** it only writes next to its config file (log, state, lock).
- **No alert spam:** you get one alert per level change, with a margin so a value hovering around a threshold doesn't flood your inbox.

## Install

### Single file (main way)

```bash
mkdir ~/sosmonitor && cd ~/sosmonitor
curl -fsSLO https://github.com/diemsouza/sosmonitor/raw/main/sosmonitor.py

python3 sosmonitor.py install    # 1. creates the config in ~/.config/sosmonitor/, schedules cron
                                 #    and creates the `sosmonitor` command
python3 sosmonitor.py config     # 2. opens the config: set server_name, webhook and/or email
python3 sosmonitor.py test       # 3. prints a test alert and sends it to the enabled channels
```

Three steps: `install`, then `config` to edit, then `test` to validate the channels. Only `install` and `config` create a config file; `run` and `test` never do (without one they tell you to run `install`).

`install` can be run as many times as you like: it never duplicates the cron line, recreates the link or rewrites an existing config. It finishes with a summary showing the state of each step (`created`, `ok`, `already`, `manual` or `failed`), for example:

```
Config     created   /Users/me/.config/sosmonitor/config.json
Command    ok        sosmonitor (~/.local/bin/sosmonitor)
Schedule   ok        every 5 minute(s)
```

A step that fails does not stop the others. `install` exits with a non-zero code only when a step failed; a step that needs a manual action (for example a host without `crontab`, where it prints the line to paste in your panel) is marked `manual` and still exits 0.

**macOS:** `crontab` is blocked until your terminal has Full Disk Access. If `install` shows `Schedule   failed    crontab: Operation not permitted`, open System Settings > Privacy & Security > Full Disk Access, enable your terminal, restart it and run `install` again. The config and the command are already in place, so it only schedules cron.

`curl -fsSLO` downloads the file under its own name. The `-L` follows GitHub's redirect from `/raw/` to the raw file. You can also `git clone https://github.com/diemsouza/sosmonitor` and use the script from the clone.

### Python package (pipx, uv or pip)

```bash
pipx install sosmonitor          # or: uv tool install sosmonitor
                                 # or: pip install --user sosmonitor

sosmonitor install               # 1. creates the config, schedules it in cron
sosmonitor config                # 2. opens the config in your editor
sosmonitor test                  # 3. validates the channels
```

Same three steps and the same `install` summary as above (including the macOS note). `install` is safe to run repeatedly, and `run` and `test` never create a config.

The `sosmonitor` command is created automatically by pip, pipx or uv, so `install` does not touch `~/.local/bin` or your shell startup files. On macOS and Windows, CPU and memory need `psutil`, so install it as an extra: `pipx install "sosmonitor[psutil]"` or `pip install "sosmonitor[psutil]"`. On Linux nothing else is needed.

The examples below use `python3 sosmonitor.py ...`. With the package, run `sosmonitor ...` instead.

## Commands

| Command                            | What it does                                                                                                                                        |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python3 sosmonitor.py` (or `run`) | Runs one check. This is what cron calls, and the default with no command. Without a config file it says so, points to `install` and exits non-zero (it never creates one) |
| `python3 sosmonitor.py test`       | Prints a test alert on screen (title and body, as in the email), sends it to every enabled channel and shows the result. Never creates a config (without one it points to `install`) |
| `python3 sosmonitor.py config`     | Creates the config if missing and opens it in `$VISUAL`, `$EDITOR`, `nano` or `vi` (`notepad` on Windows). Checks the JSON when the editor closes and offers to reopen it once. Without a terminal or an editor, prints the path and a command to copy |
| `python3 sosmonitor.py install`    | Creates the config if missing, adds a crontab entry that runs every `check_interval_minutes` and creates the `sosmonitor` command (Linux/macOS), unless a package install already provides it. Safe to run again. Runs every step even if one fails and prints a summary of each; exits non-zero only if a step `failed`. `--no-shortcut` skips the command |
| `python3 sosmonitor.py uninstall`  | Removes that crontab entry, the `sosmonitor` command and its PATH lines, and prints a summary of each step (non-zero exit only if a step `failed`) |
| `python3 sosmonitor.py --version`  | Prints the installed version. See the [CHANGELOG](CHANGELOG.md) for what changed in each version                                                    |

### Where the config lives

| Install                      | Config file                                                                          |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| New install (`curl` or package) | `~/.config/sosmonitor/config.json` (or `$XDG_CONFIG_HOME/sosmonitor/`), `%APPDATA%\SOSMonitor\config.json` on Windows |
| Existing single-file install | `config.json` next to `sosmonitor.py`, if it is already there                         |
| Any, with `-c /path/to.json` | That file. `-c` overrides everything                                                  |

The order is: `-c`, then a `config.json` next to the script if it already exists (so every existing install keeps working as is), otherwise the user config folder. `install` (or `config`) creates the file there and tells you where. `run` and `test` never create a config: without one they say so and point to `install`. SOSMonitor never moves or copies an existing file. The user folder survives upgrades, which pip and pipx would otherwise wipe from the script's folder.

**The log, state and lock files always live next to the config file in use**, wherever that is. It doesn't matter which directory you run the command from.

```bash
python3 sosmonitor.py -c /path/to/other.json            # run with that config
python3 sosmonitor.py -c /path/to/other.json test
python3 sosmonitor.py -c /path/to/other.json install    # creates it if missing; the cron line keeps the -c path
```

### The `sosmonitor` command

With the single-file install on Linux and macOS, `install` also creates the `sosmonitor` command (a link at `~/.local/bin/sosmonitor`) and adds that folder to your `PATH` in your shell startup files, so you never edit `.bashrc` yourself. Open a new terminal session to use it, or run the `export` line it prints. If your host's shell doesn't load startup files, `install` says so in its summary, and the full path it prints (`~/.local/bin/sosmonitor`) always works. The link is followed, so `config.json`, the log and the state stay next to the real file. Use `install --no-shortcut` to only schedule cron. If a package install (pip, pipx, uv) already put `sosmonitor` on your `PATH`, `install` marks the command `already`: it creates no link and touches no startup file, and the cron line runs that executable. `uninstall` only removes what `install` created.

On Windows, create `sosmonitor.cmd` in a folder on your `PATH` with this single line:

```bat
@py "C:\path\to\sosmonitor.py" %*
```

## config.json

Created by `install` or `config` (with `chmod 600`, since it holds passwords and tokens). Shown here with the email fields filled in:

```json
{
  "server_name": "MY-SERVER",
  "check_interval_minutes": 1,
  "disk_path": "/",
  "send_every_check": true,
  "sustain_checks": 1,
  "thresholds": {
    "70": "{metric} at {value}%",
    "80": "{metric} at {value}% (WARNING)",
    "90": "{metric} at {value}% (CRITICAL)",
    "100": "{metric} at {value}% (AT LIMIT)"
  },
  "recovery_message": "{metric} at {value}% (BACK TO NORMAL)",
  "top_processes": {
    "enabled": true,
    "count": 3
  },
  "webhook": {
    "enabled": false,
    "url": "",
    "token": "",
    "token_header": "X-Monitor-Token"
  },
  "email": {
    "enabled": false,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "username": "",
    "password": "",
    "from": "",
    "to": ["you@example.com"]
  }
}
```

| Key                      | What it does                                                                                                                                                                                          |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server_name`            | Name shown in alerts and in the email subject                                                                                                                                                         |
| `check_interval_minutes` | How often cron runs the check (used by `install`, 1 to 59)                                                                                                                                            |
| `disk_path`              | Partition to measure (`/`, `/home`, `C:\` ...)                                                                                                                                                        |
| `send_every_check`       | `true`: the webhook receives the metrics on every check. `false`: only when there is an alert. Email is only ever sent for alerts                                                                     |
| `sustain_checks`         | How many consecutive checks a metric must stay at or above a level before the alert is sent. `1` (default) alerts on the first check. Raise it (e.g. `3`) on noisy servers. Recovery is never delayed                |
| `thresholds`             | Levels in % and the message for each one. They apply to CPU, memory and disk. Your object **replaces** the default list, so include every level you want (e.g. add `"50"` and keep `"70"`, `"80"`...) |
| `recovery_message`       | Sent when a metric goes back below the lowest level                                                                                                                                                   |
| `top_processes`          | `enabled` (default `true`) and `count` (default `3`): list the top CPU or memory consumers in the alert, only when that metric alerts                                                                                  |
| `webhook`                | `url` receives a JSON `POST`; `token`, if set, goes in the header named by `token_header` (default `X-Monitor-Token`; change it to e.g. `X-Token` or `Authorization` if your service requires)        |
| `email`                  | SMTP settings. Port 465 uses SSL; other ports use STARTTLS when the server offers it. For Gmail, use an [app password](https://myaccount.google.com/apppasswords). `to` is a list                     |

In messages, `{metric}` (CPU, Memory, Disk), `{value}` (rounded %) and `{server}` are replaced. Messages can be in any language. Set `"enabled": false` to turn a channel off without deleting its settings.

## How alerts work

SOSMonitor remembers the last level of each metric (in `sosmonitor_state.json`) and **only sends an alert when that level changes**:

- **Going up is immediate.** A value of 72% puts you at level 70, and a value of 85% at level 80.
- **Going down needs a 5-point margin.** A level is kept until the value falls 5 points below it. This keeps a value that hovers around a threshold from sending an alert/recovery pair on every check.
- **No repeats.** Staying at the same level for hours sends nothing.
- **CPU is an average, not a snapshot.** Each check reports the average CPU use since the previous check (SOSMonitor keeps the CPU counters in the state file). That is why it matches the graph in your host's panel, and why a short burst of another job at the moment cron fires doesn't trigger a false alert. The first check after install, or after a reboot, takes a 1-second sample instead.
- **Optionally, alert only when it persists.** With `"sustain_checks": 3`, a metric has to be at or above a level for 3 checks in a row before the alert goes out. The default is `1`, because the interval average already removes the spikes; use it as a knob for noisy servers.

Example with levels 70 and 80:

| Check | Value | Level | Alert                             |
| ----- | ----- | ----- | --------------------------------- |
| 1     | 65%   | 0     | –                                 |
| 2     | 71%   | 70    | **"CPU at 71%"**                  |
| 3     | 69%   | 70    | – (still within 70 − 5)           |
| 4     | 72%   | 70    | –                                 |
| 5     | 83%   | 80    | **"CPU at 83% (WARNING)"**        |
| 6     | 77%   | 80    | – (still within 80 − 5)           |
| 7     | 74%   | 70    | **"CPU at 74%"**                  |
| 8     | 64%   | 0     | **"CPU at 64% (BACK TO NORMAL)"** |

### The alert message

The title shows **only the most severe alert** (highest level, ties broken by the highest value), plus a counter if there are others. With CPU at 75% and memory at 85%:

```
[SOSMonitor] MY-SERVER: Memory at 85% (WARNING) (+1 more)

Server: MY-SERVER
Time: 2026-11-20T18:05:03+00:00

Alerts:
  - Memory at 85% (WARNING)
  - CPU at 75%

Top processes by memory:
  1. php-fpm  8.2% (1.3 GB)
  2. mysqld   6.1% (1.0 GB)
  3. node     2.4% (395 MB)

Current metrics:
  CPU 75% | MEMORY 85% | DISK 54%
```

The top processes are looked up only when a CPU or memory alert goes out (never on quiet checks, never for disk). Memory ranks by current RSS; CPU is measured over one second. Only the command name is shown, never its arguments, since they can contain passwords. On hosts that hide other users' processes the list may be short or missing, and the alert is sent anyway.

Also:

- Every alert goes to every enabled channel.
- If every channel fails, the alert is not marked as sent, and the next check tries again.
- Load average is collected and sent, but has no thresholds (it isn't a percentage).

### Webhook payload

```json
{
  "server": "MY-SERVER",
  "hostname": "web-01",
  "platform": "Linux-5.15.0-x86_64-with-glibc2.35",
  "timestamp": "2026-11-19T20:15:00-03:00",
  "metrics": {
    "cpu": { "percent": 83.0 },
    "memory": {
      "percent": 61.2,
      "total_bytes": 4100000000,
      "available_bytes": 1590000000,
      "used_bytes": 2510000000
    },
    "disk": {
      "percent": 47.9,
      "total_bytes": 80000000000,
      "used_bytes": 38300000000,
      "free_bytes": 41700000000,
      "path": "/"
    },
    "load": {
      "1m": 1.2,
      "5m": 0.9,
      "15m": 0.7,
      "cpu_count": 2,
      "1m_per_cpu": 0.6
    }
  },
  "alerts": [
    {
      "metric": "cpu",
      "value": 83.0,
      "threshold": 80,
      "previous_threshold": 70,
      "message": "CPU at 83% (WARNING)"
    }
  ],
  "top_processes": {
    "cpu": [
      { "name": "php-fpm", "percent": 62.5 },
      { "name": "mysqld", "percent": 14.0 }
    ]
  },
  "message_title": "[SOSMonitor] MY-SERVER: CPU at 83% (WARNING)",
  "message_body": "Server: MY-SERVER\nTime: 2026-11-19T20:15:00-03:00\n\nAlerts:\n  - CPU at 83% (WARNING)\n\nTop processes by CPU:\n  1. php-fpm  62.5%\n  2. mysqld   14.0%\n\nCurrent metrics:\n  CPU 83% | MEMORY 61% | DISK 48% | LOAD 1m=1.2 5m=0.9 15m=0.7 CPUs=2\n"
}
```

`message_title` and `message_body` are the same text as the email subject and body, ready to forward unchanged to WhatsApp, Telegram, Slack or any chat tool. On checks without alerts (sent only when `send_every_check` is `true`), `alerts` is an empty list and both message fields are empty strings, so in n8n you can filter on `message_title != ""`. `top_processes` (`cpu` and/or `memory` lists; memory entries also have `rss_bytes`) is present only when there is something to report. The `test` command fills the message fields and adds `"test": true`.

## Platforms

|             | CPU / memory           | Disk / load                    | Scheduling                                                     | Dependencies                                      |
| ----------- | ---------------------- | ------------------------------ | -------------------------------------------------------------- | ------------------------------------------------- |
| **Linux**   | Native (reads `/proc`) | Native                         | User crontab                                                   | None                                              |
| **macOS**   | Needs `psutil`         | Native                         | User crontab                                                   | `pip install "sosmonitor[psutil]"` or `pip3 install psutil` |
| **Windows** | Needs `psutil`         | Disk native, load via `psutil` | Task Scheduler: `install` prints the `schtasks` command to run | `pip install "sosmonitor[psutil]"` or `pip install psutil`  |

When `psutil` is installed, SOSMonitor uses it on every platform. Without it, missing metrics are simply skipped.

Alpine and other musl or busybox hosts work with no dependency at all: everything, including the top processes list, is read straight from `/proc`, without needing `ps`.

**Docker:** SOSMonitor is not meant to run inside a container yet. There `/proc` reports the CPU, memory and load of the host, not the container's cgroup limits, so the numbers would describe the host and not your container. Run it on the host instead.

**Windows has not been tested.** It should work (locking uses `msvcrt`, and `install`/`uninstall` print the `schtasks` commands instead of editing crontab). Reports and fixes are welcome.

If your host has no `crontab` command but has a cron panel (common on managed hosting), `install` marks the schedule `manual`, prints the exact command to paste there and still exits 0.

On macOS, `crontab` also needs Full Disk Access for your terminal (see [Install](#install)).

## Files created (always next to the config in use)

| File                    | Content                                                                                                                             |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `sosmonitor.log`        | One JSON line per check (metrics, alerts, delivery result). Rotates to `.log.1` at 5 MB, keeping one old file (about 10 MB at most) |
| `sosmonitor_state.json` | Last level of each metric, the `sustain_checks` counters and the CPU counters used for the next average                           |
| `.sosmonitor.lock`      | Prevents two checks from running at the same time                                                                                   |

## Updating

**Package install:** `pipx upgrade sosmonitor` (or `uv tool upgrade sosmonitor`, or `pip install --user -U sosmonitor`), then `sosmonitor --version` and `sosmonitor test`. Your config, log and state live in the user config folder, so the upgrade doesn't touch them. Cron keeps working because it points to the `sosmonitor` executable.

**Single file:** SOSMonitor is a single file, so updating is just downloading it again:

```bash
cd ~/sosmonitor                 # the folder where sosmonitor.py lives
cp sosmonitor.py sosmonitor.py.bak
curl -fsSLO https://github.com/diemsouza/sosmonitor/raw/main/sosmonitor.py
python3 sosmonitor.py --version
python3 sosmonitor.py test
```

To get a specific version instead of the latest, download `sosmonitor.py` from that version on the [Releases](https://github.com/diemsouza/sosmonitor/releases) page (or `pipx install sosmonitor==VERSION`), where each release also lists its changes.

**What is not touched:** `config.json`, the log, the state file and the lock. They are separate files. Cron and the `sosmonitor` command keep working because they point to the same `sosmonitor.py` (or to the package executable).

**What to check after updating:**

1. Read the [CHANGELOG](CHANGELOG.md) for the versions between yours and the new one. Each entry has a "Config changes" line (renamed, removed or new keys).
2. Keys missing from your `config.json` use the default value, so a new option never breaks an old config. To use a new option, add it by hand using `config.example.json` as reference (`sosmonitor config` opens the file).
3. Run `python3 sosmonitor.py test` to confirm the channels still work.
4. Run `python3 sosmonitor.py install` again only if the CHANGELOG says the cron entry or the command changed. It is safe to run and never overwrites `config.json`.

**Rolling back (single file):** `mv sosmonitor.py.bak sosmonitor.py`. For a package, install the previous version: `pipx install --force sosmonitor==VERSION`.

## Development

```bash
python3 -m unittest discover tests
```

The tests use only the standard library and never touch your real `config.json`.

## License

[MIT](LICENSE)
