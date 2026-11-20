# Changelog

Every version lists what was added, changed and fixed, plus a **Config changes** line: keys that were added, renamed or removed in `config.json` ("none" when nothing changed). Keys missing from your `config.json` always use their default value, so a new key never breaks an old config. See [Updating](README.md#updating).

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
