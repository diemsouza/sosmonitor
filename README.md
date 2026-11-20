# SOSMonitor

> Sometimes simple is more than enough.

A tiny server monitor that watches **CPU, memory, disk and load average** and tells you when something crosses a limit, by **webhook** (n8n, Make, Zapier, your own endpoint...) and/or **email**.

No agent to install, no paid monitoring service, no dashboard to maintain, no root. One Python file run by cron. It fits any server that just needs a simple "hey, the disk is at 90%" alert, and it was born on locked-down hosts (Cloudways and similar) where you can't install anything else.

- **One file:** `sosmonitor.py`. Python 3.8+ standard library only (`psutil` is optional).
- **No root:** it is scheduled in your own user's crontab.
- **Self-contained:** it only writes next to its config file (log, state, lock).
- **No alert spam:** you get one alert per level change, with a margin so a value hovering around a threshold doesn't flood your inbox.

## Quick start

```bash
mkdir ~/sosmonitor && cd ~/sosmonitor
curl -fsSLO https://github.com/diemsouza/sosmonitor/raw/main/sosmonitor.py

python3 sosmonitor.py            # first run: creates config.json and stops
nano config.json                 # set server_name, webhook and/or email
python3 sosmonitor.py test       # prints a test alert and sends it to the enabled channels
python3 sosmonitor.py install    # schedules it in cron and creates the `sosmonitor` command
```

`curl -fsSLO` downloads the file under its own name. The `-L` follows GitHub's redirect from `/raw/` to the raw file. You can also `git clone https://github.com/diemsouza/sosmonitor` and use the script from the clone.

## Commands

