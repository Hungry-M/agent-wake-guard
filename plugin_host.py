"""Run installed device plugins over the v1 JSON-lines protocol."""

import json
import os
import re
import subprocess
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from queue import Empty, Full, Queue

from i18n import LANGUAGE_ENV, current_language, tr
from windows_power import GuardError


PROTOCOL = 1
PLUGIN_SUFFIX = ".guard-plugin.exe"
PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
MAX_FRAME = 65536


class PluginProcess:
    def __init__(self, path, process, update_status):
        self.path = path
        self.process = process
        self.update_status = update_status
        self.messages = Queue(maxsize=16)
        self.reader = threading.Thread(target=self.read, daemon=True, name=f"plugin-{path.stem}")
        self.reader.start()
        self.status = ""
        self.active = False

    def error(self, message):
        return GuardError(f"{self.path.name}: {message}")

    def read(self):
        with self.process.stdout as stream:
            try:
                while line := stream.readline(MAX_FRAME + 1):
                    if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                        break
                    self.messages.put(line, timeout=2)
            except (OSError, ValueError, Full):
                pass
            finally:
                try:
                    self.messages.put(None, timeout=2)
                except Full:
                    pass

    def request(self, command, timeout=8, **fields):
        payload = {"command": command, **fields}
        try:
            self.process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            self.process.stdin.flush()
        except OSError as exc:
            raise self.error(f"{tr('Could not send {command}.', command=command)} {exc}") from exc
        deadline = time.monotonic() + timeout
        while True:
            try:
                raw = self.messages.get(timeout=max(0, deadline - time.monotonic()))
                if raw is None:
                    raise self.error(tr("No valid reply to {command}. The plugin may have exited or returned invalid data.",
                                        command=command))
                reply = json.loads(raw.decode("utf-8"))
            except Empty as exc:
                raise self.error(tr("Timed out waiting for {command}.", command=command)) from exc
            except (ValueError, RecursionError) as exc:
                raise self.error(tr("The {command} reply is invalid JSON.", command=command)) from exc
            if not isinstance(reply, dict):
                raise self.error(tr("The {command} reply must be a JSON object.", command=command))
            if reply.get("event") == "status":
                title, detail = reply.get("title"), reply.get("detail")
                if not isinstance(title, str) or not isinstance(detail, str):
                    raise self.error(tr("The status event must provide title and detail."))
                if self.update_status is not None:
                    self.update_status(title[:120], detail[:500])
                if time.monotonic() >= deadline:
                    raise self.error(tr("Timed out waiting for {command}.", command=command))
                continue
            if reply.get("command") != command:
                raise self.error(tr("Received an out-of-order reply to {command}.", command=command))
            if reply.get("ok") is not True:
                raise self.error(str(reply.get("error") or tr("{command} failed.", command=command))[:1000])
            return reply

    def start(self, enabled):
        ready = self.request("start", timeout=140, enabled=enabled)
        self.active, self.status = ready.get("active"), ready.get("status", "")
        if type(self.active) is not bool or not isinstance(self.status, str) or (self.active and not enabled):
            raise self.error(tr("The start reply has invalid active or status fields."))

    def require_alive(self):
        self.request("check")

    def restore(self):
        self.request("restore", timeout=70)

    def suspend(self):
        self.request("suspend", timeout=70)


@contextmanager
def open_plugin(path, update_status, *, argument="--guard-plugin-v1", exit_timeout=70):
    with subprocess.Popen(
        [str(path), argument], cwd=path.parent,
        env={**os.environ, LANGUAGE_ENV: current_language()},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW, bufsize=0,
    ) as process:
        plugin = PluginProcess(path, process, update_status)
        try:
            yield plugin
        finally:
            try:
                process.stdin.close()
                process.wait(timeout=exit_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=8)
                raise plugin.error(tr("The plugin did not exit in time and was terminated.")) from None
            finally:
                plugin.reader.join(1)


class DeviceProtection:
    def __init__(self, active):
        self.active = active

    @property
    def status(self):
        return "\n".join(plugin.status for plugin in self.active if plugin.status)

    def require_alive(self):
        for plugin in self.active:
            plugin.require_alive()

    def restore(self):
        for plugin in reversed(self.active):
            plugin.restore()

    def suspend(self):
        for plugin in self.active:
            plugin.suspend()


class Plugins:
    def __init__(self, executable, state_directory):
        self.directory = executable.with_suffix(".plugins")
        self.recovery = state_directory / "plugin-recovery"

    def installed(self):
        plugins = {}
        for path in sorted(self.directory.glob(f"*{PLUGIN_SUFFIX}")):
            plugin_id = path.name.removesuffix(PLUGIN_SUFFIX)
            if PLUGIN_ID.fullmatch(plugin_id) and path.is_file():
                plugins[plugin_id] = path
        return plugins

    def pending(self):
        return sorted(path.stem for path in self.recovery.glob("*.pending") if PLUGIN_ID.fullmatch(path.stem))

    def status(self):
        return {"directory": str(self.directory), "installed": list(self.installed()), "pending": self.pending()}

    @contextmanager
    def session(self, plugin_id, path, enabled, update_status):
        with open_plugin(path, update_status) as plugin:
            hello = plugin.request("hello", timeout=20)
            if hello.get("protocol") != PROTOCOL:
                raise plugin.error(tr("The plugin must support protocol version 1."))
            self.recovery.mkdir(parents=True, exist_ok=True)
            marker = self.recovery / f"{plugin_id}.pending"
            with marker.open("ab") as stream:
                os.fsync(stream.fileno())
            plugin.start(enabled)
            try:
                yield plugin
            finally:
                plugin.request("stop", timeout=70)
                marker.unlink()
                if not any(self.recovery.iterdir()):
                    self.recovery.rmdir()

    @contextmanager
    def protect(self, enable=False, disabled=(), update_status=None):
        installed = self.installed()
        missing = set(self.pending()) - installed.keys()
        if missing:
            raise GuardError(tr("Plugins still need recovery: {plugins}. Reinstall them in {directory} and run --restore-only.",
                                plugins=", ".join(sorted(missing)), directory=self.directory))
        with ExitStack() as stack:
            active = []
            for plugin_id, path in installed.items():
                with ExitStack() as current:
                    plugin = current.enter_context(self.session(plugin_id, path, enable and plugin_id not in disabled,
                                                                update_status))
                    if plugin.active:
                        stack.enter_context(current.pop_all())
                        active.append(plugin)
            yield DeviceProtection(active) if active else None
