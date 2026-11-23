import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sosmonitor as sm  # noqa: E402


def cpu(value):
    return {"cpu": {"percent": value}}


SHORTCUT_OK = ("ok", "sosmonitor (~/.local/bin/sosmonitor)", [])


def read_result(current):
    """What crontab_read returns for a given crontab text (None: no command)."""
    return ("unavailable", "") if current is None else ("ok", current)


def make_cfg(webhook=False, email=False, every_check=True):
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "config.json"
    data = sm.build_template("S")
    data["send_every_check"] = every_check
    data["webhook"]["enabled"] = webhook
    data["email"]["enabled"] = email
    path.write_text(json.dumps(data))
    return sm.load_config(path)


def payload(alerts):
    return {"server": "S", "hostname": "h", "platform": "p",
            "timestamp": "t", "metrics": cpu(95.0), "alerts": alerts}


ALERT = [{"metric": "cpu", "value": 95.0, "threshold": 90,
          "previous_threshold": 0, "message": "CPU at 95% (CRITICAL)"}]


def cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            code = sm.main(list(argv))
        except SystemExit as error:
            code = str(error)
    return code, out.getvalue()


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_escalation_uses_level_message(self):
        alerts, state = sm.build_alerts(self.cfg, cpu(85.0), {})
        self.assertEqual(alerts[0]["threshold"], 80)
        self.assertEqual(alerts[0]["message"], "CPU at 85% (WARNING)")
        self.assertEqual(state["cpu"], 80)

    def test_no_repeat_at_same_level(self):
        alerts, _ = sm.build_alerts(self.cfg, cpu(85.0), {"cpu": 80})
        self.assertEqual(alerts, [])

    def test_recovery_message(self):
        alerts, state = sm.build_alerts(self.cfg, cpu(10.0), {"cpu": 80})
        self.assertEqual(alerts[0]["threshold"], 0)
        self.assertEqual(alerts[0]["message"], "CPU at 10% (BACK TO NORMAL)")
        self.assertEqual(state["cpu"], 0)

    def test_drop_to_lower_level_uses_that_level_message(self):
        alerts, _ = sm.build_alerts(self.cfg, cpu(74.0), {"cpu": 90})
        self.assertEqual(alerts[0]["threshold"], 70)
        self.assertEqual(alerts[0]["previous_threshold"], 90)
        self.assertEqual(alerts[0]["message"], "CPU at 74%")

    def test_load_has_no_thresholds(self):
        alerts, _ = sm.build_alerts(self.cfg, {"load": {"1m": 50}}, {})
        self.assertEqual(alerts, [])

    def test_unknown_placeholder_is_kept(self):
        self.assertEqual(sm.format_message("{metric} {foo}", "S", "disk", 1), "Disk {foo}")


class HysteresisTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()  # levels 70/80/90/100, margin 5

    def level(self, value, previous):
        alerts, state = sm.build_alerts(self.cfg, cpu(value), {"cpu": previous})
        return state["cpu"], alerts

    def test_stays_within_margin(self):
        self.assertEqual(self.level(76.0, 80), (80, []))
        self.assertEqual(self.level(75.0, 80), (80, []))

    def test_drops_below_margin(self):
        level, alerts = self.level(74.9, 80)
        self.assertEqual(level, 70)
        self.assertEqual(len(alerts), 1)

    def test_recovery_only_below_lowest_minus_margin(self):
        self.assertEqual(self.level(66.0, 70), (70, []))
        level, alerts = self.level(64.0, 70)
        self.assertEqual(level, 0)
        self.assertEqual(alerts[0]["message"], "CPU at 64% (BACK TO NORMAL)")

    def test_drop_lands_on_highest_level_still_in_margin(self):
        # 86 is within the margin of 90, so a drop from 100 stops at 90
        self.assertEqual(self.level(86.0, 100)[0], 90)

    def test_rising_is_immediate(self):
        self.assertEqual(self.level(70.0, 0)[0], 70)
        self.assertEqual(self.level(80.0, 70)[0], 80)

    def test_hovering_around_threshold_alerts_once(self):
        state, sent = {}, 0
        for value in (69.0, 71.0, 69.0, 71.0, 68.0, 72.0, 66.0):
            alerts, state = sm.build_alerts(self.cfg, cpu(value), state)
            sent += len(alerts)
        self.assertEqual(sent, 1)

    def test_previous_level_removed_from_config(self):
        level, alerts = self.level(72.0, 75)
        self.assertEqual(level, 70)
        self.assertEqual(alerts[0]["message"], "CPU at 72%")


class DeliveryTests(unittest.TestCase):
    def test_webhook_down_email_up_advances_state(self):
        cfg = make_cfg(webhook=True, email=True)
        with mock.patch.object(sm, "send_webhook", return_value=(False, "down")), \
                mock.patch.object(sm, "send_email", return_value=(True, "sent")):
            delivered, results = sm.deliver(cfg, payload(ALERT))
        self.assertTrue(delivered)
        self.assertEqual(results["webhook"], "down")

    def test_everything_down_keeps_state(self):
        cfg = make_cfg(webhook=True, email=True)
        with mock.patch.object(sm, "send_webhook", return_value=(False, "down")), \
                mock.patch.object(sm, "send_email", return_value=(False, "err")):
            delivered, _ = sm.deliver(cfg, payload(ALERT))
        self.assertFalse(delivered)

    def test_alert_goes_to_both_channels(self):
        cfg = make_cfg(webhook=True, email=True)
        with mock.patch.object(sm, "send_webhook", return_value=(True, "HTTP 200")), \
                mock.patch.object(sm, "send_email", return_value=(True, "sent")) as email:
            sm.deliver(cfg, payload(ALERT))
        email.assert_called_once()

    def test_no_channels_is_not_a_failure(self):
        delivered, results = sm.deliver(make_cfg(), payload(ALERT))
        self.assertTrue(delivered)
        self.assertEqual(results, {})

    def test_email_not_sent_without_alerts(self):
        with mock.patch.object(sm, "send_email") as email:
            sm.deliver(make_cfg(email=True), payload([]))
        email.assert_not_called()

    def test_webhook_skipped_without_alerts_when_not_every_check(self):
        with mock.patch.object(sm, "send_webhook") as webhook:
            sm.deliver(make_cfg(webhook=True, every_check=False), payload([]))
        webhook.assert_not_called()

    def test_webhook_token_header(self):
        def sent_headers(webhook):
            response = mock.MagicMock(status=200)
            response.__enter__.return_value = response
            with mock.patch.object(sm.urllib.request, "urlopen", return_value=response) as urlopen:
                sm.send_webhook(webhook, {})
            return dict(urlopen.call_args[0][0].header_items())

        webhook = make_cfg(webhook=True)["webhook"]
        webhook.update(url="http://example.test", token="secret")
        self.assertEqual(sent_headers(webhook).get("X-monitor-token"), "secret")
        webhook["token_header"] = "X-Token"
        headers = sent_headers(webhook)
        self.assertEqual(headers.get("X-token"), "secret")
        self.assertNotIn("X-monitor-token", headers)


