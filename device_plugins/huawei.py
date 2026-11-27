"""Temporarily turn off the Huawei lid-to-S4 rule through a scoped UAC helper."""

import base64
import ctypes
import os
import time
import uuid
from contextlib import ExitStack, contextmanager
from ctypes import wintypes as w
from pathlib import Path
from xml.etree import ElementTree

from i18n import title, tr
import windows_power as power
from .huawei_helper import SCRIPT

ACTIVE_STATUS = "Huawei automatic lid-to-S4 is temporarily paused."

RULE = 'Modules/Module[@ModuleName="AnomalyMs"]/Params[@MachineType="default"]/Param[@Key="AutoTurnS4"]'
ROOT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Huawei" / "Hiview"
CONFIG = ROOT / "updateDir" / "Dubai.xml"
RECOVERY = ROOT / ".CodexS4Guard-AutoTurnS4"
COMMANDS = ("disable", "restore", "stop", "ack")


class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("size", w.DWORD), ("flags", w.ULONG), ("parent", w.HWND),
        ("verb", w.LPCWSTR), ("file", w.LPCWSTR), ("parameters", w.LPCWSTR),
        ("directory", w.LPCWSTR), ("show", ctypes.c_int), ("instance", w.HINSTANCE),
        ("idl", ctypes.c_void_p), ("cls", w.LPCWSTR), ("key", w.HANDLE),
        ("hotkey", w.DWORD), ("icon", w.HANDLE), ("process", w.HANDLE),
    ]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]


def applies():
    if not CONFIG.is_file():
        return False
    try:
        nodes = ElementTree.parse(CONFIG).findall(RULE)
    except ElementTree.ParseError as exc:
        raise power.GuardError(f"{tr('Could not read the Huawei AutoTurnS4 setting.')} {exc}") from exc
    if len(nodes) != 1 or nodes[0].get("Value") not in {"0", "1"}:
        raise power.GuardError(tr("The Huawei AutoTurnS4 setting was not recognized."))
    return nodes[0].get("Value") == "1"


def elevate(prefix, parent_started):
    script = f"$leaseName = '{prefix}'; $ownerId = {os.getpid()}; $ownerStarted = {parent_started};\n"
    script += SCRIPT
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    system = ctypes.create_unicode_buffer(32768)
    system_directory = power.api(power.kernel, "GetSystemDirectoryW", w.UINT, w.LPWSTR, w.UINT)
    power.checked(system_directory(system, len(system)))
    info = ShellExecuteInfo()
    info.size, info.flags, info.verb = ctypes.sizeof(info), 0x540, "runas"
    info.file = str(Path(system.value) / r"WindowsPowerShell\v1.0\powershell.exe")
    info.parameters = f"-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    info.directory = system.value
    launch = power.api(ctypes.WinDLL("shell32", use_last_error=True), "ShellExecuteExW",
                       w.BOOL, ctypes.POINTER(ShellExecuteInfo))
    if not launch(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:
            raise power.GuardError(tr("Pausing Huawei automatic hibernation requires Windows administrator approval."))
        raise ctypes.WinError(error)
    return power.checked(info.process)


@contextmanager
def events_for(prefix):
    # The launched administrator may be a different account in the same session.
    descriptor = ctypes.c_void_p()
    convert = power.api(ctypes.WinDLL("advapi32", use_last_error=True),
                       "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL,
                       w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    free = power.api(power.kernel, "LocalFree", w.HANDLE, w.HANDLE)
    power.checked(convert("D:(A;;GA;;;OW)(A;;GA;;;SY)(A;;GA;;;BA)", 1, ctypes.byref(descriptor), None))
    security = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
    try:
        with ExitStack() as cleanup:
            handles = {}
            for command in COMMANDS:
                handle = power.checked(power.event(ctypes.byref(security), False, False, prefix + "." + command))
                handles[command] = handle
                cleanup.callback(power.close, handle)
            yield handles
    finally:
        free(descriptor)


class HuaweiSession:
    def __init__(self, process, events):
        self.process, self.events = process, events

    def require_alive(self):
        if power.waited(self.process, 0) != power.TIMEOUT:
            exit_code = w.DWORD()
            get_exit = power.api(power.kernel, "GetExitCodeProcess", w.BOOL, w.HANDLE, ctypes.POINTER(w.DWORD))
            power.checked(get_exit(self.process, ctypes.byref(exit_code)))
            if exit_code.value == 12:
                raise power.GuardError(tr("Huawei settings still need recovery. Recovery file: {path}", path=RECOVERY))
            raise power.GuardError(tr("The Huawei helper exited. Check that Huawei PC Manager and Hiview are running."))

    def acknowledged(self):
        end = time.monotonic() + 60
        while time.monotonic() < end:
            self.require_alive()
            if power.waited(self.events["ack"], 200) != power.TIMEOUT:
                return
        raise power.GuardError(tr("Timed out waiting for the Huawei change to take effect."))

    def request(self, command):
        self.require_alive()
        power.checked(power.signal(self.events[command]))
        self.acknowledged()

    def suspend(self):
        self.request("disable")

    def restore(self):
        self.request("restore")


@contextmanager
def session(block=False, on_authorize=None):
    if not RECOVERY.is_file() and (not block or not applies()):
        yield None
        return
    prefix = "Local\\CodexS4Guard.Huawei." + uuid.uuid4().hex
    started = power.process_started(os.getpid())
    if not started:
        raise power.GuardError(tr("Could not identify the process that owns the Huawei helper."))
    with events_for(prefix) as events:
        if on_authorize is not None:
            detail = tr("Approve the Windows prompt to temporarily pause Huawei automatic hibernation on lid close.") if block else tr("Approve the Windows prompt to restore the Huawei lid settings.")
            on_authorize(title("Waiting for administrator approval"), detail)
        process = elevate(prefix, round(started * 1000))
        try:
            guard = HuaweiSession(process, events)
            guard.acknowledged()
            if block:
                guard.suspend()
            yield guard if block else None
        finally:
            power.signal(events["stop"])
            outcome = power.waited(process, 65000)
            power.close(process)
            if outcome == power.TIMEOUT:
                raise power.GuardError(tr("The Huawei helper is still restoring settings."))
            if RECOVERY.is_file():
                raise power.GuardError(tr("Huawei settings still need recovery. Recovery file: {path}", path=RECOVERY))
