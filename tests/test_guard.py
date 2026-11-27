import json
import ctypes
import os
import shutil
import subprocess
import struct
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from activity import Monitor, Snapshot
from device_plugins import huawei
from device_plugins.worker import serve
from guard import (ACTION_LABELS, Countdown, LivePowerSettings, PowerSettings, apply_config,
                   apply_lid_plan, main, request_completion, save_config_value, watch)
from i18n import APP_NAME, tr
from plugin_host import PluginProcess, Plugins
from windows_power import GuardError, LidPlan

HERE = Path(__file__).resolve().parents[1]
ORIGINAL = "11111111-1111-1111-1111-111111111111"
MANUAL = "22222222-2222-2222-2222-222222222222"
THREAD = "11111111-1111-1111-1111-111111111111"


def sample(observed=True, busy=(), attention=(), unavailable=(), revision=1):
    return Snapshot(observed, busy, attention, unavailable, (revision,))


def config_arguments(**overrides):
    defaults = dict(completion_action=None, delay_seconds=None, poll_seconds=None, keep_lid_policy=None,
                    ui_mode=None, codex_dir=None, deepseek_dir=None, node=None, block_huawei_s4=None,
                    disabled_plugins=None, dry_run=True)
    return SimpleNamespace(**{**defaults, **overrides})


def live_settings(path, **overrides):
    launch = config_arguments(**overrides)
    args = SimpleNamespace(**vars(launch))
    return args, LivePowerSettings(path, apply_config(args, path), launch, args)


def stub_settings(args):
    settings = Mock()
    settings.current = PowerSettings.from_args(args)
    settings.error, settings.revision = None, 0
    settings.wait.side_effect = lambda control: control.cancelled(args.poll_seconds)
    return settings


