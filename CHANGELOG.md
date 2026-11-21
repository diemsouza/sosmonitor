# Changelog

Every version lists what was added, changed and fixed, plus a **Config changes** line: keys that were added, renamed or removed in `config.json` ("none" when nothing changed). Keys missing from your `config.json` always use their default value, so a new key never breaks an old config. See [Updating](README.md#updating).

## 0.3.1 - 2026-11-21

### Added

- `install` and `uninstall` end with a summary of each step (`Config`, `Command`, `Schedule`; `Cron entry`, `Command`, `PATH block`) and its status: `created`, `ok`, `already`, `manual` or `failed`. It is followed by the next actions that apply.
- On macOS, when `crontab` is blocked ("Operation not permitted"), the summary explains how to give your terminal Full Disk Access and to run `install` again.

### Changed

- `install` runs every step even if one fails, so a crontab error no longer hides the config and the command that were set up. Exit code: 0 when everything is done, already done, or needs a manual action (a host without `crontab` now exits 0 after printing the line for your panel); non-zero only when a step failed.
- `install` run again does not touch what already exists: no duplicate cron line, no new link, no rewritten config.
- `uninstall` reports each step the same way.

### Fixed

- `install` crashed with a traceback when `crontab` refused to write (for example macOS without Full Disk Access), after having printed "Config created".
- "No crontab yet" and "permission denied" were treated as the same thing when reading the crontab.

**Config changes:** none.

## 0.3.0 - 2026-11-21

### Added

- Install as a Python package: `pipx install sosmonitor` (or `uv tool install sosmonitor`, or `pip install --user sosmonitor`) creates the `sosmonitor` command by itself. `pip install sosmonitor[psutil]` adds `psutil` for macOS and Windows. The single-file `curl` install still works and is still the main way.
- `sosmonitor config`: creates the config if missing and opens it in `$VISUAL`, `$EDITOR`, `nano` or `vi` (`notepad` on Windows). It checks the JSON when the editor closes and offers to reopen it once. Without an interactive terminal or an editor it only prints the path and a command to copy.
- Releases are also published to PyPI (Trusted Publishing, built by the release workflow).

### Changed

- A new config is created in your user config folder (`$XDG_CONFIG_HOME/sosmonitor/` or `~/.config/sosmonitor/`, `%APPDATA%\SOSMonitor\` on Windows), so package upgrades don't delete it. An existing `config.json` next to the script is still used first, and `-c` still wins. Log, state and lock stay next to the config. Nothing is moved or copied.
- `run` and `test` no longer create a config. Without one they print "No config file found" and point to `install`. `install` now creates the config (printing where), then schedules cron and the command, and ends by pointing to `sosmonitor config`.
- `install` no longer creates the `~/.local/bin` link or touches shell startup files when a package install already put `sosmonitor` on your `PATH`. The cron line then runs that executable. `uninstall` only removes what `install` created.
- Internal: platform-specific code is grouped in two sections of `sosmonitor.py` (metrics backend and platform services). No behavior change.

### Fixed

- Top memory consumers were silently missing on Alpine and other busybox hosts, where `ps` lacks `--sort`. They are now read from `/proc`, so no `ps` is needed.

**Config changes:** none.

## 0.2.0 - 2026-11-20

### Added

- `sustain_checks` (default `1`): a metric must stay at or above a level for N consecutive checks before the alert is sent. Recovery is never delayed.
- `top_processes` (default on, `count` 3): when CPU or memory alerts, the email and message list the top consumers (command names only, never arguments). Also sent as `top_processes` in the webhook payload. Disk is not supported.

### Changed

- CPU is now the average over the whole time since the previous check (from cumulative counters saved in the state file), not a 1-second sample taken right when cron fires. It now matches what your host panel shows. Without a previous sample (first run, reboot) it falls back to a 1-second sample once.
- Runs are faster: the 1-second wait is gone from the normal path.
- The alert title names only the most severe alert (highest level, then highest value) plus `(+N more)`. The body lists every alert, then the top processes, then the current metrics on one line. The email and `message_title`/`message_body` use the same text.
- The state file now keeps `levels`, `pending` and `cpu_sample`. Old state files are read as-is and migrated silently. The CPU sample is saved on every run, even when delivery fails; alert levels still only advance after a successful delivery, so a failed alert is retried.

### Fixed

- False CPU alerts: the 1-second sample landed on the minutes when other scheduled jobs start (every 5 minutes), reading far above the real load.

**Config changes:** new optional keys `sustain_checks` and `top_processes`, both defaulted. Nothing to edit in an existing config. Cron and the `sosmonitor` command are unchanged, so there is no need to run `install` again.

## 0.1.0 - 2026-11-19

### Added

- CPU, memory, disk and load average checks, with `psutil` when installed and `/proc` otherwise.
- Threshold alerts by webhook and/or SMTP email, one alert per level change, with a 5-point margin before a level drops.
- Commands: `run`, `test`, `install`, `uninstall` and `--version`.
- `install` schedules the check in crontab and creates the `sosmonitor` command on Linux/macOS.

### Changed

- None.

### Fixed

- None.

**Config changes:** none (first version).
