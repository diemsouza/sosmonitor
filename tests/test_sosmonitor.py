import contextlib
import io
import json
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
        self.assertIn("CPU      95%", body)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_example_file_matches_template(self):
        example = json.loads((ROOT / "config.example.json").read_text())
        self.assertEqual(example, sm.build_template("MY-SERVER"))

    def test_missing_config_is_created_without_running(self):
        with mock.patch.object(sm, "run_once") as run:
            code, out = cli("-c", str(self.path))
        self.assertEqual(code, 0)
        self.assertIn("Config created", out)
        self.assertTrue(self.path.exists())
        run.assert_not_called()

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

    def test_default_config_is_next_to_script(self):
        default = Path(self.tmp.name) / "config.json"
        with mock.patch.object(sm, "SCRIPT", Path(self.tmp.name) / "sosmonitor.py"):
            code, out = cli()
        self.assertEqual(code, 0)
        self.assertTrue(default.exists())
        self.assertIn(str(default), out)

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
        with mock.patch.object(sm, "crontab_read", return_value=""), \
                mock.patch.object(sm, "crontab_write"), \
                mock.patch.object(sm, "install_shortcut"):
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
        with mock.patch.object(sm, "crontab_read", return_value=current), \
                mock.patch.object(sm, "crontab_write") as write, \
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
        self.assertEqual(code, 1)
        write.assert_not_called()
        self.assertIn(f'-c "{self.path}" run', out)

    def test_install_on_windows_prints_schtasks(self):
        _, _, out = self.install(None, os_name="nt")
        self.assertIn("schtasks /Create /SC MINUTE /MO 5 /TN SOSMonitor", out)

    def test_uninstall_removes_only_tagged_line(self):
        current = f"0 * * * * backup\n* * * * * x {sm.CRON_MARK}\n"
        with mock.patch.object(sm, "crontab_read", return_value=current), \
                mock.patch.object(sm, "crontab_write") as write, \
                mock.patch.object(sm, "remove_shortcut"), \
                contextlib.redirect_stdout(io.StringIO()):
            sm.cmd_uninstall()
        write.assert_called_once_with(["0 * * * * backup"])


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
        self.assertIn("- SOSMonitor test", out)
        self.assertIn("CPU      12%", out)
        self.assertIn("No channel enabled", out)
        hook.assert_not_called()

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
        return self.run_quiet(sm.install_shortcut)

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
        with mock.patch.object(sm, "uninstall_cron"):
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
        self.assertIn("WARNING", out)
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
        self.assertIn("WARNING", out)
        self.assertIn(str(self.link), out)
        self.assertNotIn("Command installed", out)
        self.assertIn(sm.PATH_LINE, out)

    def test_install_wiring(self):
        cfg, path = make_cfg(), Path("/x/config.json")
        with mock.patch.object(sm, "install_cron", return_value=0), \
                mock.patch.object(sm, "install_shortcut") as shortcut:
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