class EmailFormatTests(unittest.TestCase):
    def test_subject_and_body(self):
        subject, body = sm.build_message(payload(ALERT))
        self.assertEqual(subject, "[SOSMonitor] S: CPU at 95% (CRITICAL)")
        self.assertIn("CPU 95%", body)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_example_file_matches_template(self):
        example = json.loads((ROOT / "config.example.json").read_text())
        self.assertEqual(example, sm.build_template("MY-SERVER"))

    def test_missing_config_is_not_created_by_run_or_test(self):
        for command in ("run", "test"):
            with mock.patch.object(sm, "run_once") as run, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code, out = cli("-c", str(self.path), command)
            self.assertEqual(code, 1)
            self.assertIn("No config file found", err.getvalue())
            self.assertIn("install", err.getvalue())
            self.assertFalse(self.path.exists())
            run.assert_not_called()

    def test_install_creates_missing_config_then_continues(self):
        with mock.patch.object(sm, "crontab_read", return_value=("ok", "")), \
                mock.patch.object(sm, "crontab_write", return_value=(True, "")) as write, \
                mock.patch.object(sm, "install_shortcut", return_value=SHORTCUT_OK):
            code, out = cli("-c", str(self.path), "install")
        self.assertEqual(code, 0)
        self.assertTrue(self.path.exists())
        self.assertIn(f"Config     created   {self.path.resolve()}", out)
        write.assert_called_once()
        self.assertIn(f"{self.path.resolve()} config", out)
        self.assertIn(f"{self.path.resolve()} test", out)
        self.assertNotIn("nano", out)

    def test_install_keeps_existing_config_and_prints_no_edit_hint(self):
        self.path.write_text("{}")
        with mock.patch.object(sm, "crontab_read", return_value=("ok", "")), \
                mock.patch.object(sm, "crontab_write", return_value=(True, "")), \
                mock.patch.object(sm, "install_shortcut", return_value=SHORTCUT_OK):
            _, out = cli("-c", str(self.path), "install")
        self.assertIn("Config     already", out)
        self.assertNotIn("Set server_name", out)

    def test_existing_config_is_never_overwritten(self):
        self.path.write_text(json.dumps({"server_name": "CUSTOM"}))
        with mock.patch.object(sm, "run_once") as run:
            cli("-c", str(self.path))
        run.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text()), {"server_name": "CUSTOM"})

    def test_user_thresholds_replace_defaults(self):
        self.path.write_text(json.dumps({"thresholds": {"95": "alto", "60": "médio"}}))
        cfg = sm.load_config(self.path)
        self.assertEqual(cfg["thresholds"], [(60, "médio"), (95, "alto")])
        self.assertIs(cfg["webhook"]["enabled"], False)

    def test_default_config_is_created_in_user_dir_by_install(self):
        xdg = Path(self.tmp.name) / "xdg"
        script = Path(self.tmp.name) / "app" / "sosmonitor.py"
        with mock.patch.dict(sm.os.environ, {"XDG_CONFIG_HOME": str(xdg)}), \
                mock.patch.object(sm, "SCRIPT", script), \
                mock.patch.object(sm.shutil, "which", return_value=None), \
                mock.patch.object(sm, "find_entry_point", return_value=None), \
                mock.patch.object(sm, "_own_link", return_value=False), \
                mock.patch.object(sm, "crontab_read", return_value=("ok", "")), \
                mock.patch.object(sm, "crontab_write", return_value=(True, "")), \
                mock.patch.object(sm, "install_shortcut", return_value=SHORTCUT_OK):
            code, out = cli("install")
        default = xdg / "sosmonitor" / "config.json"
        self.assertEqual(code, 0)
        self.assertTrue(default.exists())
        self.assertFalse((script.parent / "config.json").exists())
        self.assertIn(str(default), out)
        self.assertIn(f"python3 {script} config", out)

    def test_old_config_missing_new_key_uses_default(self):
        old = sm.build_template("OLD")
        del old["recovery_message"]
        del old["email"]["smtp_port"]
        self.path.write_text(json.dumps(old))
        before = self.path.read_bytes()
        # A key that a newer version adds to DEFAULTS.
        with mock.patch.dict(sm.DEFAULTS, {"new_option": 42}):
            cfg = sm.load_config(self.path)
        self.assertEqual(cfg["new_option"], 42)
        self.assertEqual(cfg["recovery_message"], sm.DEFAULTS["recovery_message"])
        self.assertEqual(cfg["email"]["smtp_port"], 587)
        self.assertEqual(cfg["server_name"], "OLD")
        self.assertEqual(self.path.read_bytes(), before)

    def test_install_twice_leaves_config_byte_for_byte(self):
        # Hand-edited formatting and a missing key must survive untouched.
        self.path.write_text('{ "server_name": "HAND",\n\t"disk_path": "/data" }')
        before = self.path.read_bytes()
        with mock.patch.object(sm, "crontab_read", return_value=("ok", "")), \
                mock.patch.object(sm, "crontab_write", return_value=(True, "")), \
                mock.patch.object(sm, "install_shortcut", return_value=SHORTCUT_OK):
            for _ in range(2):
                code, _ = cli("-c", str(self.path), "install")
                self.assertEqual(code, 0)
                self.assertEqual(self.path.read_bytes(), before)

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as exit_, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            sm.main(["--version"])
        self.assertEqual(exit_.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"SOSMonitor {sm.VERSION}")

    def test_bad_threshold_key(self):
        self.path.write_text(json.dumps({"thresholds": {"abc": "x"}}))
        with self.assertRaises(SystemExit) as error:
            sm.load_config(self.path)
        self.assertIn("thresholds", str(error.exception))


class CronTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.cfg["check_interval_minutes"] = 5
        self.path = Path("/srv/mon/config.json")

    def install(self, current, os_name="posix"):
        out = io.StringIO()
        with mock.patch.object(sm, "crontab_read", return_value=read_result(current)), \
                mock.patch.object(sm, "crontab_write", return_value=(True, "")) as write, \
                mock.patch.object(sm.os, "name", os_name), \
                contextlib.redirect_stdout(out):
            code = sm.cmd_install(self.cfg, self.path, shortcut=False)
        return code, write, out.getvalue()

    def test_install_writes_tagged_line_with_config_path(self):
        code, write, _ = self.install("MAILTO=x\n")
        self.assertEqual(code, 0)
        lines = write.call_args[0][0]
        self.assertEqual(lines[0], "MAILTO=x")
        self.assertTrue(lines[1].startswith("*/5 * * * * "))
        self.assertIn(f'-c "{self.path}" run', lines[1])
        self.assertTrue(lines[1].endswith(sm.CRON_MARK))

    def test_install_replaces_previous_entry(self):
        _, write, _ = self.install(f"* * * * * old {sm.CRON_MARK}\n")
        lines = write.call_args[0][0]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("old", lines[0])

    def test_install_without_crontab_prints_command(self):
        code, write, out = self.install(None)
        self.assertEqual(code, 0)  # nothing broke: the user pastes the line in the panel
        self.assertIn("manual", out)
        write.assert_not_called()
        self.assertIn(f'-c "{self.path}" run', out)

    def test_install_on_windows_prints_schtasks(self):
        _, _, out = self.install(None, os_name="nt")
        self.assertIn("schtasks /Create /SC MINUTE /MO 5 /TN SOSMonitor", out)

    def test_uninstall_removes_only_tagged_line(self):
        current = f"0 * * * * backup\n* * * * * x {sm.CRON_MARK}\n"
        with mock.patch.object(sm, "crontab_read", return_value=read_result(current)), \
                mock.patch.object(sm, "crontab_write", return_value=(True, "")) as write, \
                mock.patch.object(sm, "remove_shortcut"), \
                contextlib.redirect_stdout(io.StringIO()):
            sm.cmd_uninstall()
        write.assert_called_once_with(["0 * * * * backup"])


