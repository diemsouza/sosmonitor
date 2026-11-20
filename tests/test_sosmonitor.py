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
    PS = ("  PID COMMAND            %MEM   RSS\n"
          "    1 mysqld              6.1 1048576\n"
          "    2 php-fpm: pool www   8.2 1363148\n"
          "    3 node                2.4 404480\n"
          "    4 weird/bin/secret-tool --password=hunter2 1.0 100\n")

    def ps(self, **kwargs):
        defaults = dict(returncode=0, stdout=self.PS, stderr="")
        defaults.update(kwargs)
        return mock.patch.object(sm.subprocess, "run",
                                 return_value=mock.Mock(**defaults))

    def memory(self, count=3):
        with mock.patch.object(sm, "psutil", None), \
                mock.patch.object(sm, "get_memory_usage",
                                  return_value={"total_bytes": 16 * 1024 ** 3}), \
                self.ps():
            return sm.top_memory(count)

    def test_memory_ranked_by_rss_with_name_only(self):
        rows = self.memory()
        self.assertEqual(rows[0]["name"], "php-fpm:")  # largest rss first
        self.assertEqual(rows[0]["rss_bytes"], 1363148 * 1024)
        self.assertEqual(len(rows), 3)

    def test_names_never_carry_arguments(self):
        rows = self.memory(count=10)
        names = " ".join(r["name"] for r in rows)
        self.assertNotIn("hunter2", names)
        self.assertNotIn("--password", names)
        self.assertTrue(all(len(r["name"]) <= sm.TOP_NAME_WIDTH for r in rows))

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

    def test_ps_missing_or_failing_gives_empty(self):
        with mock.patch.object(sm, "psutil", None), \
                mock.patch.object(sm.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                sm.top_memory(3)  # swallowed one level up, by collect_top_processes
        with mock.patch.object(sm, "psutil", None), self.ps(returncode=1, stdout=""):
            self.assertEqual(sm.top_memory(3), [])

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