| Command                            | What it does                                                                                                                                        |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python3 sosmonitor.py` (or `run`) | Runs one check. If the config file doesn't exist yet, creates it and stops so you can edit it                                                       |
| `python3 sosmonitor.py test`       | Prints a test alert on screen (title and body, as in the email), sends it to every enabled channel and shows the result                             |
| `python3 sosmonitor.py install`    | Adds a crontab entry that runs every `check_interval_minutes` and creates the `sosmonitor` command (Linux/macOS). `--no-shortcut` skips the command |
| `python3 sosmonitor.py uninstall`  | Removes that crontab entry and the `sosmonitor` command                                                                                             |
| `python3 sosmonitor.py --version`  | Prints the installed version. See the [CHANGELOG](CHANGELOG.md) for what changed in each version                                                    |

### Which config file is used

By default, **every command uses `config.json` in the same folder as `sosmonitor.py`**. It doesn't matter which directory you run the command from. So `python3 ~/sosmonitor/sosmonitor.py test` reads `~/sosmonitor/config.json`.

To use a different file, pass `-c` (`--config`) to any command:

```bash
python3 sosmonitor.py -c /path/to/other.json            # run with that config (creates it if missing)
python3 sosmonitor.py -c /path/to/other.json test
python3 sosmonitor.py -c /path/to/other.json install    # the cron line keeps the -c path
```

The log and state files are created next to whichever config file is used.

### The `sosmonitor` command

On Linux and macOS, `install` also creates the `sosmonitor` command (a link at `~/.local/bin/sosmonitor`) and adds that folder to your `PATH` in your shell startup files, so you never edit `.bashrc` yourself. Open a new terminal session to use it, or run the `export` line it prints. If your host's shell doesn't load startup files, `install` warns you, and the full path it prints (`~/.local/bin/sosmonitor`) always works. The link is followed, so `config.json`, the log and the state stay next to the real file. Use `install --no-shortcut` to only schedule cron.

On Windows, create `sosmonitor.cmd` in a folder on your `PATH` with this single line:

```bat
@py "C:\path\to\sosmonitor.py" %*
```

## config.json

Created automatically on the first run (with `chmod 600`, since it holds passwords and tokens). Shown here with the email fields filled in:

```json
{
  "server_name": "MY-SERVER",
  "check_interval_minutes": 1,
  "disk_path": "/",
  "send_every_check": true,
  "thresholds": {
    "70": "{metric} at {value}%",
    "80": "{metric} at {value}% (WARNING)",
    "90": "{metric} at {value}% (CRITICAL)",
    "100": "{metric} at {value}% (AT LIMIT)"
  },
  "recovery_message": "{metric} at {value}% (BACK TO NORMAL)",
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
| `thresholds`             | Levels in % and the message for each one. They apply to CPU, memory and disk. Your object **replaces** the default list, so include every level you want (e.g. add `"50"` and keep `"70"`, `"80"`...) |
| `recovery_message`       | Sent when a metric goes back below the lowest level                                                                                                                                                   |
| `webhook`                | `url` receives a JSON `POST`; `token`, if set, goes in the header named by `token_header` (default `X-Monitor-Token`; change it to e.g. `X-Token` or `Authorization` if your service requires)        |
| `email`                  | SMTP settings. Port 465 uses SSL; other ports use STARTTLS when the server offers it. For Gmail, use an [app password](https://myaccount.google.com/apppasswords). `to` is a list                     |

In messages, `{metric}` (CPU, Memory, Disk), `{value}` (rounded %) and `{server}` are replaced. Messages can be in any language. Set `"enabled": false` to turn a channel off without deleting its settings.

## How alerts work

SOSMonitor remembers the last level of each metric (in `sosmonitor_state.json`) and **only sends an alert when that level changes**:

- **Going up is immediate.** A value of 72% puts you at level 70, and a value of 85% at level 80.
- **Going down needs a 5-point margin.** A level is kept until the value falls 5 points below it. This keeps a value that hovers around a threshold from sending an alert/recovery pair on every check.
- **No repeats.** Staying at the same level for hours sends nothing.

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
  "message_title": "[SOSMonitor] MY-SERVER: CPU at 83% (WARNING)",
  "message_body": "Server: MY-SERVER (web-01)\nTime:   2026-11-19T20:15:00-03:00\n\n- CPU at 83% (WARNING)\n\nCurrent metrics:\n  CPU      83%\n  MEMORY   61%\n  DISK     48%\n  LOAD     1m=1.2 5m=0.9 15m=0.7 CPUs=2\n"
}
```

`message_title` and `message_body` are the same text as the email subject and body, ready to forward unchanged to WhatsApp, Telegram, Slack or any chat tool. On checks without alerts (sent only when `send_every_check` is `true`), `alerts` is an empty list and both message fields are empty strings, so in n8n you can filter on `message_title != ""`. The `test` command fills them and adds `"test": true`.

## Platforms

|             | CPU / memory           | Disk / load                    | Scheduling                                                     | Dependencies          |
| ----------- | ---------------------- | ------------------------------ | -------------------------------------------------------------- | --------------------- |
| **Linux**   | Native (reads `/proc`) | Native                         | User crontab                                                   | None                  |
| **macOS**   | Needs `psutil`         | Native                         | User crontab                                                   | `pip3 install psutil` |
| **Windows** | Needs `psutil`         | Disk native, load via `psutil` | Task Scheduler: `install` prints the `schtasks` command to run | `pip install psutil`  |

When `psutil` is installed, SOSMonitor uses it on every platform. Without it, missing metrics are simply skipped.

**Windows has not been tested.** It should work (locking uses `msvcrt`, and `install`/`uninstall` print the `schtasks` commands instead of editing crontab). Reports and fixes are welcome.

If your host has no `crontab` command but has a cron panel (common on managed hosting), `install` prints the exact command to paste there.

## Files created (next to the config)

| File                    | Content                                                                                                                             |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `sosmonitor.log`        | One JSON line per check (metrics, alerts, delivery result). Rotates to `.log.1` at 5 MB, keeping one old file (about 10 MB at most) |
| `sosmonitor_state.json` | Last level of each metric                                                                                                           |
| `.sosmonitor.lock`      | Prevents two checks from running at the same time                                                                                   |

## Updating

SOSMonitor is a single file, so updating is just downloading it again:

```bash
cd ~/sosmonitor                 # the folder where sosmonitor.py lives
cp sosmonitor.py sosmonitor.py.bak
curl -fsSLO https://github.com/diemsouza/sosmonitor/raw/main/sosmonitor.py
python3 sosmonitor.py --version
python3 sosmonitor.py test
```

To get a specific version instead of the latest, download `sosmonitor.py` from that version on the [Releases](https://github.com/diemsouza/sosmonitor/releases) page, where each release also lists its changes.

**What is not touched:** `config.json`, the log, the state file and the lock. They are separate files. Cron and the `sosmonitor` command keep working because they point to the same `sosmonitor.py`.

**What to check after updating:**

1. Read the [CHANGELOG](CHANGELOG.md) for the versions between yours and the new one. Each entry has a "Config changes" line (renamed, removed or new keys).
2. Keys missing from your `config.json` use the default value, so a new option never breaks an old config. To use a new option, add it by hand using `config.example.json` as reference.
3. Run `python3 sosmonitor.py test` to confirm the channels still work.
4. Run `python3 sosmonitor.py install` again only if the CHANGELOG says the cron entry or the command changed. It is safe to run and never overwrites `config.json`.

**Rolling back:** `mv sosmonitor.py.bak sosmonitor.py`.

## Development

```bash
python3 -m unittest discover tests
```

The tests use only the standard library and never touch your real `config.json`.

## License

[MIT](LICENSE)
