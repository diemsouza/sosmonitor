# Changelog

Every version lists what was added, changed and fixed, plus a **Config changes** line: keys that were added, renamed or removed in `config.json` ("none" when nothing changed). Keys missing from your `config.json` always use their default value, so a new key never breaks an old config. See [Updating](README.md#updating).

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
