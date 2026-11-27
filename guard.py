"""Keep Windows awake through all local Codex and DeepSeek desktop tasks."""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from activity import Monitor, Snapshot
from i18n import APP_NAME, LANGUAGES, VERSION, current_language, language_setting, select_language, title as app_title, tr
from plugin_host import PLUGIN_ID, Plugins
from source_host import open_task_sources
import windows_power as power

CANCEL_NOTICE = 10
ACTION_LABELS = {"sleep": "Sleep", "hibernate": "Hibernate", "shutdown": "Shut down", "none": "Keep awake"}
CONFIG_CHECK_SECONDS = 1.0
RUN_LOG = logging.getLogger("codex_s4_guard.run")
RUN_LOG.setLevel(logging.INFO)
RUN_LOG.propagate = False
CONFIG_TEMPLATE = """[guard]
# These three settings reload while the Guard is running.
delay_seconds = 180
poll_seconds = 2
completion_action = hibernate
# Language and other settings below require restarting the Guard.
language = auto
keep_lid_policy = false
ui_mode = tray
disabled_plugins =
codex_dir =
deepseek_dir =
node =
"""


class GuardParser(argparse.ArgumentParser):
    def error(self, message):
        if sys.stderr is None and os.name == "nt":
            power.desktop_notice(message, error=True)
        super().error(message)