class DevicePluginTests(unittest.TestCase):
    def test_no_compatible_device_leaves_vendor_rules_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (
                patch("device_plugins.huawei.CONFIG", root / "Dubai.xml"),
                patch("device_plugins.huawei.RECOVERY", root / "marker"),
                patch("device_plugins.huawei.elevate") as elevate,
                huawei.session(True) as protection,
            ):
                self.assertIsNone(protection)
                elevate.assert_not_called()

    def test_huawei_matches_legacy_rule_and_recovers_when_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            config, marker = Path(temp) / "Dubai.xml", Path(temp) / ".CodexS4Guard-AutoTurnS4"
            config.write_text(
                '<DubaiConfig><Modules><Module ModuleName="AnomalyMs"><Params MachineType="default">'
                '<Param Key="AutoTurnS4" Value="1"/></Params></Module></Modules></DubaiConfig>',
                encoding="utf-8",
            )
            helper, status = Mock(), Mock()
            with (
                patch.object(huawei, "CONFIG", config),
                patch.object(huawei, "RECOVERY", marker),
                patch.object(huawei.power, "process_started", return_value=123),
                patch.object(huawei, "events_for", side_effect=lambda _: nullcontext({"stop": 2})),
                patch.object(huawei, "elevate", return_value=1) as elevate,
                patch.object(huawei, "HuaweiSession", return_value=helper),
                patch.object(huawei.power, "signal", side_effect=lambda _: marker.unlink(missing_ok=True)),
                patch.object(huawei.power, "waited", return_value=0),
                patch.object(huawei.power, "close"),
            ):
                with huawei.session(True, status) as protection:
                    self.assertIs(protection, helper)
                    helper.suspend.assert_called_once()
                marker.touch()
                with huawei.session(False, status) as protection:
                    self.assertIsNone(protection)
                self.assertFalse(marker.exists())
                helper.suspend.assert_called_once()
                self.assertEqual(elevate.call_count, 2)
                self.assertEqual(status.call_count, 2)

    def test_worker_acknowledges_stop_after_cleanup_and_restores_on_eof(self):
        calls = []
        helper = Mock()

        @contextmanager
        def session(enabled, update):
            calls.append(enabled)
            update("title", "detail")
            try:
                yield helper if enabled else None
            finally:
                calls.append("cleanup")

        class Replies(BytesIO):
            def write(self, value):
                if json.loads(value).get("command") == "stop":
                    calls.append("stop ack")
                return super().write(value)

        outgoing = Replies()
        packets = b'{"command":"hello"}\n{"command":"start","enabled":true}\n'
        commands = b'{"command":"check"}\n{"command":"restore"}\n{"command":"suspend"}\n'
        self.assertEqual(serve(session, "test", BytesIO(packets + commands + b'{"command":"stop"}\n'), outgoing), 0)
        self.assertEqual(calls, [True, "cleanup", "stop ack"])
        self.assertEqual(json.loads(outgoing.getvalue().splitlines()[1])["event"], "status")
        helper.require_alive.assert_called_once()
        helper.restore.assert_called_once()
        helper.suspend.assert_called_once()
        calls.clear()
        self.assertEqual(serve(session, "test", BytesIO(packets), Replies()), 0)
        self.assertEqual(calls, [True, "cleanup"])

    def test_optional_plugins_and_recovery_only_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host = Plugins(root / "Guard.exe", root / "state")
            with host.protect(enable=True) as protection:
                self.assertIsNone(protection)
            self.assertEqual(host.status()["installed"], [])
            host.directory.mkdir()
            (host.directory / "fixture.guard-plugin.exe").touch()
            host.recovery.mkdir(parents=True)
            (host.recovery / "fixture.pending").touch()
            output = (b'{"command":"hello","ok":true,"protocol":1}\n'
                      b'{"command":"start","ok":true,"active":false}\n'
                      b'{"command":"stop","ok":true}\n')
            process = SimpleNamespace(stdin=BytesIO(), stdout=BytesIO(output))
            client = PluginProcess(host.directory / "fixture.guard-plugin.exe", process, None)
            with patch("plugin_host.open_plugin", return_value=nullcontext(client)):
                with host.protect(enable=True, disabled={"fixture"}) as protection:
                    self.assertIsNone(protection)
            self.assertEqual(json.loads(process.stdin.getvalue().splitlines()[1]), {"command": "start", "enabled": False})
            self.assertEqual(host.pending(), [])

    def test_unconfirmed_recovery_blocks_power_until_plugin_is_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host = Plugins(root / "Guard.exe", root / "state")
            host.directory.mkdir()
            plugin_file = host.directory / "fixture.guard-plugin.exe"
            plugin_file.touch()
            output = (b'{"command":"hello","ok":true,"protocol":1}\n'
                      b'{"command":"start","ok":true,"active":true}\n'
                      b'{"command":"restore","ok":true}\n')
            process = SimpleNamespace(stdin=BytesIO(), stdout=BytesIO(output))
            client = PluginProcess(plugin_file, process, None)
            with patch("plugin_host.open_plugin", return_value=nullcontext(client)):
                with self.assertRaisesRegex(GuardError, "stop"):
                    with host.protect(enable=True):
                        self.assertEqual(host.pending(), ["fixture"])
            self.assertEqual(host.pending(), ["fixture"])
            plugin_file.unlink()
            with self.assertRaisesRegex(GuardError, "--restore-only"), host.protect():
                self.fail("Missing plugin should block while its recovery is pending")