class CrontabPrimitiveTests(unittest.TestCase):
    def run_read(self, returncode, stderr="", stdout=""):
        result = mock.Mock(returncode=returncode, stderr=stderr, stdout=stdout)
        with mock.patch.object(sm.shutil, "which", return_value="/usr/bin/crontab"), \
                mock.patch.object(sm.subprocess, "run", return_value=result):
            return sm.crontab_read()

    def test_read_without_command_is_unavailable(self):
        with mock.patch.object(sm.shutil, "which", return_value=None):
            self.assertEqual(sm.crontab_read(), ("unavailable", ""))

    def test_read_permission_denied_keeps_original_message(self):
        message = "crontab: Operation not permitted"
        self.assertEqual(self.run_read(1, message), ("denied", message))

    def test_read_no_crontab_yet_is_an_empty_success(self):
        self.assertEqual(self.run_read(1, "no crontab for diego"), ("ok", ""))

    def test_read_success_returns_text(self):
        self.assertEqual(self.run_read(0, stdout="* * * * * x\n"), ("ok", "* * * * * x\n"))

    def test_read_other_error_is_reported_not_raised(self):
        state, message = self.run_read(2, "crontab: bad thing")
        self.assertEqual((state, message), ("error", "crontab: bad thing"))
        with mock.patch.object(sm.shutil, "which", return_value="/usr/bin/crontab"), \
                mock.patch.object(sm.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(sm.crontab_read(), ("error", "boom"))

    def test_write_failure_returns_error_instead_of_raising(self):
        result = mock.Mock(returncode=1, stderr="crontab: Operation not permitted\n")
        with mock.patch.object(sm.subprocess, "run", return_value=result) as run:
            self.assertEqual(sm.crontab_write(["a"]),
                             (False, "crontab: Operation not permitted"))
        self.assertNotIn("check", run.call_args.kwargs)
        with mock.patch.object(sm.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(sm.crontab_write(["a"]), (False, "boom"))

    def test_write_success(self):
        with mock.patch.object(sm.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.assertEqual(sm.crontab_write(["a"]), (True, ""))


class InstallSummaryTests(unittest.TestCase):
    """install/uninstall in a scratch HOME, with a fake crontab."""

    DENIED = ("denied", "crontab: Operation not permitted")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.script = root / "app" / "sosmonitor.py"
        self.script.parent.mkdir()
        self.script.write_text("#!/usr/bin/env python3\n")
        self.config = root / "cfg" / "config.json"
        self.crontab = ""  # what the fake crontab holds
        for patch in (
            mock.patch.dict(sm.os.environ, {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}),
            mock.patch.object(sm, "SCRIPT", self.script),
            mock.patch.object(sm, "shell_finds_command", return_value=True),
            mock.patch.object(sm, "find_entry_point", return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def fake_write(self, lines):
        self.crontab = "\n".join(lines) + "\n"
        return True, ""

    def run_cli(self, command, read=None, write=None, platform=None):
        read = read or (lambda: ("ok", self.crontab))
        out = io.StringIO()
        with mock.patch.object(sm, "crontab_read", side_effect=read), \
                mock.patch.object(sm, "crontab_write", side_effect=write or self.fake_write), \
                mock.patch.object(sm.sys, "platform", platform or sys.platform), \
                contextlib.redirect_stdout(out):
            code = sm.main(["-c", str(self.config), command])
        return code, out.getvalue()

    def link(self):
        return self.home / ".local" / "bin" / "sosmonitor"

    def test_denied_schedule_still_creates_config_and_command(self):
        code, out = self.run_cli("install", read=lambda: self.DENIED, platform="darwin")
        self.assertNotEqual(code, 0)
        self.assertTrue(self.config.exists())
        self.assertTrue(self.link().is_symlink())
        rows = [line.split()[0] for line in out.splitlines() if line[:1].isalpha()][:3]
        self.assertEqual(rows, ["Config", "Command", "Schedule"])
        self.assertIn("Schedule   failed    crontab: Operation not permitted", out)
        self.assertIn("Full Disk Access", out)
        self.assertIn("then run: sosmonitor install", out)

    def test_denied_write_is_reported_not_raised(self):
        code, out = self.run_cli("install", write=lambda lines: (False, "crontab: Operation not permitted"),
                                 platform="darwin")
        self.assertNotEqual(code, 0)
        self.assertIn("Schedule   failed", out)
        self.assertIn("Full Disk Access", out)
        self.assertTrue(self.link().is_symlink())

    def test_denied_on_linux_has_no_macos_hint_but_a_fix_line(self):
        code, out = self.run_cli("install", read=lambda: self.DENIED, platform="linux")
        self.assertNotEqual(code, 0)
        self.assertNotIn("Full Disk Access", out)
        self.assertIn("Fix the error above", out)

    def test_host_without_crontab_is_manual_and_exits_zero(self):
        code, out = self.run_cli("install", read=lambda: ("unavailable", ""))
        self.assertEqual(code, 0)
        self.assertIn("Schedule   manual", out)
        self.assertIn(f'-c "{self.config.resolve()}" run', out)

    def test_install_twice_does_not_duplicate_or_rewrite(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        config_bytes = self.config.read_bytes()
        link_stat = sm.os.lstat(self.link())
        bashrc = (self.home / ".bashrc").read_text()
        crontab = self.crontab
        code, out = self.run_cli("install")
        self.assertEqual(code, 0)
        self.assertEqual(self.crontab, crontab)
        self.assertEqual(self.crontab.count(sm.CRON_MARK), 1)
        self.assertEqual(self.config.read_bytes(), config_bytes)
        self.assertEqual(sm.os.lstat(self.link()).st_ino, link_stat.st_ino)
        self.assertEqual((self.home / ".bashrc").read_text(), bashrc)
        self.assertIn("Config     already", out)
        self.assertIn("Command    already", out)
        self.assertIn("Schedule   already", out)

    def test_uninstall_denied_does_not_raise(self):
        self.run_cli("install")
        code, out = self.run_cli("uninstall", read=lambda: self.DENIED, platform="darwin")
        self.assertNotEqual(code, 0)
        self.assertIn("Cron entry failed", out)
        self.assertIn("Full Disk Access", out)
        self.assertIn("then run: sosmonitor uninstall", out)
        self.assertFalse(self.link().exists())  # the other steps still ran

    def test_uninstall_write_failure_does_not_raise(self):
        self.run_cli("install")
        code, out = self.run_cli("uninstall", write=lambda lines: (False, "disk full"))
        self.assertNotEqual(code, 0)
        self.assertIn("crontab: disk full", out)

    def test_uninstall_success_summary(self):
        self.run_cli("install")
        code, out = self.run_cli("uninstall")
        self.assertEqual(code, 0)
        self.assertEqual(self.crontab.count(sm.CRON_MARK), 0)
        for label in ("Cron entry", "Command", "PATH block"):
            self.assertIn(label, out)

    def test_run_and_test_without_config_create_nothing_and_fail(self):
        for command in ("run", "test"):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                code, out = self.run_cli(command)
            self.assertNotEqual(code, 0)
            self.assertFalse(self.config.exists())
            self.assertFalse(self.config.parent.exists())
            self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
            self.assertIn("install", err.getvalue())

    def test_no_argument_means_run(self):
        self.config.parent.mkdir()
        self.config.write_text("{}")
        with mock.patch.object(sm, "run_once") as run:
            sm.main(["-c", str(self.config)])
        run.assert_called_once()


class MessageTests(unittest.TestCase):
    def sent_email(self, data):
        email = make_cfg(email=True)["email"]
        email.update(smtp_host="smtp.test", to=["a@b.test"], **{"from": "m@b.test"})
        with mock.patch.object(sm.smtplib, "SMTP") as smtp:
            ok, _ = sm.send_email(email, data)
        self.assertTrue(ok)
        return smtp.return_value.send_message.call_args[0][0]

    def test_webhook_fields_match_email(self):
        data = sm.build_payload(make_cfg(), cpu(95.0), ALERT)
        message = self.sent_email(data)
        self.assertEqual(data["message_title"], message["Subject"])
        self.assertEqual(data["message_body"], message.get_content())
        self.assertIn("CPU at 95% (CRITICAL)", data["message_title"])

    def test_fields_empty_without_alerts(self):
        data = sm.build_payload(make_cfg(), cpu(10.0), [])
        self.assertEqual(data["message_title"], "")
        self.assertEqual(data["message_body"], "")


class RunTestCommandTests(unittest.TestCase):
    def run_test(self, cfg):
        out = io.StringIO()
        with mock.patch.object(sm, "collect_metrics", return_value=cpu(12.0)), \
                mock.patch.object(sm, "send_webhook", return_value=(True, "HTTP 200")) as hook, \
                contextlib.redirect_stdout(out):
            code = sm.run_test(cfg)
        return code, out.getvalue(), hook

    def test_prints_message_without_channels(self):
        code, out, hook = self.run_test(make_cfg())
        self.assertEqual(code, 2)
        self.assertIn("[SOSMonitor] S: SOSMonitor test", out)
        self.assertIn("  - SOSMonitor test", out)
        self.assertIn("CPU 12%", out)
        self.assertIn("No channel enabled", out)
        hook.assert_not_called()

    def test_shows_top_processes_when_available(self):
        top = {"memory": [{"name": "php-fpm", "percent": 8.2, "rss_bytes": 1395864371}]}
        with mock.patch.object(sm, "collect_top_processes", return_value=top):
            code, out, _ = self.run_test(make_cfg())
        self.assertIn("Top processes by memory:", out)
        self.assertIn("php-fpm", out)

    def test_prints_message_before_results(self):
        code, out, hook = self.run_test(make_cfg(webhook=True))
        self.assertEqual(code, 0)
        self.assertLess(out.index("[SOSMonitor] S: SOSMonitor test"), out.index("webhook: HTTP 200"))
        sent = hook.call_args[0][1]
        self.assertTrue(sent["test"])
        self.assertEqual(sent["message_title"], "[SOSMonitor] S: SOSMonitor test")


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.script = Path(self.tmp.name) / "app" / "sosmonitor.py"
        self.script.parent.mkdir()
        self.script.write_text("#!/usr/bin/env python3\n")
        self.link = self.home / ".local" / "bin" / "sosmonitor"
        patches = [
            mock.patch.dict(sm.os.environ, {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}),
            mock.patch.object(sm, "SCRIPT", self.script),
            mock.patch.object(sm, "shell_finds_command", return_value=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_quiet(self, func):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            func()
        return out.getvalue()

    def install(self):
        """install_shortcut's result as text: status, detail and next steps."""
        self.result = sm.install_shortcut()
        status, detail, notes = self.result
        return "\n".join([status, detail] + notes)

    def block_count(self, name):
        return (self.home / name).read_text().splitlines().count(sm.CRON_MARK)

    def test_writes_every_existing_startup_file(self):
        (self.home / ".bashrc").write_text("alias ll='ls -l'\n")
        (self.home / ".profile").write_text("umask 022")  # no trailing newline
        out = self.install()
        self.assertEqual(self.link.resolve(), self.script.resolve())
        self.assertTrue(self.script.stat().st_mode & 0o100)
        for name in (".bashrc", ".profile"):
            text = (self.home / name).read_text()
            self.assertTrue(text.endswith(f"{sm.CRON_MARK}\n{sm.PATH_LINE}\n"))
        self.assertEqual((self.home / ".profile").read_text().splitlines()[0], "umask 022")
        self.assertFalse((self.home / ".zshrc").exists())
        self.assertFalse((self.home / ".bash_profile").exists())
        self.assertIn(sm.PATH_LINE, out)

    def test_creates_bashrc_when_none_exists(self):
        self.install()
        self.assertEqual((self.home / ".bashrc").read_text(), f"{sm.CRON_MARK}\n{sm.PATH_LINE}\n")

    def test_second_run_is_idempotent(self):
        (self.home / ".bashrc").write_text("x=1\n")
        (self.home / ".zshrc").write_text("y=2\n")
        self.install()
        self.install()
        self.assertEqual(self.block_count(".bashrc"), 1)
        self.assertEqual(self.block_count(".zshrc"), 1)
        self.assertTrue(self.link.is_symlink())

    def test_uninstall_removes_only_block_and_link(self):
        original = {".bashrc": "a=1\n# my notes\n", ".bash_profile": "b=2", ".zshrc": ""}
        for name, text in original.items():
            (self.home / name).write_text(text)
        self.install()
        with mock.patch.object(sm, "uninstall_cron", return_value=("already", "")):
            self.run_quiet(sm.cmd_uninstall)
        self.assertFalse(self.link.exists() or self.link.is_symlink())
        expected = dict(original, **{".bash_profile": "b=2\n"})  # newline added before the block
        for name, text in expected.items():
            self.assertEqual((self.home / name).read_text(), text)

    def test_uninstall_keeps_foreign_link(self):
        other = Path(self.tmp.name) / "other"
        other.write_text("")
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(other)
        self.run_quiet(sm.remove_shortcut)
        self.assertTrue(self.link.is_symlink())

    def test_regular_file_is_not_overwritten(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("mine")
        out = self.install()
        self.assertEqual(self.result[0], "manual")
        self.assertIn("left it untouched", out)
        self.assertIn(str(self.script), out)
        self.assertEqual(self.link.read_text(), "mine")
        self.assertFalse((self.home / ".bashrc").exists())

    def test_already_on_path_touches_no_startup_file(self):
        sm.os.environ["PATH"] = f"{self.home}/.local/bin:/usr/bin"
        (self.home / ".bashrc").write_text("a=1\n")
        self.install()
        self.assertTrue(self.link.is_symlink())
        self.assertEqual((self.home / ".bashrc").read_text(), "a=1\n")

    def test_failed_verification_prints_full_path(self):
        with mock.patch.object(sm, "shell_finds_command", return_value=False):
            out = self.install()
        self.assertIn("could not confirm", out)
        self.assertIn(str(self.link), out)
        self.assertIn(sm.PATH_LINE, out)

    def test_install_wiring(self):
        cfg, path = make_cfg(), Path("/x/config.json")
        with mock.patch.object(sm, "schedule_step", return_value=("ok", "every 5 minute(s)")), \
                mock.patch.object(sm, "install_shortcut", return_value=SHORTCUT_OK) as shortcut, \
                contextlib.redirect_stdout(io.StringIO()):
            sm.cmd_install(cfg, path)
            shortcut.assert_called_once()
            shortcut.reset_mock()
            sm.cmd_install(cfg, path, shortcut=False)
            with mock.patch.object(sm.os, "name", "nt"):
                sm.cmd_install(cfg, path)
            shortcut.assert_not_called()

    def test_no_shortcut_flag(self):
        config = Path(self.tmp.name) / "config.json"
        config.write_text("{}")
        with mock.patch.object(sm, "cmd_install", return_value=0) as install:
            cli("-c", str(config), "install", "--no-shortcut")
        self.assertIs(install.call_args.kwargs["shortcut"], False)


def two_alerts():
    return [
        {"metric": "cpu", "value": 75.0, "threshold": 70, "previous_threshold": 0,
         "message": "CPU at 75%"},
        {"metric": "memory", "value": 85.0, "threshold": 80, "previous_threshold": 0,
         "message": "Memory at 85% (WARNING)"},
    ]


class CpuTests(unittest.TestCase):
    def measure(self, previous, *reads):
        with mock.patch.object(sm, "read_cpu_counters", side_effect=list(reads)), \
                mock.patch.object(sm.time, "sleep") as sleep:
            return sm.measure_cpu(previous), sleep

    def test_interval_average_without_sleeping(self):
        previous = {"busy": 100, "total": 1000, "time": 0}
        (percent, sample), sleep = self.measure(previous, (160, 1200))
        self.assertEqual(percent, 30.0)  # 60 busy of 200
        self.assertEqual((sample["busy"], sample["total"]), (160, 1200))
        self.assertIn("time", sample)
        sleep.assert_not_called()

    def test_two_runs_chain_through_the_saved_sample(self):
        with mock.patch.object(sm, "read_cpu_counters", side_effect=[(10, 100), (10, 100)]), \
                mock.patch.object(sm.time, "sleep"):
            _, first = sm.measure_cpu(None)
        (percent, _), _ = self.measure(first, (40, 200))
        self.assertEqual(percent, 30.0)

    def test_first_run_falls_back_to_one_second_sample(self):
        (percent, sample), sleep = self.measure(None, (10, 100), (60, 200))
        self.assertEqual(percent, 50.0)
        sleep.assert_called_once_with(1)
        self.assertEqual((sample["busy"], sample["total"]), (60, 200))

    def test_reboot_negative_delta_falls_back(self):
        previous = {"busy": 5000, "total": 9000, "time": 0}
        (percent, sample), sleep = self.measure(previous, (10, 100), (20, 200))
        self.assertEqual(percent, 10.0)
        sleep.assert_called_once()
        self.assertEqual(sample["total"], 200)

    def test_zero_total_delta_falls_back(self):
        previous = {"busy": 10, "total": 100, "time": 0}
        (percent, _), sleep = self.measure(previous, (10, 100), (30, 200))
        self.assertEqual(percent, 20.0)
        sleep.assert_called_once()

    def test_garbage_sample_falls_back(self):
        (percent, _), sleep = self.measure({"busy": "x"}, (0, 0), (50, 100))
        self.assertEqual(percent, 50.0)
        sleep.assert_called_once()

    def test_no_counters_means_no_cpu(self):
        (percent, sample), sleep = self.measure(None, None)
        self.assertEqual((percent, sample), (None, None))
        sleep.assert_not_called()


class RunOnceStateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(webhook=True)
        self.state_path = sm.file_in(self.cfg, sm.STATE_FILE)

    def run_once(self, webhook_ok, percent=95.0, sample=None):
        sample = sample or {"busy": 1.0, "total": 2.0, "time": 5.0}
        with mock.patch.object(sm, "measure_cpu", return_value=(percent, sample)), \
                mock.patch.object(sm, "get_memory_usage", return_value=None), \
                mock.patch.object(sm, "get_disk_usage", return_value=None), \
                mock.patch.object(sm, "get_load_average", return_value=None), \
                mock.patch.object(sm, "get_uptime", return_value=None), \
                mock.patch.object(sm, "collect_top_processes", return_value={}), \
                mock.patch.object(sm, "send_webhook",
                                  return_value=(webhook_ok, "x")), \
                contextlib.redirect_stdout(io.StringIO()):
            sm.run_once(self.cfg)
        return json.loads(self.state_path.read_text())

    def test_console_summary_lists_top_processes(self):
        top = {"cpu": [{"name": "php-fpm", "percent": 62.5}]}
        out = io.StringIO()
        with mock.patch.object(sm, "collect_top_processes", return_value=top), \
                mock.patch.object(sm, "measure_cpu", return_value=(95.0, None)), \
                mock.patch.object(sm, "send_webhook", return_value=(True, "x")), \
                contextlib.redirect_stdout(out):
            sm.run_once(self.cfg)
        self.assertIn("ALERT: CPU at 95% (CRITICAL)", out.getvalue())
        self.assertIn("  Top processes by CPU:\n    1. php-fpm  62.5%", out.getvalue())

    def test_failed_delivery_saves_cpu_sample_but_not_levels(self):
        state = self.run_once(webhook_ok=False)
        self.assertEqual(state["cpu_sample"]["total"], 2.0)
        self.assertEqual(state["levels"], {})

    def test_successful_delivery_advances_levels(self):
        state = self.run_once(webhook_ok=True)
        self.assertEqual(state["levels"], {"cpu": 90})
        self.assertEqual(state["cpu_sample"]["total"], 2.0)

    def test_failed_delivery_keeps_previous_levels(self):
        self.state_path.write_text(json.dumps({"levels": {"cpu": 70}}))
        self.assertEqual(self.run_once(webhook_ok=False)["levels"], {"cpu": 70})

    def test_old_flat_state_still_loads_and_is_migrated(self):
        self.state_path.write_text(json.dumps({"cpu": 90, "memory": 70}))
        state = sm.load_state(self.cfg)
        self.assertEqual(state["levels"], {"cpu": 90, "memory": 70})
        self.assertIsNone(state["cpu_sample"])
        saved = self.run_once(webhook_ok=True)  # cpu stays at 90: no alert
        self.assertEqual(saved["levels"], {"cpu": 90, "memory": 70})
        self.assertIn("cpu_sample", saved)

    def test_missing_or_corrupt_state(self):
        self.assertEqual(sm.load_state(self.cfg)["levels"], {})
        self.state_path.write_text("{not json")
        self.assertEqual(sm.load_state(self.cfg)["levels"], {})


class SustainTests(unittest.TestCase):
    def check(self, cfg, value, levels, pending):
        alerts, levels = sm.build_alerts(cfg, cpu(value), levels, pending)
        return alerts, levels

    def cfg_with(self, n):
        cfg = make_cfg()
        cfg["sustain_checks"] = n
        return cfg

    def test_default_alerts_immediately(self):
        self.assertEqual(make_cfg()["sustain_checks"], 1)
        alerts, _ = self.check(make_cfg(), 75.0, {}, {})
        self.assertEqual(len(alerts), 1)

    def test_three_alerts_on_third_consecutive_check(self):
        cfg, levels, pending = self.cfg_with(3), {}, {}
        for expected in (0, 0, 1):
            alerts, levels = self.check(cfg, 75.0, levels, pending)
            self.assertEqual(len(alerts), expected)
        self.assertEqual(levels["cpu"], 70)

    def test_drop_in_between_resets_the_counter(self):
        cfg, levels, pending = self.cfg_with(3), {}, {}
        for value in (75.0, 75.0, 10.0, 75.0, 75.0):
            alerts, levels = self.check(cfg, value, levels, pending)
            self.assertEqual(alerts, [])
        alerts, _ = self.check(cfg, 75.0, levels, pending)
        self.assertEqual(len(alerts), 1)

    def test_higher_level_keeps_the_streak(self):
        cfg, levels, pending = self.cfg_with(2), {}, {}
        self.check(cfg, 72.0, levels, pending)
        alerts, levels = self.check(cfg, 85.0, levels, pending)
        self.assertEqual(alerts[0]["threshold"], 80)

    def test_lower_level_restarts_the_streak(self):
        cfg, levels, pending = self.cfg_with(2), {}, {}
        self.check(cfg, 85.0, levels, pending)
        alerts, _ = self.check(cfg, 72.0, levels, pending)
        self.assertEqual(alerts, [])
        self.assertEqual(pending["cpu"], {"level": 70, "count": 1})

    def test_recovery_is_never_delayed(self):
        cfg = self.cfg_with(5)
        alerts, levels = self.check(cfg, 10.0, {"cpu": 80}, {})
        self.assertEqual(alerts[0]["threshold"], 0)
        self.assertEqual(levels["cpu"], 0)

    def test_hysteresis_drop_is_never_delayed(self):
        alerts, _ = self.check(self.cfg_with(5), 74.0, {"cpu": 80}, {})
        self.assertEqual(alerts[0]["threshold"], 70)

    def test_failed_delivery_retries_without_waiting_again(self):
        cfg, pending = self.cfg_with(2), {}
        self.check(cfg, 75.0, {}, pending)
        alerts, _ = self.check(cfg, 75.0, {}, pending)  # fires, but levels not saved
        self.assertEqual(len(alerts), 1)
        alerts, levels = self.check(cfg, 75.0, {}, pending)  # retried at once
        self.assertEqual(len(alerts), 1)
        self.check(cfg, 75.0, levels, pending)  # delivered: no more alerts, counter gone
        self.assertNotIn("cpu", pending)

    def test_bad_value_means_one(self):
        cfg = self.cfg_with("abc")
        alerts, _ = self.check(cfg, 75.0, {}, {})
        self.assertEqual(len(alerts), 1)


class TitleTests(MessageTests):
    def data(self, alerts, **extra):
        data = {"server": "Cloudways", "hostname": "h", "platform": "p",
                "timestamp": "2026-11-20T18:05:03+00:00",
                "metrics": {"cpu": {"percent": 75.0}, "memory": {"percent": 85.0},
                            "disk": {"percent": 54.0}}, "alerts": alerts}
        data.update(extra)
        return data

    def test_title_names_most_severe_with_counter(self):
        title, body = sm.build_message(self.data(two_alerts()))
        self.assertEqual(title, "[SOSMonitor] Cloudways: Memory at 85% (WARNING) (+1 more)")
        self.assertEqual(body, "\n".join([
            "Server: Cloudways",
            "Time: 2026-11-20T18:05:03+00:00",
            "",
            "Alerts:",
            "  - CPU at 75%",
            "  - Memory at 85% (WARNING)",
            "",
            "Current metrics:",
            "  CPU 75% | MEMORY 85% | DISK 54%",
        ]) + "\n")

    def test_tie_on_level_is_broken_by_value(self):
        alerts = two_alerts()
        alerts[0].update(threshold=80, value=88.0, message="CPU at 88% (WARNING)")
        title, _ = sm.build_message(self.data(alerts))
        self.assertIn("CPU at 88%", title)

    def test_single_alert_has_no_counter(self):
        title, _ = sm.build_message(self.data(two_alerts()[:1]))
        self.assertEqual(title, "[SOSMonitor] Cloudways: CPU at 75%")

    def test_message_fields_equal_email_and_are_empty_without_alerts(self):
        cfg = make_cfg(email=True)
        data = sm.build_payload(cfg, cpu(75.0), two_alerts())
        message = self.sent_email(data)
        self.assertEqual(data["message_title"], message["Subject"])
        self.assertEqual(data["message_body"], message.get_content())
        self.assertTrue(data["message_title"].endswith("(+1 more)"))
        empty = sm.build_payload(cfg, cpu(10.0), [])
        self.assertEqual((empty["message_title"], empty["message_body"]), ("", ""))


class TopProcessTests(unittest.TestCase):
    PAGE = 4096
    # pid: (comm as the kernel shows it, resident pages)
    PROCS = {
        1: ("mysqld", 256),
        2: ("php-fpm: pool www", 332),
        3: ("node", 98),
        4: ("weird/bin/secret-tool --password=hunter2", 25),
        5: ("tmux: server)(x", 40),  # parentheses inside the name
    }

    def fake_proc(self, procs=None):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        for pid, (name, pages) in (procs or self.PROCS).items():
            folder = root / str(pid)
            folder.mkdir()
            (folder / "statm").write_text(f"9999 {pages} 10 1 0 5 0\n")
            (folder / "stat").write_text(f"{pid} ({name}) S 1 1 1 0 -1 4194560\n")
        (root / "self").mkdir()  # non-numeric entries are ignored
        return root

    def memory(self, count=3, root=None):
        root = root or self.fake_proc()
        with mock.patch.object(sm, "psutil", None), \
                mock.patch.object(sm, "PROC", str(root)), \
                mock.patch.object(sm.os, "sysconf", create=True, return_value=self.PAGE), \
                mock.patch.object(sm, "get_memory_usage",
                                  return_value={"total_bytes": 16 * 1024 ** 3}), \
                mock.patch.object(sm.subprocess, "run") as run:
            rows = sm.top_memory(count)
        run.assert_not_called()  # no `ps`: works on busybox/Alpine
        return rows

    def test_memory_ranked_by_rss_with_name_only(self):
        rows = self.memory()
        self.assertEqual(rows[0]["name"], "php-fpm:")  # largest rss first
        self.assertEqual(rows[0]["rss_bytes"], 332 * self.PAGE)
        self.assertEqual(rows[0]["percent"], round(100.0 * 332 * self.PAGE / 16 / 1024 ** 3, 1))
        self.assertEqual(len(rows), 3)

    def test_names_never_carry_arguments(self):
        rows = self.memory(count=10)
        names = " ".join(r["name"] for r in rows)
        self.assertNotIn("hunter2", names)
        self.assertNotIn("--password", names)
        self.assertIn("tmux:", names)
        self.assertTrue(all(len(r["name"]) <= sm.TOP_NAME_WIDTH for r in rows))

    def test_memory_skips_process_that_vanishes_mid_scan(self):
        root = self.fake_proc()
        (root / "2" / "statm").unlink()  # gone between listing and reading
        names = [r["name"] for r in self.memory(count=10, root=root)]
        self.assertNotIn("php-fpm:", names)
        self.assertIn("mysqld", names)

    def test_memory_with_missing_or_unreadable_proc_is_empty(self):
        self.assertEqual(self.memory(root=Path(tempfile.gettempdir()) / "no-such-proc-dir"), [])
        root = self.fake_proc()
        with mock.patch.object(sm.os, "listdir", side_effect=PermissionError):
            self.assertEqual(self.memory(root=root), [])
        for pid in self.PROCS:  # every pid unreadable
            (root / str(pid) / "stat").unlink()
        self.assertEqual(self.memory(root=root), [])

    def test_cpu_from_proc_deltas_not_ps(self):
        first = {1: ("busy", 100), 2: ("idle", 5), 3: ("gone-later", 9)}
        second = {1: ("busy", 150), 2: ("idle", 5), 4: ("new", 80)}
        with mock.patch.object(sm, "psutil", None), \
                mock.patch.object(sm, "_proc_cpu_ticks", side_effect=[first, second]), \
                mock.patch.object(sm.time, "sleep"), \
                mock.patch.object(sm.time, "monotonic", side_effect=[0, 0, 1, 1, 1, 1]), \
                mock.patch.object(sm.os, "sysconf", create=True, return_value=100), \
                mock.patch.object(sm.subprocess, "run") as run:
            rows = sm.top_cpu(3)
        self.assertEqual(rows, [{"name": "busy", "percent": 50.0}])
        run.assert_not_called()

    def alerts_for(self, *metrics):
        return [{"metric": m, "value": 90.0, "threshold": 90, "previous_threshold": 0,
                 "message": m} for m in metrics]

    def test_collected_only_for_alerting_metrics(self):
        cfg = make_cfg()
        with mock.patch.object(sm, "top_memory", return_value=[{"name": "a"}]) as mem, \
                mock.patch.object(sm, "top_cpu", return_value=[{"name": "b"}]) as cpu_:
            self.assertEqual(sm.collect_top_processes(cfg, []), {})
            self.assertEqual(sm.collect_top_processes(cfg, self.alerts_for("disk")), {})
            self.assertEqual(sm.collect_top_processes(cfg, self.alerts_for("memory")),
                             {"memory": [{"name": "a"}]})
            mem.assert_called_once_with(3)
            cpu_.assert_not_called()

    def test_no_collection_on_recovery_or_when_disabled(self):
        cfg = make_cfg()
        recovery = self.alerts_for("cpu")
        recovery[0]["threshold"] = 0
        with mock.patch.object(sm, "top_cpu") as top:
            self.assertEqual(sm.collect_top_processes(cfg, recovery), {})
            cfg["top_processes"]["enabled"] = False
            self.assertEqual(sm.collect_top_processes(cfg, self.alerts_for("cpu")), {})
        top.assert_not_called()

    def test_failures_yield_no_list(self):
        cfg = make_cfg()
        for error in (FileNotFoundError("ps"), PermissionError("hidepid"),
                      sm.subprocess.TimeoutExpired("ps", 3)):
            with mock.patch.object(sm, "top_memory", side_effect=error):
                self.assertEqual(sm.collect_top_processes(cfg, self.alerts_for("memory")), {})

    def test_alert_is_still_sent_when_lookup_fails(self):
        cfg = make_cfg(webhook=True)
        with mock.patch.object(sm, "top_memory", side_effect=OSError("denied")), \
                mock.patch.object(sm, "send_webhook", return_value=(True, "HTTP 200")) as hook:
            top = sm.collect_top_processes(cfg, self.alerts_for("memory"))
            data = sm.build_payload(cfg, cpu(95.0), ALERT, top)
            sm.deliver(cfg, data)
        self.assertNotIn("top_processes", hook.call_args[0][1])
        self.assertNotIn("Top processes", hook.call_args[0][1]["message_body"])

    def test_rendering_and_payload(self):
        top = {"memory": [{"name": "php-fpm", "percent": 8.2, "rss_bytes": 1395864371},
                          {"name": "mysqld", "percent": 6.1, "rss_bytes": 1073741824},
                          {"name": "node", "percent": 2.4, "rss_bytes": 414187520}]}
        data = sm.build_payload(make_cfg(), cpu(95.0), ALERT, top)
        self.assertEqual(data["top_processes"], top)
        self.assertIn("\nTop processes by memory:\n"
                      "  1. php-fpm  8.2% (1.3 GB)\n"
                      "  2. mysqld   6.1% (1.0 GB)\n"
                      "  3. node     2.4% (395 MB)\n", data["message_body"])
        self.assertLess(data["message_body"].index("Top processes"),
                        data["message_body"].index("Current metrics"))

    def test_cpu_rendering(self):
        top = {"cpu": [{"name": "php", "percent": 45.0}]}
        data = sm.build_payload(make_cfg(), cpu(95.0), ALERT, top)
        self.assertIn("Top processes by CPU:\n  1. php  45.0%\n", data["message_body"])


class UptimeTests(unittest.TestCase):
    def fake_proc(self, uptime="273120.55 1000.0\n", boot_id="abc-123\n"):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        if uptime is not None:
            (root / "uptime").write_text(uptime)
        if boot_id is not None:
            (root / "sys" / "kernel" / "random").mkdir(parents=True)
            (root / "sys" / "kernel" / "random" / "boot_id").write_text(boot_id)
        return root

    def read(self, root, psutil=None):
        with mock.patch.object(sm, "PROC", str(root)), mock.patch.object(sm, "psutil", psutil):
            return sm.get_uptime()

    def test_linux_reads_proc(self):
        with mock.patch.object(sm.time, "time", return_value=1_000_000.0):
            up = self.read(self.fake_proc())
        self.assertEqual(up, {"seconds": 273120, "boot_time": 726879,
                              "boot_id": "abc-123"})

    def test_proc_without_boot_id(self):
        self.assertIsNone(self.read(self.fake_proc(boot_id=None))["boot_id"])

    def test_psutil_when_there_is_no_proc(self):
        fake = mock.Mock()
        fake.boot_time.return_value = 900_000.0
        with mock.patch.object(sm.time, "time", return_value=903_600.0):
            up = self.read(self.fake_proc(uptime=None), psutil=fake)
        self.assertEqual(up, {"seconds": 3600, "boot_time": 900_000, "boot_id": None})

    def test_nothing_available_is_none(self):
        self.assertIsNone(self.read(self.fake_proc(uptime=None)))
        self.assertIsNone(self.read(self.fake_proc(uptime="garbage")))

    def test_human_uptime(self):
        self.assertEqual(sm.human_uptime(30), "0m")
        self.assertEqual(sm.human_uptime(12 * 60), "12m")
        self.assertEqual(sm.human_uptime(3600 + 5 * 60), "1h 5m")
        self.assertEqual(sm.human_uptime(3 * 86400 + 4 * 3600 + 12 * 60), "3d 4h 12m")
        self.assertEqual(sm.human_uptime(86400), "1d 0h 0m")

    def test_metric_items_and_payload(self):
        metrics = {**cpu(10.0), "uptime": {"seconds": 90060, "boot_time": 1, "boot_id": "x"}}
        self.assertEqual(sm.metric_items(metrics), ["CPU 10%", "UPTIME 1d 1h 1m"])
        self.assertEqual(sm.metric_lines(metrics)[1], "UPTIME   1d 1h 1m")
        data = sm.build_payload(make_cfg(), metrics, ALERT)
        self.assertEqual(data["metrics"]["uptime"]["seconds"], 90060)
        self.assertIn("UPTIME 1d 1h 1m", data["message_body"])

    def test_no_uptime_alerts_and_collect_includes_it(self):
        cfg = make_cfg()
        up = {"seconds": 5, "boot_time": 1, "boot_id": None}
        with mock.patch.object(sm, "get_uptime", return_value=up):
            metrics = sm.collect_metrics(cfg, 10.0)
        self.assertEqual(metrics["uptime"], up)
        alerts, _ = sm.build_alerts(cfg, metrics, {})
        self.assertEqual(alerts, [])


class NewConfigKeysTests(unittest.TestCase):
    def test_old_config_without_new_keys_uses_defaults(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "config.json"
        path.write_text(json.dumps({"server_name": "OLD"}))
        before = path.read_bytes()
        cfg = sm.load_config(path)
        self.assertEqual(cfg["sustain_checks"], 1)
        self.assertEqual(cfg["top_processes"], {"enabled": True, "count": 3})
        self.assertEqual(path.read_bytes(), before)

    def test_partial_top_processes_is_merged(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "config.json"
        path.write_text(json.dumps({"top_processes": {"count": 5}}))
        self.assertEqual(sm.load_config(path)["top_processes"], {"enabled": True, "count": 5})


class ConfigLocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.plain = self.root / "app"
        self.pkg = self.root / "venv" / "lib" / "python3.12" / "site-packages"
        for folder in (self.plain, self.pkg):
            folder.mkdir(parents=True)

    def resolve(self, script, explicit=None, platform="linux", **env):
        env.setdefault("HOME", str(self.home))
        with mock.patch.dict(sm.os.environ, env, clear=True), \
                mock.patch.object(sm.Path, "home", return_value=self.home), \
                mock.patch.object(sm.sys, "platform", platform), \
                mock.patch.object(sm, "SCRIPT", script / "sosmonitor.py"):
            return sm.resolve_config_path(explicit)

    def test_explicit_config_wins_over_everything(self):
        (self.pkg / "config.json").write_text("{}")
        explicit = str(self.root / "other.json")
        self.assertEqual(self.resolve(self.pkg, explicit), Path(explicit))

    def test_config_next_to_script_beats_user_dir(self):
        (self.pkg / "config.json").write_text("{}")
        self.assertEqual(self.resolve(self.pkg), self.pkg / "config.json")

    def test_package_uses_user_dir_xdg_and_fallback(self):
        xdg = self.root / "xdg"
        self.assertEqual(self.resolve(self.pkg, XDG_CONFIG_HOME=str(xdg)),
                         xdg / "sosmonitor" / "config.json")
        self.assertEqual(self.resolve(self.pkg),
                         self.home / ".config" / "sosmonitor" / "config.json")

    def test_dist_packages_counts_as_package(self):
        deb = self.root / "usr" / "lib" / "python3" / "dist-packages"
        deb.mkdir(parents=True)
        self.assertEqual(self.resolve(deb), self.home / ".config" / "sosmonitor" / "config.json")

    def test_package_on_windows_uses_appdata(self):
        appdata = self.root / "AppData"
        path = self.resolve(self.pkg, platform="win32", APPDATA=str(appdata))
        self.assertEqual(path, appdata / "SOSMonitor" / "config.json")

    def test_plain_script_without_config_uses_user_dir(self):
        self.assertEqual(self.resolve(self.plain),
                         self.home / ".config" / "sosmonitor" / "config.json")

    def test_plain_script_with_config_beside_keeps_it(self):
        (self.plain / "config.json").write_text("{}")
        self.assertEqual(self.resolve(self.plain), self.plain / "config.json")

    def test_log_state_lock_follow_resolved_config(self):
        user_dir = self.root / "xdg" / "sosmonitor"
        with mock.patch.dict(sm.os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}), \
                mock.patch.object(sm, "SCRIPT", self.pkg / "sosmonitor.py"), \
                mock.patch.object(sm, "crontab_read", return_value=("ok", "")), \
                mock.patch.object(sm, "crontab_write", return_value=(True, "")), \
                mock.patch.object(sm, "install_shortcut", return_value=SHORTCUT_OK):
            code, out = cli("install")
        self.assertEqual(code, 0)
        self.assertTrue((user_dir / "config.json").exists())
        cfg = sm.load_config(user_dir / "config.json")
        for name in (sm.LOG_FILE, sm.STATE_FILE, sm.LOCK_FILE):
            self.assertEqual(sm.file_in(cfg, name), user_dir / name)

    def test_existing_files_are_never_moved_or_rewritten(self):
        beside = self.pkg / "config.json"
        beside.write_text('{ "server_name": "KEEP" }')
        state = self.pkg / sm.STATE_FILE
        state.write_text("{}")
        before = {f.name: f.read_bytes() for f in (beside, state)}
        with mock.patch.dict(sm.os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}), \
                mock.patch.object(sm, "SCRIPT", self.pkg / "sosmonitor.py"), \
                mock.patch.object(sm, "run_once"):
            cli()
        self.assertEqual({f.name: f.read_bytes() for f in (beside, state)}, before)
        self.assertFalse((self.root / "xdg").exists())


class FakeTerminal(io.StringIO):
    interactive = True

    def isatty(self):
        return self.interactive


class ConfigCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "config.json"

    def run_config(self, tty=True, env=None, which=lambda name: "/usr/bin/" + name,
                   editor_effect=None, answers=()):
        env = {"EDITOR": "", "VISUAL": ""} if env is None else env
        out, err = FakeTerminal(), io.StringIO()
        out.interactive = tty
        with mock.patch.dict(sm.os.environ, env), \
                mock.patch.object(sm.shutil, "which", side_effect=which), \
                mock.patch.object(sm.sys.stdin, "isatty", return_value=tty), \
                mock.patch("builtins.input", side_effect=list(answers)) as ask, \
                mock.patch.object(sm.subprocess, "run", side_effect=editor_effect) as run, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sm.cmd_config(self.path)
        return code, out.getvalue(), err.getvalue(), run, ask

    def test_creates_missing_config_and_opens_editor(self):
        code, out, _, run, _ = self.run_config(env={"EDITOR": "vim -u NONE", "VISUAL": ""})
        self.assertEqual(code, 0)
        self.assertTrue(self.path.exists())
        self.assertIn("Config created", out)
        run.assert_called_once_with(["vim", "-u", "NONE", str(self.path)])

    def test_visual_beats_editor_then_nano_then_vi(self):
        _, _, _, run, _ = self.run_config(env={"EDITOR": "vim", "VISUAL": "code -w"})
        self.assertEqual(run.call_args[0][0][0], "code")
        _, _, _, run, _ = self.run_config()
        self.assertEqual(run.call_args[0][0][0], "nano")
        _, _, _, run, _ = self.run_config(which=lambda n: "/bin/vi" if n == "vi" else None)
        self.assertEqual(run.call_args[0][0][0], "vi")

    def test_windows_falls_back_to_notepad(self):
        with mock.patch.object(sm.sys, "platform", "win32"):
            _, _, _, run, _ = self.run_config()
        self.assertEqual(run.call_args[0][0][0], "notepad")

    def test_without_tty_prints_path_and_command(self):
        code, out, _, run, _ = self.run_config(tty=False, env={"EDITOR": "vim", "VISUAL": ""})
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn(str(self.path), out)
        self.assertIn(f"vim {sm.shlex.quote(str(self.path))}", out)

    def test_without_any_editor_prints_nano_command_and_never_opens(self):
        code, out, _, run, _ = self.run_config(which=lambda name: None)
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn(f"nano {sm.shlex.quote(str(self.path))}", out)

    def test_invalid_json_is_reported_and_reopen_offered(self):
        def break_it(cmd, *a, **k):
            Path(cmd[-1]).write_text("{ nope")
        code, _, err, run, ask = self.run_config(editor_effect=break_it, answers=["y"])
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 2)  # reopened once, then gave up
        ask.assert_called_once()
        self.assertEqual(err.count("Invalid config"), 2)
        self.assertIn("Invalid JSON", err)

    def test_fixed_on_second_try_succeeds(self):
        texts = iter(["{ nope", '{"server_name": "X"}'])

        def edit(cmd, *a, **k):
            Path(cmd[-1]).write_text(next(texts))
        code, out, _, run, _ = self.run_config(editor_effect=edit, answers=[""])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)
        self.assertIn("Config OK", out)

    def test_declining_reopen_stops(self):
        def break_it(cmd, *a, **k):
            Path(cmd[-1]).write_text("[]")
        code, _, err, run, _ = self.run_config(editor_effect=break_it, answers=["n"])
        self.assertEqual(code, 1)
        run.assert_called_once()
        self.assertIn("JSON object", err)

    def test_cli_command_goes_through_main(self):
        with mock.patch.object(sm, "cmd_config", return_value=0) as cmd:
            code, _ = cli("-c", str(self.path), "config")
        self.assertEqual(code, 0)
        cmd.assert_called_once_with(self.path.resolve())


class EntryPointInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        (self.home / ".local" / "bin").mkdir(parents=True)
        self.script = root / "venv" / "lib" / "site-packages" / "sosmonitor.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("# package copy\n")
        self.bin = root / "venv" / "bin"
        self.bin.mkdir()
        self.entry = self.bin / "sosmonitor"
        self.entry.write_text("#!/bin/sh\n")
        self.entry.chmod(0o755)
        self.bashrc = self.home / ".bashrc"
        self.bashrc.write_text("alias a=b\n")
        for patch in (
            mock.patch.dict(sm.os.environ, {"HOME": str(self.home),
                                            "PATH": f"{self.bin}:/usr/bin"}),
            mock.patch.object(sm, "SCRIPT", self.script),
            mock.patch.object(sm.sys, "executable", str(self.bin / "python")),
            mock.patch.object(sm, "shell_finds_command", return_value=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.cfg = make_cfg()

    def install(self, current=""):
        out = io.StringIO()
        with mock.patch.object(sm, "crontab_read", return_value=read_result(current)), \
                mock.patch.object(sm, "crontab_write", return_value=(True, "")) as write, \
                contextlib.redirect_stdout(out):
            sm.cmd_install(self.cfg, Path("/srv/config.json"))
        return write, out.getvalue()

    def test_existing_command_means_no_link_and_no_startup_changes(self):
        write, out = self.install()
        self.assertNotIn("ensurepath", out)
        self.assertFalse((self.home / ".local" / "bin" / "sosmonitor").exists())
        self.assertEqual(self.bashrc.read_text(), "alias a=b\n")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".bashrc", ".local"])
        self.assertIn("Command    already   sosmonitor (", out)
        self.assertEqual(self.script.read_text(), "# package copy\n")
        self.assertFalse(self.script.stat().st_mode & 0o111)  # not chmod'ed either

    def test_cron_line_calls_the_entry_point_with_absolute_path(self):
        write, _ = self.install()
        line = write.call_args[0][0][-1]
        self.assertIn(f'"{self.entry}" -c "/srv/config.json" run', line)
        self.assertNotIn("sosmonitor.py", line)

    def test_entry_point_off_path_is_recognized_and_hints_the_path_fix(self):
        before = self.bashrc.read_bytes()
        with mock.patch.dict(sm.os.environ, {"PATH": "/usr/bin"}):
            write, out = self.install()
        self.assertIn(f'"{self.entry}" -c', write.call_args[0][0][-1])
        self.assertIn("Command    already", out)
        self.assertFalse((self.home / ".local" / "bin" / "sosmonitor").exists())
        self.assertEqual(self.bashrc.read_bytes(), before)
        self.assertIn("pipx ensurepath", out)
        self.assertIn(str(self.entry), out)

    def test_uninstall_with_entry_point_off_path_removes_no_shortcut(self):
        with mock.patch.dict(sm.os.environ, {"PATH": "/usr/bin"}), \
                mock.patch.object(sm, "remove_shortcut") as remove, \
                mock.patch.object(sm, "crontab_read", return_value=("ok", "")), \
                contextlib.redirect_stdout(io.StringIO()):
            sm.cmd_uninstall()
        remove.assert_not_called()

    def test_hint_uses_short_name_only_when_the_shell_finds_it(self):
        self.assertEqual(sm._edit_command(None, Path("/c.json")), "sosmonitor config")
        with mock.patch.dict(sm.os.environ, {"PATH": "/usr/bin"}):
            self.assertEqual(sm._edit_command(None, Path("/c.json")), f"{self.entry} config")
            self.entry.unlink()
            with mock.patch.object(sm.shutil, "which", return_value=None):
                self.assertEqual(sm._edit_command(None, Path("/c.json")),
                                 f"python3 {self.script} config")

    def test_without_entry_point_behavior_is_unchanged(self):
        self.entry.unlink()
        with mock.patch.object(sm.shutil, "which", return_value=None):
            write, out = self.install()
        self.assertIn(f'"{sys.executable}" "{self.script}" -c', write.call_args[0][0][-1])
        self.assertTrue((self.home / ".local" / "bin" / "sosmonitor").is_symlink())
        self.assertIn("Command    ok        sosmonitor (~/.local/bin/sosmonitor)", out)

    def test_curl_install_ignores_package_lookup(self):
        with mock.patch.object(sm, "SCRIPT", self.home / "mon" / "sosmonitor.py"):
            self.assertIsNone(sm.find_entry_point())

    def test_uninstall_leaves_alone_what_it_did_not_create(self):
        self.bashrc.write_text(f"alias a=b\n{sm.CRON_MARK}\n{sm.PATH_LINE}\n")
        before = self.bashrc.read_bytes()
        with mock.patch.object(sm, "crontab_read", return_value=("ok", "")), \
                contextlib.redirect_stdout(io.StringIO()):
            sm.cmd_uninstall()
        self.assertEqual(self.bashrc.read_bytes(), before)

    def test_link_to_this_script_is_not_an_entry_point(self):
        link = self.bin / "sosmonitor"
        link.unlink()
        link.symlink_to(self.script)
        with mock.patch.object(sm.shutil, "which", return_value=str(link)), \
                mock.patch.object(sm, "bin_dir", return_value=self.bin):
            self.assertIsNone(sm.find_entry_point())
            with contextlib.redirect_stdout(io.StringIO()):
                sm.remove_shortcut()  # our own link is still removed
        self.assertFalse(link.exists())


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    def test_metadata(self):
        for needle in ('name = "sosmonitor"', 'requires-python = ">=3.8"',
                       'sosmonitor = "sosmonitor:main"', 'py-modules = ["sosmonitor"]',
                       'psutil = ["psutil>=5.9"]', "dependencies = []"):
            self.assertIn(needle, self.text)

    def test_version_comes_from_the_constant(self):
        self.assertIn('version = {attr = "sosmonitor.VERSION"}', self.text)
        self.assertNotRegex(self.text, r'(?m)^version\s*=\s*"')
        self.assertRegex((ROOT / "sosmonitor.py").read_text(), r'(?m)^VERSION = "[^"]+"')

    def test_changelog_has_current_version(self):
        self.assertIn(f"## {sm.VERSION} ", (ROOT / "CHANGELOG.md").read_text())


class LockTests(unittest.TestCase):
    def test_second_lock_is_refused(self):
        cfg = make_cfg()
        first = sm.acquire_lock(cfg)
        try:
            self.assertIsNotNone(first)
            self.assertIsNone(sm.acquire_lock(cfg))
        finally:
            first.close()
        second = sm.acquire_lock(cfg)
        self.assertIsNotNone(second)
        second.close()


if __name__ == "__main__":
    unittest.main()