def ensure_config(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8-sig") as stream:
            stream.write(CONFIG_TEMPLATE)
    except FileExistsError:
        pass


def edit_config(path):
    ensure_config(path)
    subprocess.Popen([str(power.system_exe("notepad.exe")), str(path)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)


def save_config_value(path, key, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    original = read_config(path)
    source = CONFIG_TEMPLATE if original is None else original.decode("utf-8-sig")
    settings = configparser.ConfigParser(interpolation=None)
    settings.read_string(source)
    updated, count = re.subn(rf"(?im)^([ \t]*{re.escape(key)}[ \t]*[=:])[ \t]*[^\r\n]*",
                             rf"\g<1> {value}", source, count=1)
    if not count:
        newline = "\r\n" if "\r\n" in source else "\n"
        updated = source.rstrip("\r\n") + newline + f"{key} = {value}" + newline
    result = configparser.ConfigParser(interpolation=None)
    result.read_string(updated)
    settings["guard"][key] = value
    if set(result.sections()) != {"guard"} or result.defaults() or result["guard"] != settings["guard"]:
        raise ValueError(tr("Could not safely update {key} in config.ini.", key=key))
    encoding = "utf-8" if original is not None and not original.startswith(b"\xef\xbb\xbf") else "utf-8-sig"
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="config-", suffix=".tmp", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(updated.encode(encoding))
        if read_config(path) != original:
            raise ValueError(tr("Another program modified config.ini. Try again."))
        pending.replace(path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def read_config(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def initialize_language(path, override):
    if override is not None:
        select_language(override)
        return
    parser = configparser.ConfigParser(interpolation=None)
    try:
        if source := read_config(path):
            parser.read_string(source.decode("utf-8-sig"))
        select_language(parser.get("guard", "language", fallback="auto"))
    except (OSError, ValueError, KeyError, configparser.Error):
        select_language()


def apply_config(args, path):
    source = read_config(path)
    parse_config(args, path, source)
    return source


def parse_config(args, path, source):
    config = configparser.ConfigParser(interpolation=None)
    if source is not None:
        config.read_string(source.decode("utf-8-sig"), source=str(path))
        if set(config.sections()) != {"guard"} or config.defaults():
            raise ValueError(tr("{path}: use a single [guard] section.", path=path))
    else:
        config.add_section("guard")
    values = config["guard"]
    defaults = {
        "delay_seconds": 180,
        "poll_seconds": 2.0,
        "keep_lid_policy": False,
        "ui_mode": "tray",
        "codex_dir": Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
        "deepseek_dir": Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh"),
        "node": None,
        "block_huawei_s4": True,
        "disabled_plugins": "",
    }
    unknown = values.keys() - defaults.keys() - {"completion_action", "auto_hibernate", "language"}
    if unknown:
        raise ValueError(tr("{path}: unknown settings: {keys}.", path=path, keys=", ".join(sorted(unknown))))
    try:
        language_setting(values.get("language", "auto"))
    except KeyError as exc:
        raise ValueError(tr("language must be auto, en or zh-Hans.")) from exc
    for key, default in defaults.items():
        raw = values.get(key, "").strip()
        if getattr(args, key) is not None:
            continue
        value = default
        if raw:
            if isinstance(default, bool):
                value = values.getboolean(key)
            elif isinstance(default, Path):
                value = Path(os.path.expandvars(raw)).expanduser()
                if not value.is_absolute():
                    value = path.parent / value
            elif default is None:
                value = os.path.expandvars(raw)
            else:
                value = type(default)(raw)
        setattr(args, key, value)
    if args.completion_action is None:
        args.completion_action = values.get("completion_action", "").strip().lower()
        if not args.completion_action:
            legacy = values.get("auto_hibernate", "").strip()
            args.completion_action = "none" if legacy and not values.getboolean("auto_hibernate") else "hibernate"
    if args.completion_action not in ACTION_LABELS:
        raise ValueError(tr("completion_action must be sleep, hibernate, shutdown or none."))
    if args.delay_seconds < 1 or not math.isfinite(args.poll_seconds) or args.poll_seconds < 0.25:
        raise ValueError(tr("The delay must be at least 1 second and the poll interval at least 0.25 seconds."))
    if args.ui_mode not in {"tray", "window"}:
        raise ValueError(tr("ui_mode must be tray or window."))
    disabled = {item.strip() for item in args.disabled_plugins.split(",") if item.strip()}
    if any(not PLUGIN_ID.fullmatch(item) for item in disabled):
        raise ValueError(tr("disabled_plugins must contain comma-separated plugin IDs."))
    if not args.block_huawei_s4:
        disabled.add("huawei-hiview")
    args.disabled_plugins = disabled


@dataclass(frozen=True)
class PowerSettings:
    action: str
    delay: int
    poll: float

    @classmethod
    def from_args(cls, args):
        return cls(args.completion_action, args.delay_seconds, args.poll_seconds)


class LivePowerSettings:
    def __init__(self, path, source, overrides, args):
        self.path, self.source, self.overrides = path, source, overrides
        self.current = PowerSettings.from_args(args)
        self.seen_file = source is not None
        self.error = None
        self.revision = 0

    def refresh(self):
        before, error = self.current, self.error
        try:
            source = read_config(self.path)
            if source is None and self.seen_file:
                raise ValueError(tr("config.ini is missing. Save or restore the file."))
            if source is not None:
                self.seen_file = True
            if source != self.source:
                args = argparse.Namespace(**vars(self.overrides))
                parse_config(args, self.path, source)
                updated = PowerSettings.from_args(args)
                if updated.action != before.action and not args.dry_run:
                    power.require_action_available(updated.action)
                self.current, self.source = updated, source
            self.error = None
        except (OSError, ValueError, configparser.Error, power.GuardError) as exc:
            self.error = str(exc)
        if (before.action, before.delay, error) != (self.current.action, self.current.delay, self.error):
            self.revision += 1
        return (before, error) != (self.current, self.error)

    def wait(self, control):
        until = time.monotonic() + self.current.poll
        while True:
            left = until - time.monotonic()
            if control.cancelled(min(CONFIG_CHECK_SECONDS, max(0.0, left))):
                return True
            if time.monotonic() >= until or self.refresh():
                return False


@dataclass
class Countdown:
    delay: int
    quiet_since: float | None = None
    revision: tuple | None = None

    def remaining(self, snapshot: Snapshot, now: float, input_idle: float) -> float | None:
        changed = snapshot.revision != self.revision
        self.revision = snapshot.revision
        if not snapshot.observed or snapshot.blocked:
            self.quiet_since = None
            return None
        if changed or self.quiet_since is None:
            self.quiet_since = now
        return max(0.0, self.delay - min(now - self.quiet_since, input_idle))


def state_message(snapshot: Snapshot, remaining: float | None, action: str) -> str:
    if snapshot.unavailable:
        return tr("Waiting for readable task state") + " | " + " | ".join(snapshot.unavailable)
    if snapshot.attention:
        return tr("Attention required") + " | " + ", ".join(f"{app} {count}" for app, count in snapshot.attention)
    if snapshot.busy:
        return tr("Tasks active or queued") + " | " + ", ".join(f"{app} {count}" for app, count in snapshot.busy)
    if action == "none":
        return "Keep-awake only | no automatic power action."
    if not snapshot.observed:
        return "Waiting for the first Codex or DeepSeek task."
    if remaining is None:
        return "Waiting for the next confirmed idle snapshot."
    return tr("All observed tasks finished | {action} in {seconds}s of continuous idle.",
              action=tr(ACTION_LABELS[action]), seconds=math.ceil(remaining))


def taskbar_message(snapshot: Snapshot, remaining: float | None, action: str):
    if remaining is not None:
        title = tr("{action} in {seconds}s", action=tr(ACTION_LABELS[action]), seconds=math.ceil(remaining))
        detail = tr("All tasks finished. New tasks or keyboard and mouse activity reset the timer.")
    elif snapshot.unavailable:
        title, detail = tr("Waiting for task status"), tr("Task status is temporarily unavailable. The Guard is still active.")
    elif snapshot.attention:
        title, detail = tr("Tasks need attention"), tr("Check the tasks that need attention. The Guard is still active.")
    elif snapshot.busy:
        title = tr("Tasks running")
        detail = tr("Running or queued: {tasks}", tasks=", ".join(f"{app} {count}" for app, count in snapshot.busy))
    elif action == "none":
        title, detail = tr("Keep awake"), tr("The PC will stay awake after tasks finish.")
    else:
        title = tr("Ready")
        detail = tr("Waiting to confirm idle status.") if snapshot.observed else tr("Waiting for the first Codex or DeepSeek task.")
    return f"{APP_NAME} · {title}", detail


def request_completion(action, dry_run, confirm_fallback):
    if dry_run:
        print(tr("Dry run: would request Windows {action} now.", action=action), flush=True)
        return True
    print(tr("Requesting Windows {action}.", action=action), flush=True)
    RUN_LOG.info("power_requested action=%s", action)
    try:
        mode = power.issue_power_action(action)
    except power.ShutdownLocked as locked:
        RUN_LOG.info("shutdown_locked code=1271")
        if not confirm_fallback():
            RUN_LOG.info("fallback_cancelled")
            return False
        print("Windows rejected shutdown while locked (1271). Requesting Windows hibernate.", flush=True)
        action = "hibernate"
        RUN_LOG.info("power_requested action=%s cause=shutdown_locked", action)
        try:
            mode = power.issue_power_action(action)
        except (OSError, power.GuardError, subprocess.SubprocessError) as exc:
            raise power.GuardError(f"{locked}\n{tr('S4 fallback failed.')} {exc}") from exc
    RUN_LOG.info("power_dispatched action=%s mode=%s", action, mode or "system_request")
    return True


@contextmanager
def apply_lid_plan(control, plan):
    if plan is None:
        yield
        return
    try:
        with control.locked():
            if not control.cancelled():
                plan.begin()
                RUN_LOG.info("lid_plan_active")
        yield
    finally:
        with control.locked():
            if plan.restore():
                RUN_LOG.info("lid_plan_restored")


def watch(args, control, plan, monitor, boot, settings, update_status=None, protection=None, task_sources=None):
    deadline = Countdown(settings.current.delay)
    revision = settings.revision
    last_status = (None, None)
    lid_listener = ExitStack()

    def still_due():
        if control.cancelled() or power.input_idle() < deadline.delay:
            return False
        settings.refresh()
        return settings.revision == revision

    def confirm_fallback():
        latest = monitor.sample(power.applications(boot), task_sources)
        return deadline.remaining(latest, time.monotonic(), power.input_idle()) == 0 and still_due()

    try:
        if plan and not control.cancelled():
            lid_listener.enter_context(power.auto_lock_on_lid_close(control.cancelled))
            if not control.cancelled():
                print(tr("Lid protection is ready. Closing the lid locks the session and keeps tasks running."), flush=True)
        if not control.cancelled() and not args.dry_run:
            RUN_LOG.info("monitoring_ready action=%s", settings.current.action)
        while not control.cancelled():
            settings.refresh()
            action, delay = settings.current.action, settings.current.delay
            if settings.revision != revision:
                revision = settings.revision
                deadline = Countdown(delay)
            if protection:
                protection.require_alive()
            snapshot = monitor.sample(power.applications(boot), task_sources)
            remaining = None
            if action != "none" and settings.error is None:
                remaining = deadline.remaining(snapshot, time.monotonic(), power.input_idle())
            message = (tr("Automatic power action paused | config.ini: {reason}", reason=settings.error) if settings.error
                       else state_message(snapshot, remaining, action))
            if (message, action) != last_status:
                if message != last_status[0]:
                    print(message, flush=True)
                if update_status is not None:
                    if settings.error:
                        title = app_title("Check configuration")
                        detail = tr("Automatic power actions are paused.") + "\n" + settings.error
                    else:
                        title, detail = taskbar_message(snapshot, remaining, action)
                    if protection:
                        detail += "\n" + protection.status
                    update_status(title, detail)
                last_status = (message, action)
            if remaining == 0 and not control.cancelled():
                settings.refresh()
                if settings.revision != revision:
                    continue
                # The restoration step can take time. Resample before issuing a machine-wide action.
                if protection:
                    protection.restore()
                if plan:
                    with control.locked():
                        if plan.restore():
                            RUN_LOG.info("lid_plan_restored")
                snapshot = monitor.sample(power.applications(boot), task_sources)
                confirmed = deadline.remaining(snapshot, time.monotonic(), power.input_idle()) == 0
                if control.cancelled():
                    break
                if confirmed:
                    with control.locked():
                        if still_due() and request_completion(action, args.dry_run, confirm_fallback):
                            return True
                if plan:
                    with control.locked():
                        if not control.cancelled():
                            plan.begin()
                            RUN_LOG.info("lid_plan_active")
                if protection and not control.cancelled():
                    protection.suspend()
            if settings.wait(control):
                break
        print("Monitoring cancelled. Releasing the Windows power request.", flush=True)
        return False
    finally:
        lid_listener.close()


def main():
    locale_parser = GuardParser(add_help=False, allow_abbrev=False)
    locale_parser.add_argument("--language", choices=LANGUAGES)
    locale_args, _ = locale_parser.parse_known_args()
    journal = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CodexS4Guard" / "power-plan.json"
    config = journal.with_name("config.ini")
    initialize_language(config, locale_args.language)
    parser = GuardParser(description=tr(__doc__), add_help=False, allow_abbrev=False)
    parser.add_argument("-h", "--help", action="help", help=tr("show this help message and exit"))
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}",
                        help=tr("show the program version and exit"))
    parser.add_argument("--language", choices=LANGUAGES, help=tr("set the interface language for this launch"))
    parser.add_argument("--delay-seconds", type=int, help=tr("idle time before the power action, default 180 seconds"))
    parser.add_argument("--poll-seconds", type=float, help=tr("task check interval, default 2 seconds"))
    parser.add_argument("--codex-dir", type=Path, help=tr("custom Codex data directory"))
    parser.add_argument("--deepseek-dir", type=Path, help=tr("custom DeepSeek data directory"))
    parser.add_argument("--node", help=tr("path to a Node.js executable with Zstandard support"))
    parser.add_argument("--keep-lid-policy", action="store_true", default=None,
                        help=tr("keep the existing Windows lid action"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--completion-action", choices=ACTION_LABELS, help=tr("power action after all tasks complete"))
    action.add_argument("--keep-awake-only", dest="completion_action", action="store_const", const="none",
                        help=tr("keep Windows awake without an automatic power action"))
    parser.add_argument("--ui-mode", choices=("tray", "window"), help=tr("show either a tray icon or a status window"))
    parser.add_argument("--keep-huawei-policy", dest="block_huawei_s4", action="store_false", default=None,
                        help=tr("skip enabling the installed Huawei plugin"))
    parser.set_defaults(disabled_plugins=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help=tr("show decisions without requesting power changes"))
    mode.add_argument("--status", action="store_true", help=tr("inspect task sources once without changing power"))
    mode.add_argument("--restore-only", action="store_true", help=tr("recover pending Windows and device settings"))
    mode.add_argument("--stop", action="store_true", help=tr("stop the running guard and restore the original power plan"))
    mode.add_argument("--uninstall", action="store_true", help=tr("stop the guard, restore power, and remove this portable executable"))
    mode.add_argument("--edit-config", action="store_true", help="open the optional config.ini in Notepad")
    args = parser.parse_args()
    if os.name != "nt":
        print("This guard requires Windows 10 or 11.", file=sys.stderr)
        return 1
    run_handler = None
    try:
        executable = power.portable_exe() or Path(__file__).resolve().with_name("Agent-Wake-Guard.exe")
        plugins = Plugins(executable, journal.parent)
        if args.uninstall:
            power.uninstall(journal, plugins.protect)
            return 0
        if args.stop:
            with power.stopped(journal), plugins.protect():
                return 0
        if args.edit_config:
            edit_config(config)
            return 0
        if args.status:
            apply_config(args, config)
            boot = power.boot_time()
            monitor = Monitor(args.codex_dir, args.deepseek_dir, boot, time.time(), args.node)
            with open_task_sources(executable) as task_sources:
                snapshot = monitor.sample(power.applications(boot), task_sources)
            with power.Control() as control:
                running = not control.wait_for_owner(0)
            print(json.dumps({
                "guard_running": running, "config_file": str(config),
                "language": current_language(),
                "completion_action": args.completion_action,
                "plugins": plugins.status(),
                "task_sources": list(task_sources),
                "windows_s4_available": bool(power.can_hibernate()),
                "observed": snapshot.observed, "busy": dict(snapshot.busy),
                "attention": dict(snapshot.attention), "unavailable": snapshot.unavailable,
            }, indent=2))
            return 0 if not snapshot.unavailable else 1
        with power.Control(".preview" if args.dry_run else "") as control:
            if not control.start():
                print("Another guard was detected. Cancellation sent.", flush=True)
                if not control.wait_for_owner():
                    raise power.GuardError("The existing guard has not finished recovery. Check its window.")
                if not args.dry_run:
                    with control.startup_locked():
                        # A third launch may already have become the new owner.
                        if not control.wait_for_owner(0):
                            raise power.GuardError("Another launch is already active. Start again to cancel it.")
                        with control.locked():
                            power.LidPlan(journal).restore()
                        with plugins.protect():
                            pass
                print("Guard stopped. The duplicate launch will close; this notice remains.", flush=True)
                if sys.stdout is None:
                    power.desktop_notice(tr("The running Guard was stopped and its power plan was restored."))
                return CANCEL_NOTICE
            plan = power.LidPlan(journal, lease=uuid.uuid4().hex)
            if args.restore_only:
                with control.locked():
                    print("Previous power plan restored." if plan.restore() else "No recovery was needed.")
                with plugins.protect():
                    pass
                return 0
            if not args.dry_run:
                try:
                    journal.parent.mkdir(parents=True, exist_ok=True)
                    run_handler = RotatingFileHandler(journal.with_name("run.log"), maxBytes=128 * 1024,
                                                      backupCount=1, encoding="utf-8")
                    run_handler.setFormatter(logging.Formatter("%(asctime)s pid=%(process)d %(message)s"))
                    RUN_LOG.addHandler(run_handler)
                except OSError:
                    pass
                RUN_LOG.info("started")
            overrides = argparse.Namespace(**vars(args))
            source = apply_config(args, config)
            settings = LivePowerSettings(config, source, overrides, args)
            boot = power.boot_time()
            monitor = Monitor(args.codex_dir, args.deepseek_dir, boot, time.time(), args.node)
            if not args.dry_run:
                with control.locked():
                    plan.restore()
                power.require_action_available(args.completion_action)
            mode_text = (tr("keep-awake only") if args.completion_action == "none"
                         else tr("{action} after {seconds}s idle",
                                 action=tr(ACTION_LABELS[args.completion_action]), seconds=args.delay_seconds))
            print(f"{APP_NAME} | {tr('all local tasks')} | {mode_text}", flush=True)
            if args.dry_run:
                print(tr("Dry run: this window does not change Windows power state."), flush=True)
            active_plan = None if args.dry_run or args.keep_lid_policy else plan
            recovery = power.recover_on_exit(active_plan) if active_plan else nullcontext()

            def remember_mode(mode):
                try:
                    save_config_value(config, "ui_mode", mode)
                except (OSError, ValueError, configparser.Error) as exc:
                    raise power.GuardError(f"{tr('The view changed, but config.ini could not be saved.')}\n{exc}") from exc

            def choose_action(action):
                power.require_action_available(action)
                try:
                    save_config_value(config, "completion_action", action)
                except (OSError, ValueError, KeyError, configparser.Error) as exc:
                    raise power.GuardError(f"{tr('Could not save the power action.')}\n{exc}") from exc

            status = nullcontext() if args.dry_run else power.guard_status(
                args.ui_mode, control.request_stop, lambda: edit_config(config), remember_mode,
                {key: tr(label) for key, label in ACTION_LABELS.items()},
                lambda: settings.current.action, choose_action if overrides.completion_action is None else None,
            )
            completed = False
            with (
                control.on_close(),
                nullcontext() if args.dry_run else power.awake(),
                recovery,
                apply_lid_plan(control, active_plan),
                status as update,
            ):
                if not control.cancelled():
                    devices = nullcontext() if args.dry_run else plugins.protect(True, args.disabled_plugins, update)
                    with devices as protection, open_task_sources(executable) as task_sources:
                        if protection:
                            print(protection.status, flush=True)
                        completed = watch(args, control, active_plan, monitor, boot, settings, update, protection,
                                          task_sources)
            if run_handler:
                RUN_LOG.info("exited reason=%s", "completion" if completed else "stop_requested")
    except KeyboardInterrupt:
        if run_handler:
            RUN_LOG.info("exited reason=keyboard_interrupt")
        return 0
    except (OSError, KeyError, TypeError, ValueError, sqlite3.Error, subprocess.SubprocessError,
            configparser.Error, power.GuardError) as exc:
        if run_handler:
            RUN_LOG.error("exited reason=error type=%s", type(exc).__name__)
        if sys.stderr is None:
            power.desktop_notice(f"{tr('The Guard stopped.')}\n{exc}", error=True)
        else:
            print(tr("Guard stopped: {reason}", reason=exc), file=sys.stderr, flush=True)
        return 1
    finally:
        if run_handler:
            RUN_LOG.removeHandler(run_handler)
            run_handler.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--rollback-worker":
        raise SystemExit(power.rollback_worker(Path(sys.argv[2]), sys.argv[3]))
    raise SystemExit(main())