class ConfigTests(unittest.TestCase):
    def test_power_action_config_and_legacy_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            for config, cli, expected in (
                ("", None, "hibernate"),
                ("auto_hibernate = true", None, "hibernate"),
                ("auto_hibernate = false", None, "none"),
                ("completion_action = Sleep", None, "sleep"),
                ("auto_hibernate = false\ncompletion_action = shutdown", None, "shutdown"),
                ("completion_action = shutdown", "none", "none"),
            ):
                with self.subTest(config=config, cli=cli):
                    path.write_text(f"[guard]\n{config}\n", encoding="utf-8")
                    args = config_arguments(completion_action=cli)
                    apply_config(args, path)
                    self.assertEqual(args.completion_action, expected)
            path.write_text("[guard]\ncompletion_action = typo\n", encoding="utf-8")
            args.completion_action = None
            with self.assertRaisesRegex(ValueError, "completion_action"):
                apply_config(args, path)

    def test_live_settings_reloads_valid_edits_and_pauses_on_invalid_or_missing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            path.write_text("[guard]\ncompletion_action = hibernate\ndelay_seconds = 180\n", encoding="utf-8")
            _, settings = live_settings(path)
            path.write_text("[guard]\ncompletion_action = hibernate\npoll_seconds = 120\n", encoding="utf-8")
            self.assertTrue(settings.refresh())
            self.assertEqual((settings.current.poll, settings.revision), (120, 0))
            path.write_text("[guard]\ncompletion_action = none\ndelay_seconds = 60\n", encoding="utf-8")
            self.assertTrue(settings.refresh())
            self.assertEqual(settings.current, PowerSettings("none", 60, 2))
            path.write_text("[guard]\ncompletion_action = sle", encoding="utf-8")
            self.assertTrue(settings.refresh())
            self.assertIsNotNone(settings.error)
            self.assertEqual(settings.current.action, "none")
            path.unlink()
            self.assertTrue(settings.refresh())
            self.assertIsNotNone(settings.error)
            path.write_text("[guard]\ncompletion_action = shutdown\ndelay_seconds = 45\n", encoding="utf-8")
            self.assertTrue(settings.refresh())
            self.assertEqual(settings.current, PowerSettings("shutdown", 45, 2))
            self.assertIsNone(settings.error)

    def test_live_settings_keeps_launch_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            _, settings = live_settings(path, completion_action="none", delay_seconds=90)
            path.write_text("[guard]\ncompletion_action = shutdown\ndelay_seconds = 1\npoll_seconds = 3\n",
                            encoding="utf-8")
            self.assertTrue(settings.refresh())
            self.assertEqual(settings.current, PowerSettings("none", 90, 3))
            self.assertEqual(settings.revision, 0)

    def test_switching_modes_preserves_other_config_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            original = "[guard]\r\n# 用户设置\r\ndelay_seconds = 5\r\n".encode("utf-8-sig")
            path.write_bytes(original)
            save_config_value(path, "ui_mode", "window")
            self.assertEqual(path.read_bytes(), original + b"ui_mode = window\r\n")
            save_config_value(path, "ui_mode", "tray")
            self.assertEqual(path.read_bytes(), original + b"ui_mode = tray\r\n")
            path.unlink()
            save_config_value(path, "ui_mode", "window")
            self.assertIn("ui_mode = window", path.read_text(encoding="utf-8-sig"))
            self.assertEqual(list(Path(temp).iterdir()), [path])

    def test_action_selector_save_updates_only_one_key_and_reloads(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            original = b"[guard]\r\n# existing settings\r\nui_mode = window\r\ndelay_seconds = 8\r\n"
            path.write_bytes(original + b"completion_action = shutdown\r\n")
            _, settings = live_settings(path)
            save_config_value(path, "completion_action", "none")
            self.assertEqual(path.read_bytes(), original + b"completion_action = none\r\n")
            self.assertTrue(settings.refresh())
            self.assertEqual(settings.current, PowerSettings("none", 8, 2))


class CountdownTests(unittest.TestCase):
    def test_first_run_is_required_and_a_fast_next_run_restarts_delay(self):
        timer = Countdown(20)
        self.assertIsNone(timer.remaining(sample(observed=False), 100, 100))
        self.assertIsNone(timer.remaining(sample(observed=False), 150, 150))
        self.assertEqual(timer.remaining(sample(revision=2), 151, 50), 20)
        self.assertEqual(timer.remaining(sample(revision=2), 170, 60), 1)
        self.assertEqual(timer.remaining(sample(revision=3), 171, 60), 20)
        self.assertEqual(timer.remaining(sample(revision=3), 192, 1), 19)
        self.assertEqual(timer.remaining(sample(revision=3), 213, 22), 0)

    def test_both_apps_and_uncertain_states_must_clear(self):
        timer = Countdown(10)
        self.assertIsNone(timer.remaining(sample(busy=(("Codex", 1), ("DeepSeek", 1))), 0, 100))
        self.assertIsNone(timer.remaining(sample(busy=(("DeepSeek", 1),)), 20, 100))
        self.assertIsNone(timer.remaining(sample(attention=(("DeepSeek", 1),)), 40, 100))
        self.assertIsNone(timer.remaining(sample(unavailable=("partial journal",)), 60, 100))
        self.assertEqual(timer.remaining(sample(revision=3), 65, 100), 10)
        self.assertEqual(timer.remaining(sample(revision=3), 75, 100), 0)


class MockPowercfg:
    def __init__(self, journal):
        self.journal = journal
        self.active = ORIGINAL
        self.plans = {ORIGINAL, MANUAL}
        self.lid = {}

    def __call__(self, verb, *args):
        if verb == "/getactivescheme":
            return self.active
        if verb == "/list":
            return "\n".join(self.plans)
        if verb == "/duplicatescheme":
            assert self.journal.is_file()
            assert args[0] in self.plans
            self.plans.add(args[1])
        if verb in {"/setacvalueindex", "/setdcvalueindex"}:
            assert args[0] not in {ORIGINAL, MANUAL}
            self.lid[verb] = args[-1]
        if verb == "/setactive":
            self.active = args[0]
        if verb == "/qh":
            return "0x00000000\n0x00000000"
        if verb == "/delete":
            assert args[0] not in {ORIGINAL, MANUAL}
            assert self.active != args[0]
            self.plans.remove(args[0])
        return ""


class RecoveryTests(unittest.TestCase):
    def test_main_applies_lid_plan_before_plugin_and_restores_on_startup_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journal = root / "CodexS4Guard" / "power-plan.json"
            driver = MockPowercfg(journal)
            control = Mock()
            control.start.return_value = True
            control.cancelled.return_value = False
            control.locked.return_value = nullcontext()
            control.on_close.return_value = nullcontext()

            def failing_plugin(*_args):
                self.assertNotEqual(driver.active, ORIGINAL)
                self.assertTrue(journal.is_file())
                raise GuardError("Fixture plugin failed")

            with (
                patch.object(sys, "argv", ["guard.py", "--keep-awake-only"]),
                patch.dict(os.environ, {"LOCALAPPDATA": temp}),
                patch("guard.power.Control", return_value=nullcontext(control)),
                patch("guard.power.LidPlan", side_effect=lambda path, **kwargs: LidPlan(path, driver, **kwargs)),
                patch("guard.power.boot_time", return_value=0),
                patch("guard.power.awake", return_value=nullcontext()),
                patch("guard.power.recover_on_exit", return_value=nullcontext()),
                patch("guard.power.guard_status", return_value=nullcontext()),
                patch("guard.Plugins.protect", side_effect=failing_plugin),
            ):
                self.assertEqual(main(), 1)
            self.assertEqual(driver.active, ORIGINAL)
            self.assertFalse(journal.exists())
            log = journal.with_name("run.log").read_text(encoding="utf-8")
            self.assertIn("lid_plan_active", log)
            self.assertIn("lid_plan_restored", log)
            self.assertIn("exited reason=error type=GuardError", log)

    def test_normal_and_crash_recovery_restore_original(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "power-plan.json"
            driver = MockPowercfg(journal)
            LidPlan(journal, driver).begin()
            self.assertNotEqual(driver.active, ORIGINAL)
            self.assertEqual(set(driver.lid.values()), {"0"})
            self.assertTrue(LidPlan(journal, driver).restore())
            self.assertEqual(driver.active, ORIGINAL)
            self.assertEqual(driver.plans, {ORIGINAL, MANUAL})
            self.assertFalse(journal.exists())

    def test_rollback_preserves_user_power_plan_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "power-plan.json"
            driver = MockPowercfg(journal)
            plan = LidPlan(journal, driver)
            plan.begin()
            driver.active = MANUAL
            plan.restore()
            self.assertEqual(driver.active, MANUAL)
            self.assertEqual(driver.plans, {ORIGINAL, MANUAL})

    def test_restore_happens_before_each_action_then_watcher_exits(self):
        for action in ("sleep", "hibernate", "shutdown"):
            with self.subTest(action=action):
                self.check_restore_before_action(action)

    def check_restore_before_action(self, action):
        control = Mock()
        control.cancelled.return_value = False
        control.locked.return_value = nullcontext()
        monitor = Mock()
        monitor.sample.return_value = sample()
        plan = Mock()
        protection = Mock()
        events = []
        plan.restore.side_effect = lambda: events.append("restore")
        protection.restore.side_effect = lambda: events.append("restore_devices")
        args = SimpleNamespace(delay_seconds=5, poll_seconds=1, dry_run=False, completion_action=action)
        with (
            patch("guard.power.applications", return_value={}),
            patch("guard.power.input_idle", return_value=20),
            patch("guard.time.monotonic", side_effect=[0, 5, 5]),
            patch("guard.power.auto_lock_on_lid_close", return_value=nullcontext()),
            patch("guard.power.issue_power_action", side_effect=events.append) as issue,
            apply_lid_plan(control, plan),
        ):
            watch(args, control, plan, monitor, 0, stub_settings(args), protection=protection)
        issue.assert_called_once_with(action)
        self.assertEqual(events, ["restore_devices", "restore", action, "restore"])

    def test_new_task_during_device_restore_rearms_protection(self):
        control = Mock()
        control.cancelled.return_value = False
        control.locked.return_value = nullcontext()
        monitor = Mock()
        monitor.sample.side_effect = [sample(), sample(), sample(busy=(("Codex", 1),), revision=2)]
        protection = Mock()
        protection.suspend.side_effect = lambda: setattr(control.cancelled, "return_value", True)
        args = SimpleNamespace(delay_seconds=5, poll_seconds=1, dry_run=False, completion_action="hibernate")
        with (
            patch("guard.power.applications", return_value={}),
            patch("guard.power.input_idle", return_value=20),
            patch("guard.time.monotonic", side_effect=[0, 5, 5]),
            patch("guard.power.issue_power_action") as issue,
        ):
            watch(args, control, None, monitor, 0, stub_settings(args), protection=protection)
        protection.restore.assert_called_once()
        protection.suspend.assert_called_once()
        issue.assert_not_called()

    def test_none_saved_during_device_restore_cancels_power_action_and_keeps_protection(self):
        control = Mock()
        control.cancelled.return_value = False
        control.locked.return_value = nullcontext()
        monitor = Mock()
        monitor.sample.return_value = sample()
        protection = Mock()
        protection.status = "fixture protection"
        status = Mock()
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.ini"
            config.write_text("[guard]\ncompletion_action = hibernate\ndelay_seconds = 5\n", encoding="utf-8")
            args, settings = live_settings(config, dry_run=False)
            protection.restore.side_effect = lambda: config.write_text("[guard]\ncompletion_action = none\n", encoding="utf-8")

            def wait(_control):
                return settings.current.action == "none" and status.call_args.args[0].endswith(tr("Keep awake"))

            with (
                patch.object(settings, "wait", side_effect=wait),
                patch("guard.power.applications", return_value={}),
                patch("guard.power.input_idle", return_value=20),
                patch("guard.time.monotonic", side_effect=[0, 5, 5]),
                patch("guard.power.issue_power_action") as issue,
            ):
                watch(args, control, None, monitor, 0, settings, status, protection)
            issue.assert_not_called()
            protection.restore.assert_called_once()
            protection.suspend.assert_called_once()

    def test_locked_shutdown_rechecks_new_tasks_before_hibernate(self):
        import windows_power as power

        control = Mock()
        control.cancelled.return_value = False
        control.locked.return_value = nullcontext()
        monitor = Mock()
        monitor.sample.side_effect = [sample(), sample(), sample(), sample(busy=(("Codex", 1),), revision=2)]
        protection = Mock()
        protection.suspend.side_effect = lambda: setattr(control.cancelled, "return_value", True)
        args = SimpleNamespace(delay_seconds=5, poll_seconds=1, dry_run=False, completion_action="shutdown")
        with (
            patch("guard.power.applications", return_value={}),
            patch("guard.power.input_idle", return_value=20),
            patch("guard.time.monotonic", side_effect=[0, 5, 5, 6]),
            patch("guard.power.issue_power_action", side_effect=power.ShutdownLocked("1271")) as issue,
        ):
            watch(args, control, None, monitor, 0, stub_settings(args), protection=protection)
        issue.assert_called_once_with("shutdown")
        protection.suspend.assert_called_once()

    def test_an_old_watchdog_cannot_restore_a_new_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "power-plan.json"
            driver = MockPowercfg(journal)
            plan = LidPlan(journal, driver, lease="new")
            plan.begin()
            self.assertFalse(LidPlan(journal, driver).restore(only_lease="old"))
            self.assertNotEqual(driver.active, ORIGINAL)
            self.assertTrue(plan.restore(only_lease="new"))


def codex_header(originator="Codex Desktop"):
    return (json.dumps({"type": "session_meta", "payload": {"originator": originator}}) + "\n").encode()


def codex_row(kind, when, turn):
    return json.dumps({
        "timestamp": datetime.fromtimestamp(when, timezone.utc).isoformat(),
        "type": "event_msg", "payload": {"type": kind, "turn_id": turn},
    }).encode() + b"\n"


class ActivityTests(unittest.TestCase):
    def test_cli_rollout_does_not_arm_the_guard_even_with_desktop_open(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = root / "codex" / "sessions"
            sessions.mkdir(parents=True)
            now = time.time()
            log = sessions / f"rollout-cli-{THREAD}.jsonl"
            log.write_bytes(codex_header("codex_cli_rs") + codex_row("task_started", now + 1, "turn1"))
            state = Monitor(root / "codex", root / "dsh", now - 100, now).sample({"Codex": now - 30})
            self.assertFalse(state.observed)
            self.assertFalse(state.blocked)

    def test_codex_started_before_guard_and_incomplete_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = root / "codex" / "sessions"
            sessions.mkdir(parents=True)
            log = sessions / f"rollout-x-{THREAD}.jsonl"
            now = time.time()
            log.write_bytes(codex_header() + codex_row("task_started", now - 4, "turn1"))
            monitor = Monitor(root / "codex", root / "dsh", now - 100, now)
            opened = {"Codex": now - 30}
            self.assertEqual(monitor.sample(opened).busy, (("Codex", 1),))
            completion = codex_row("task_complete", now + 1, "turn1")
            with log.open("ab") as stream:
                stream.write(completion[:-2])
            self.assertTrue(monitor.sample(opened).unavailable)
            with log.open("ab") as stream:
                stream.write(completion[-2:])
            completed = monitor.sample(opened)
            self.assertTrue(completed.observed)
            self.assertFalse(completed.blocked)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_parallel_codex_and_deepseek_then_close_one_app(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            c = root / "codex" / "sessions"
            d = root / "dsh" / "sessions" / "space" / "session"
            c.mkdir(parents=True)
            d.mkdir(parents=True)
            now = time.time()
            cfile = c / f"rollout-x-{THREAD}.jsonl"
            dfile = d / "session.v4.jsonl.zstd"
            cfile.write_bytes(codex_header() + codex_row("task_started", now + 1, "turn1"))

            def deep(kind, seq, **data):
                # Each call appends an independent Zstandard frame, matching the desktop journal.
                code = ("const fs=require('fs'),z=require('zlib');"
                        "fs.appendFileSync(process.argv[1],z.zstdCompressSync(fs.readFileSync(0)));")
                row = {"type": kind, "time": int((now + seq) * 1000), "seq": seq, "data": data}
                subprocess.run(["node", "-e", code, str(dfile)], input=json.dumps(row) + "\n",
                               text=True, capture_output=True, check=True)

            deep("turn/start", 1, turn=0)
            monitor = Monitor(root / "codex", root / "dsh", now - 100, now)
            both = {"Codex": now - 20, "DeepSeek": now - 20}
            self.assertEqual(monitor.sample(both).busy, (("Codex", 1), ("DeepSeek", 1)))
            with cfile.open("ab") as stream:
                stream.write(codex_row("task_complete", now + 2, "turn1"))
            self.assertEqual(monitor.sample(both).busy, (("DeepSeek", 1),))
            deep("turn/end", 2, reason={"kind": "completed"}, turn=0)
            self.assertFalse(monitor.sample(both).blocked)
            deep("turn/start", 3, turn=1)
            self.assertTrue(monitor.sample(both).blocked)
            self.assertFalse(monitor.sample({"Codex": now - 20}).blocked)


@unittest.skipUnless(os.name == "nt", "Named cancellation objects require Windows")
class DuplicateTests(unittest.TestCase):
    def test_guard_switches_one_window_and_one_tray_icon_without_stopping(self):
        import windows_power as power
        from ctypes import wintypes

        visible = power.api(power.user, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
        style = power.api(power.user, "GetWindowLongW", wintypes.LONG, wintypes.HWND, ctypes.c_int)
        window = None
        icon_present = False
        visible_during_registration = []
        show_window = power.message_window

        @contextmanager
        def capture(*args, **kwargs):
            nonlocal window
            with show_window(*args, **kwargs) as handle:
                window = handle
                yield handle

        def shell(code, pointer):
            nonlocal icon_present
            if code == 0:
                visible_during_registration.append(bool(visible(pointer._obj.window)))
                exists, icon_present = icon_present, True
                return not exists
            if code == 2:
                exists, icon_present = icon_present, False
                return exists
            return icon_present

        stop = Mock()
        remember = Mock()
        read_action = Mock(return_value="hibernate")
        save_action = Mock()
        with (
            patch.object(power, "notify_icon", shell),
            patch.object(power, "message_window", capture),
            patch.object(power, "foreground"),
            power.guard_status("tray", stop, Mock(), remember, ACTION_LABELS, read_action, save_action),
        ):
            def check(tray):
                self.assertEqual(icon_present, tray)
                self.assertEqual(bool(visible(window)), not tray)

            self.assertEqual(style(window, -20) & 0x40080, 0x80)  # No WS_EX_APPWINDOW.
            check(tray=True)
            power.send_message(window, 0x8002, 0, 0x400)  # NIN_SELECT
            check(tray=False)
            get_control = power.api(power.user, "GetDlgItem", wintypes.HWND, wintypes.HWND, ctypes.c_int)
            selector = get_control(window, 105)
            self.assertEqual(power.send_message(selector, 0x146, 0, 0), 4)  # CB_GETCOUNT
            self.assertEqual(power.send_message(selector, 0x147, 0, 0), 1)  # CB_GETCURSEL
            power.send_message(selector, 0x14E, 3, 0)  # CB_SETCURSEL
            power.send_message(window, 0x111, (9 << 16) | 105, selector)  # CBN_SELENDOK
            save_action.assert_called_once_with("none")
            read_action.return_value = "sleep"
            power.send_message(window, 0x8001, 0, 0)  # Refresh from current config.
            self.assertEqual(power.send_message(selector, 0x147, 0, 0), 0)
            remember.assert_not_called()
            power.send_message(window, power.WM_CLOSE, 0, 0)
            check(tray=True)
            power.send_message(window, 0x111, 104, 0)  # WM_COMMAND, switch
            check(tray=False)
            power.send_message(window, power.register_message("TaskbarCreated"), 0, 0)
            check(tray=False)
            power.send_message(window, 0x111, 104, 0)
            check(tray=True)
            self.assertEqual([call.args for call in remember.call_args_list], [("window",), ("tray",)])
            icon_present = False  # Explorer has lost all icons.
            power.send_message(window, power.register_message("TaskbarCreated"), 0, 0)
            check(tray=True)
            stop.assert_not_called()
            power.send_message(window, 0x111, 101, 0)  # Explicit stop.
            stop.assert_called_once()
        self.assertFalse(icon_present)
        self.assertTrue(visible_during_registration)
        self.assertFalse(any(visible_during_registration))

    def test_windowless_recovery_worker_starts_with_invalid_parent_stderr(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("This Python installation has no windowless interpreter")
        script = r"""
import ctypes
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import windows_power as power

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetConsoleWindow.restype = ctypes.c_void_p
kernel.SetStdHandle.argtypes = (ctypes.c_uint32, ctypes.c_void_p)
assert not kernel.GetConsoleWindow()
assert kernel.SetStdHandle(0xfffffff4, ctypes.c_void_p(-1))  # STD_ERROR_HANDLE
plan = power.LidPlan(Path(sys.argv[2]), lease="headless-test")
with power.recover_on_exit(plan):
    pass
"""
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "power-plan.json"
            result = subprocess.run(
                [str(pythonw), "-I", "-B", "-c", script, str(HERE), str(journal)],
                creationflags=subprocess.DETACHED_PROCESS, timeout=20,
            )
            self.assertEqual(result.returncode, 0)
            self.assertFalse(journal.exists())

    def test_windows_lid_messages_lock_once_per_close_without_locking_the_test_pc(self):
        import windows_power as power
        from ctypes import wintypes

        send = power.api(power.user, "SendMessageW", ctypes.c_ssize_t, wintypes.HWND,
                         wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        requested = Mock()
        cancelled = Mock(return_value=False)
        transition = power.LidLock(cancelled, requested)
        with (
            patch.object(power, "register_power", return_value=1),
            patch.object(power, "unregister_power"),
            power.lid_notifications(transition) as window,
        ):
            def synthetic(value, guid=power.LID_EVENT):
                packet = ctypes.create_string_buffer(guid + struct.pack("<II", 4, value))
                send(window, power.WM_POWERBROADCAST, power.PBT_POWERSETTINGCHANGE,
                     ctypes.addressof(packet))

            synthetic(1)
            requested.assert_not_called()
            synthetic(0, bytes(16))
            requested.assert_not_called()
            synthetic(0)
            synthetic(0)
            requested.assert_called_once()
            synthetic(1)
            cancelled.return_value = True
            synthetic(0)
            requested.assert_called_once()

    def test_duplicate_ends_both_dry_run_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            cmd = [sys.executable, "-I", "-B", str(HERE / "guard.py"), "--dry-run",
                   "--codex-dir", temp, "--deepseek-dir", temp, "--poll-seconds", "0.25", "--language", "en"]
            first = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertIn(APP_NAME, first.stdout.readline())
                second = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
                self.assertEqual(second.returncode, 10, (second.stdout, second.stderr))
                self.assertIn("Cancellation sent", second.stdout)
                self.assertIn("notice remains", second.stdout)
                stdout, stderr = first.communicate(timeout=10)
                self.assertEqual(first.returncode, 0, stderr)
                self.assertIn("Monitoring cancelled", stdout)
            finally:
                if first.poll() is None:
                    first.kill()
                    first.wait(timeout=5)


@unittest.skipUnless(os.name == "nt", "Windows power actions require Win32")
class PowerActionTests(unittest.TestCase):
    def test_sleep_uses_standby_supported_by_hardware(self):
        import windows_power as power

        calls = Mock()
        calls.release.return_value = True
        calls.suspend.return_value = True
        for mode in ("modern", "traditional"):
            with (
                self.subTest(mode=mode),
                patch.object(power, "sleep_mode", return_value=mode),
                patch.object(power, "set_execution", calls.release),
                patch.object(power, "set_suspend_state", calls.suspend),
                patch.object(power, "send_message", calls.message),
                patch.object(power, "message_window", return_value=nullcontext(123)),
                patch.object(power, "shutdown_privilege", return_value=nullcontext()),
            ):
                calls.reset_mock()
                power.issue_power_action("sleep")
                request = call.message(123, 0x112, 0xF170, 2) if mode == "modern" else call.suspend(False, False, False)
                self.assertEqual(calls.mock_calls, [call.release(power.CONTINUOUS), request])

    def test_shutdown_and_hibernate_use_distinct_commands(self):
        import windows_power as power

        with (
            patch.object(power, "set_execution", return_value=True),
            patch.object(power, "can_hibernate", return_value=True),
            patch.object(power.subprocess, "Popen") as spawn,
        ):
            spawn.return_value.wait.return_value = 0
            for action, args in (("hibernate", ["/h"]), ("shutdown", ["/s", "/t", "0"])):
                power.issue_power_action(action)
                self.assertEqual(spawn.call_args.args[0][1:], args)

    def test_only_locked_shutdown_falls_back_to_hibernate(self):
        import windows_power as power

        for result, expected in ((0, 1), (1271, 2), (5, 1)):
            with self.subTest(result=result), patch.object(power, "can_hibernate", return_value=True):
                graceful = Mock()
                graceful.wait.return_value = result
                fallback = Mock()
                fallback.wait.return_value = 0
                confirm = Mock(return_value=True)
                with (
                    patch.object(power, "set_execution", return_value=True) as execution,
                    patch.object(power.subprocess, "Popen", side_effect=[graceful, fallback]) as spawn,
                ):
                    if result == 5:
                        with self.assertRaisesRegex(GuardError, "exit 5"):
                            request_completion("shutdown", False, confirm)
                    else:
                        self.assertTrue(request_completion("shutdown", False, confirm))
                    self.assertEqual(spawn.call_count, expected)
                    self.assertEqual(spawn.call_args_list[0].args[0][1:], ["/s", "/t", "0"])
                    if result == 1271:
                        confirm.assert_called_once()
                        execution.assert_any_call(power.CONTINUOUS | 1)
                        self.assertEqual(spawn.call_args_list[1].args[0][1:], ["/h"])
                    else:
                        confirm.assert_not_called()
