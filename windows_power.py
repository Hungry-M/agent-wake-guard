"""Windows session control, keep-awake requests, and reversible lid policy."""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from ctypes import wintypes as w
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from i18n import APP_NAME, title, tr


class GuardError(RuntimeError):
    pass


class ShutdownLocked(GuardError):
    pass


def api(dll, name, result, *types):
    call = getattr(dll, name)
    call.restype, call.argtypes = result, types
    return call


class LastInput(ctypes.Structure):
    _fields_ = [("size", w.UINT), ("tick", w.DWORD)]


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
        ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
        ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
        ("exe", w.WCHAR * 260),
    ]


class NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("size", w.DWORD), ("window", w.HWND), ("id", w.UINT), ("flags", w.UINT),
        ("callback", w.UINT), ("icon", w.HANDLE), ("tip", w.WCHAR * 128),
        ("state", w.DWORD), ("state_mask", w.DWORD), ("info", w.WCHAR * 256),
        ("version", w.UINT), ("info_title", w.WCHAR * 64), ("info_flags", w.DWORD),
        ("guid", ctypes.c_ubyte * 16), ("balloon_icon", w.HANDLE),
    ]


class PowerCapabilities(ctypes.Structure):
    # SYSTEM_POWER_CAPABILITIES layout on Windows 10/11.
    _fields_ = [
        ("buttons_and_lid", ctypes.c_ubyte * 3), ("sleep_states", ctypes.c_ubyte * 3),
        ("hibernate_through_wake_alarm", ctypes.c_ubyte * 14), ("aoac", ctypes.c_ubyte),
        ("disk_through_battery_flags", ctypes.c_ubyte * 11), ("battery_scale", w.DWORD * 6),
        ("wake_states", ctypes.c_int * 5),
    ]


class Luid(ctypes.Structure):
    _fields_ = [("low", w.DWORD), ("high", w.LONG)]


class TokenPrivilege(ctypes.Structure):
    _fields_ = [("count", w.DWORD), ("luid", Luid), ("attributes", w.DWORD)]


if os.name == "nt":
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    power = ctypes.WinDLL("powrprof", use_last_error=True)
    mutex = api(kernel, "CreateMutexW", w.HANDLE, ctypes.c_void_p, w.BOOL, w.LPCWSTR)
    event = api(kernel, "CreateEventW", w.HANDLE, ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR)
    wait = api(kernel, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
    release = api(kernel, "ReleaseMutex", w.BOOL, w.HANDLE)
    close = api(kernel, "CloseHandle", w.BOOL, w.HANDLE)
    signal = api(kernel, "SetEvent", w.BOOL, w.HANDLE)
    reset = api(kernel, "ResetEvent", w.BOOL, w.HANDLE)
    set_execution = api(kernel, "SetThreadExecutionState", w.DWORD, w.DWORD)
    get_ticks = api(kernel, "GetTickCount64", ctypes.c_uint64)
    last_input = api(user, "GetLastInputInfo", w.BOOL, ctypes.POINTER(LastInput))
    can_hibernate = api(power, "IsPwrHibernateAllowed", ctypes.c_ubyte)
    get_power_capabilities = api(power, "GetPwrCapabilities", ctypes.c_ubyte, ctypes.POINTER(PowerCapabilities))
    set_suspend_state = api(power, "SetSuspendState", ctypes.c_ubyte, *([ctypes.c_ubyte] * 3))
    snapshot_processes = api(kernel, "CreateToolhelp32Snapshot", w.HANDLE, w.DWORD, w.DWORD)
    first_process = api(kernel, "Process32FirstW", w.BOOL, w.HANDLE, ctypes.POINTER(ProcessEntry))
    next_process = api(kernel, "Process32NextW", w.BOOL, w.HANDLE, ctypes.POINTER(ProcessEntry))
    open_process = api(kernel, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    process_times = api(kernel, "GetProcessTimes", w.BOOL, w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4))
    Handler = ctypes.WINFUNCTYPE(w.BOOL, w.DWORD)
    console_handler = api(kernel, "SetConsoleCtrlHandler", w.BOOL, Handler, w.BOOL)
    WindowProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WindowClass(ctypes.Structure):
        _fields_ = [
            ("style", w.UINT), ("proc", WindowProc), ("extra_class", ctypes.c_int),
            ("extra_window", ctypes.c_int), ("instance", w.HINSTANCE), ("icon", w.HANDLE),
            ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR),
            ("name", w.LPCWSTR),
        ]

    module_handle = api(kernel, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
    register_class = api(user, "RegisterClassW", w.ATOM, ctypes.POINTER(WindowClass))
    unregister_class = api(user, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)
    create_window = api(
        user, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p,
    )
    destroy_window = api(user, "DestroyWindow", w.BOOL, w.HWND)
    default_window = api(user, "DefWindowProcW", ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    get_message = api(user, "GetMessageW", ctypes.c_int, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
    translate_message = api(user, "TranslateMessage", w.BOOL, ctypes.POINTER(w.MSG))
    dispatch_message = api(user, "DispatchMessageW", ctypes.c_ssize_t, ctypes.POINTER(w.MSG))
    post_message = api(user, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    post_quit = api(user, "PostQuitMessage", None, ctypes.c_int)
    show_window = api(user, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)
    set_window_text = api(user, "SetWindowTextW", w.BOOL, w.HWND, w.LPCWSTR)
    send_message = api(user, "SendMessageW", ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    load_image = api(user, "LoadImageW", w.HANDLE, w.HINSTANCE, w.LPCWSTR, w.UINT,
                     ctypes.c_int, ctypes.c_int, w.UINT)
    destroy_icon = api(user, "DestroyIcon", w.BOOL, w.HANDLE)
    load_cursor = api(user, "LoadCursorW", w.HANDLE, w.HINSTANCE, ctypes.c_void_p)
    stock_object = api(ctypes.WinDLL("gdi32", use_last_error=True), "GetStockObject", w.HANDLE, ctypes.c_int)
    message_box = api(user, "MessageBoxW", ctypes.c_int, w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT)
    register_message = api(user, "RegisterWindowMessageW", w.UINT, w.LPCWSTR)
    get_cursor = api(user, "GetCursorPos", w.BOOL, ctypes.POINTER(w.POINT))
    foreground = api(user, "SetForegroundWindow", w.BOOL, w.HWND)
    popup_menu = api(user, "CreatePopupMenu", w.HMENU)
    append_menu = api(user, "AppendMenuW", w.BOOL, w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR)
    track_menu = api(user, "TrackPopupMenu", w.UINT, w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int,
                     ctypes.c_int, w.HWND, ctypes.c_void_p)
    destroy_menu = api(user, "DestroyMenu", w.BOOL, w.HMENU)
    end_menu = api(user, "EndMenu", w.BOOL)
    notify_icon = api(ctypes.WinDLL("shell32", use_last_error=True), "Shell_NotifyIconW",
                      w.BOOL, w.DWORD, ctypes.POINTER(NotifyIconData))
    register_power = api(user, "RegisterPowerSettingNotification", w.HANDLE, w.HWND, ctypes.c_void_p, w.DWORD)
    unregister_power = api(user, "UnregisterPowerSettingNotification", w.BOOL, w.HANDLE)
    lock_workstation = api(user, "LockWorkStation", w.BOOL)


TIMEOUT = 0x102
OWNED = {0, 0x80}
CONTINUOUS = 0x80000000
LID = "5ca83367-6e45-459f-a27b-476b1d01c936"
LID_EVENT = uuid.UUID("ba3e0f4d-b817-4094-a2d1-d56379e6a0f3").bytes_le
WM_CLOSE, WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE = 0x10, 0x218, 0x8013
GUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)


def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def waited(handle, milliseconds):
    result = wait(handle, milliseconds)
    if result not in OWNED | {TIMEOUT}:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


class Control:
    """A second launch broadcasts cancellation and waits for the only owner."""

    def __init__(self, suffix=""):
        name = "Local\\CodexS4Guard.v1" + suffix
        self.handles = []
        self.owns = False
        try:
            for handle in (
                lambda: mutex(None, False, name + ".owner"),
                lambda: mutex(None, False, name + ".gate"),
                lambda: event(None, True, False, name + ".cancel"),
                lambda: mutex(None, False, name + ".startup"),
            ):
                self.handles.append(checked(handle()))
        except BaseException:
            self.__exit__()
            raise
        self.owner, self.gate, self.cancel, self.startup = self.handles

    @contextmanager
    def locked(self):
        if waited(self.gate, 30000) == TIMEOUT:
            raise GuardError("The existing guard did not release its control lock.")
        try:
            yield
        finally:
            release(self.gate)

    @contextmanager
    def startup_locked(self):
        if waited(self.startup, 30000) == TIMEOUT:
            raise GuardError("Another guard is still completing startup or uninstall.")
        try:
            yield
        finally:
            release(self.startup)

    def start(self) -> bool:
        with self.startup_locked(), self.locked():
            if waited(self.owner, 0) in OWNED:
                self.owns = True
                checked(reset(self.cancel))
                return True
            checked(signal(self.cancel))
            return False

    def stop_and_reserve(self):
        """Cancel the owner and reserve its slot until maintenance is complete."""
        with self.locked():
            self.request_stop()
        if waited(self.owner, 30000) == TIMEOUT:
            raise GuardError("The existing guard did not exit. Check its window and try again.")
        self.owns = True

    def request_stop(self):
        checked(signal(self.cancel))

    def wait_for_owner(self, milliseconds=30000) -> bool:
        if waited(self.owner, milliseconds) in OWNED:
            checked(release(self.owner))
            return True
        return False

    def cancelled(self, seconds=0) -> bool:
        return waited(self.cancel, int(seconds * 1000)) == 0

    @contextmanager
    def on_close(self):
        done = checked(event(None, True, False, None))

        @Handler
        def on_signal(code):
            signal(self.cancel)
            if code in {2, 5, 6}:
                wait(done, 3800)
            return True

        registered = console_handler(on_signal, True)
        try:
            yield
        finally:
            signal(done)
            if registered:
                console_handler(on_signal, False)
            close(done)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.owns:
            release(self.owner)
        for handle in reversed(self.handles):
            close(handle)
        self.handles.clear()


@contextmanager
def awake():
    checked(set_execution(CONTINUOUS | 1))
    try:
        yield
    finally:
        set_execution(CONTINUOUS)


def lid_state(address):
    """Return a matching Windows lid state (0 closed, 1 open), or None."""
    if not address or ctypes.string_at(address, 16) != LID_EVENT:
        return None
    if ctypes.c_uint32.from_address(address + 16).value != 4:
        return None
    value = ctypes.c_uint32.from_address(address + 20).value
    return value if value in {0, 1} else None


class LidLock:
    def __init__(self, cancelled, request_lock):
        self.cancelled, self.request_lock = cancelled, request_lock
        self.previous = None
        self.initial = threading.Event()

    def __call__(self, value):
        previous, self.previous = self.previous, value
        self.initial.set()
        # The first notification reports the current state. Lock on an actual close.
        if previous == 1 and value == 0 and not self.cancelled():
            self.request_lock()


@contextmanager
def message_window(name, on_message, setup, *, title=None, style=0, ex_style=0, size=(0, 0)):
    ready = threading.Event()
    errors = []
    handles = []
    stopping = threading.Event()

    def receive():
        class_name = f"CodexS4Guard.{name}.{os.getpid()}.{threading.get_ident()}"
        instance = module_handle(None)

        @WindowProc
        def process(hwnd, message, wparam, lparam):
            if not stopping.is_set():
                try:
                    handled = on_message(hwnd, message, wparam, lparam)
                    if handled is not None:
                        return handled
                except (OSError, GuardError) as exc:
                    print(f"{name}: {exc}", file=sys.stderr, flush=True)
            if message == WM_CLOSE:
                if style:
                    end_menu()
                post_quit(0)
                return 0
            return default_window(hwnd, message, wparam, lparam)

        window_type = WindowClass()
        window_type.proc, window_type.instance, window_type.name = process, instance, class_name
        if style:
            window_type.background = 16  # COLOR_BTNFACE + 1
            window_type.cursor = load_cursor(None, 32512)  # IDC_ARROW
        registered = hwnd = None
        try:
            registered = checked(register_class(ctypes.byref(window_type)))
            position = -2147483648 if style else 0  # CW_USEDEFAULT
            hwnd = checked(create_window(ex_style, class_name, title, style, position, 0,
                                         *size, None, None, instance, None))
            with setup(hwnd):
                handles.append(hwnd)
                ready.set()
                message = w.MSG()
                while not stopping.is_set():
                    result = get_message(ctypes.byref(message), None, 0, 0)
                    if result == 0:
                        break
                    if result == -1:
                        raise ctypes.WinError(ctypes.get_last_error())
                    translate_message(ctypes.byref(message))
                    dispatch_message(ctypes.byref(message))
        except Exception as exc:
            errors.append(exc)
        finally:
            if hwnd:
                destroy_window(hwnd)
            if registered:
                unregister_class(class_name, instance)
            ready.set()

    worker = threading.Thread(target=receive, name=f"window-{name}", daemon=True)
    worker.start()
    try:
        if not ready.wait(4):
            raise GuardError(f"Windows did not initialize {name}.")
        if errors:
            raise GuardError(f"Windows could not open {name}: {errors[0]}")
        yield handles[0]
    finally:
        stopping.set()
        if handles and worker.is_alive():
            post_message(handles[0], WM_CLOSE, 0, 0)
        worker.join(3)


@contextmanager
def lid_notifications(on_change):
    def dispatch(_hwnd, message, wparam, lparam):
        if message == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE:
            value = lid_state(lparam)
            if value is not None:
                on_change(value)
            return 1
        return None

    @contextmanager
    def subscribe(hwnd):
        identity = (ctypes.c_ubyte * 16).from_buffer_copy(LID_EVENT)
        subscription = checked(register_power(hwnd, ctypes.byref(identity), 0))
        try:
            yield
        finally:
            unregister_power(subscription)

    with message_window("lid notifications", dispatch, subscribe) as hwnd:
        yield hwnd


@contextmanager
def guard_status(mode, request_stop, open_settings, remember_mode, actions, read_action, save_action):
    """Show a single entry point for the running guard."""
    status = (title("Starting"), tr("Starting task monitoring and device plugins."))
    stop_button, settings_button, show_status, switch_button, action_control = 101, 102, 103, 104, 105
    action_values = tuple(actions)
    status_update = 0x8001  # WM_APP + 1
    tray_callback = 0x8002
    taskbar_created = checked(register_message("TaskbarCreated"))
    tray = NotifyIconData()
    tray.size, tray.id, tray.callback = ctypes.sizeof(tray), 1, tray_callback
    # NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
    tray.flags = 0x87
    label = switch = action_list = shown_action = None
    tray_added = preview = False

    def sync_action(force=False):
        nonlocal shown_action
        action = read_action()
        if force or shown_action != action:
            send_message(action_list, 0x14E, action_values.index(action), 0)  # CB_SETCURSEL
            shown_action = action

    def add_tray_icon():
        nonlocal tray_added
        tray.tip = status[0]
        if notify_icon(0, ctypes.byref(tray)):  # NIM_ADD
            tray.version = 4  # NOTIFYICON_VERSION_4
            notify_icon(4, ctypes.byref(tray))  # NIM_SETVERSION
            tray_added = True
        else:
            # A DPI change can send TaskbarCreated even when the icon still exists.
            tray_added = bool(notify_icon(1, ctypes.byref(tray)))  # NIM_MODIFY
        return tray_added

    def display_window(hwnd, temporary=False):
        nonlocal tray_added, preview
        if tray_added and not notify_icon(2, ctypes.byref(tray)):  # NIM_DELETE
            if notify_icon(1, ctypes.byref(tray)):
                raise GuardError(tr("Windows could not remove the tray icon. Try again."))
        tray_added, preview = False, temporary
        set_window_text(switch, tr("Keep window") if preview else tr("Switch to tray"))
        show_window(hwnd, 4)  # SW_SHOWNOACTIVATE

    def display_tray(hwnd):
        nonlocal preview
        show_window(hwnd, 0)  # SW_HIDE
        if add_tray_icon():
            preview = False
            return
        display_window(hwnd)
        raise GuardError(tr("The tray is unavailable. The status window is still open."))

    @contextmanager
    def setup(hwnd):
        nonlocal label, switch, action_list
        icons = []
        try:
            path = str(Path(__file__).resolve().with_name("guard.ico"))
            for number, size in enumerate((16, 32)):
                icon = checked(load_image(None, path, 1, size, size, 0x10))  # IMAGE_ICON, LR_LOADFROMFILE
                icons.append(icon)
                send_message(hwnd, 0x80, number, icon)  # WM_SETICON
            style = 0x50000000  # WS_CHILD | WS_VISIBLE
            instance = module_handle(None)
            label = checked(create_window(0, "STATIC", status[1], style | 0x80, 16, 12, 404, 64,
                                          hwnd, None, instance, None))
            font = checked(stock_object(17))  # DEFAULT_GUI_FONT
            send_message(label, 0x30, font, 1)  # WM_SETFONT
            caption = tr("After tasks finish") if save_action else tr("Set at launch")
            action_label = checked(create_window(0, "STATIC", caption, style, 16, 86, 138, 22,
                                                 hwnd, None, instance, None))
            send_message(action_label, 0x30, font, 1)
            combo_style = style | 0x10003  # WS_TABSTOP | CBS_DROPDOWNLIST
            if save_action is None:
                combo_style |= 0x08000000  # WS_DISABLED
            action_list = checked(create_window(0, "COMBOBOX", None, combo_style, 160, 82, 260, 140,
                                                hwnd, action_control, instance, None))
            send_message(action_list, 0x30, font, 1)
            for caption in actions.values():
                value = ctypes.create_unicode_buffer(caption)
                if send_message(action_list, 0x143, 0, ctypes.addressof(value)) < 0:  # CB_ADDSTRING
                    raise GuardError(tr("Could not load the power actions."))
            sync_action()
            for identifier, caption, x in ((stop_button, tr("Stop"), 16), (settings_button, tr("Open config"), 154),
                                           (switch_button, tr("Switch to tray"), 292)):
                button = checked(create_window(0, "BUTTON", caption, style | 0x10000, x, 120, 128, 30,
                                               hwnd, identifier, instance, None))
                send_message(button, 0x30, font, 1)
                if identifier == switch_button:
                    switch = button
            tray.window, tray.icon = hwnd, icons[0]
            if mode == "window" or not add_tray_icon():
                display_window(hwnd)
            yield
        finally:
            if tray_added:
                notify_icon(2, ctypes.byref(tray))  # NIM_DELETE
            for number, icon in enumerate(icons):
                send_message(hwnd, 0x80, number, 0)
                destroy_icon(icon)

    def select(hwnd, command):
        nonlocal mode
        if command == stop_button:
            request_stop()
            set_window_text(hwnd, title("Stopping"))
        elif command == settings_button:
            open_settings()
        elif command == show_status and tray_added:
            display_window(hwnd, temporary=True)
            foreground(hwnd)
        elif command == switch_button:
            if tray_added or preview:
                display_window(hwnd)
                foreground(hwnd)
                mode = "window"
            else:
                display_tray(hwnd)
                mode = "tray"
            remember_mode(mode)

    def show_menu(hwnd):
        menu = checked(popup_menu())
        try:
            for identifier, caption in ((show_status, tr("Show status")), (switch_button, tr("Switch to window")),
                                        (settings_button, tr("Open config")),
                                        (stop_button, tr("Stop"))):
                checked(append_menu(menu, 0, identifier, caption))
            point = w.POINT()
            checked(get_cursor(ctypes.byref(point)))
            foreground(hwnd)
            choice = track_menu(menu, 0x182, point.x, point.y, 0, hwnd, None)
            post_message(hwnd, 0, 0, 0)  # Return focus after the menu closes.
            select(hwnd, choice)
        finally:
            destroy_menu(menu)

    def dispatch(hwnd, message, wparam, lparam):
        try:
            if message == WM_CLOSE:
                if preview:
                    display_tray(hwnd)
                else:
                    select(hwnd, stop_button)
                return 0
            if message == 0x111:  # WM_COMMAND
                command = wparam & 0xFFFF
                if command == action_control:
                    if save_action and lparam == action_list and (wparam >> 16) == 9:  # CBN_SELENDOK
                        index = send_message(action_list, 0x147, 0, 0)  # CB_GETCURSEL
                        if 0 <= index < len(action_values):
                            try:
                                save_action(action_values[index])
                            except (OSError, GuardError):
                                sync_action(force=True)
                                raise
                else:
                    select(hwnd, command)
                return 0
            if message == tray_callback:
                if tray_added:
                    event = lparam & 0xFFFF
                    if event in {0x7B, 0x205}:  # WM_CONTEXTMENU or WM_RBUTTONUP
                        show_menu(hwnd)
                    elif event in {0x400, 0x401, 0x202}:  # NIN_SELECT, NIN_KEYSELECT, WM_LBUTTONUP
                        select(hwnd, show_status)
                return 0
            if message == taskbar_created and tray_added:
                if not add_tray_icon():
                    display_window(hwnd)
                return 0
            if message == status_update:
                title, detail = status
                set_window_text(hwnd, title)
                set_window_text(label, detail)
                sync_action()
                if tray_added:
                    tray.tip = title
                    notify_icon(1, ctypes.byref(tray))  # NIM_MODIFY
                return 0
        except (OSError, GuardError) as exc:
            message_box(hwnd, str(exc), APP_NAME, 0x10)  # MB_ICONERROR
            return 0
        return None

    # WS_CAPTION | WS_SYSMENU and WS_EX_TOOLWINDOW.
    with message_window("guard status", dispatch, setup, title=status[0], style=0xC80000,
                        ex_style=0x80, size=(444, 195)) as hwnd:
        def update(title, detail):
            nonlocal status
            status = (title, detail)
            post_message(hwnd, status_update, 0, 0)

        yield update


@contextmanager
def auto_lock_on_lid_close(cancelled):
    def request_lock():
        checked(lock_workstation())
        print(tr("Lid closed. Windows was asked to lock the session."), flush=True)

    listener = LidLock(cancelled, request_lock)
    with lid_notifications(listener):
        if not listener.initial.wait(3):
            raise GuardError(tr("Windows did not report the lid state. Automatic locking is unavailable."))
        yield


def boot_time():
    return time.time() - get_ticks() / 1000


def input_idle():
    info = LastInput(ctypes.sizeof(LastInput), 0)
    checked(last_input(ctypes.byref(info)))
    age = (get_ticks() - info.tick) & 0xffffffff
    return age / 1000 if age < 0x80000000 else 0.0


FAMILIES = {
    "Codex": {"chatgpt.exe"},
    "DeepSeek": {"deepseek harness.exe", "deepseek harness desktop.exe",
                 "deepseekharness.exe", "dsh-desktop.exe"},
}


def process_started(pid):
    handle = open_process(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [w.FILETIME() for _ in range(4)]
        if not process_times(handle, *(ctypes.byref(value) for value in times)):
            return None
        birth = times[0]
        return ((birth.dwHighDateTime << 32) | birth.dwLowDateTime) / 10_000_000 - 11644473600
    finally:
        close(handle)


def applications(boot):
    result = {name: None for name in FAMILIES}
    snap = snapshot_processes(2, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        item = ProcessEntry()
        item.size = ctypes.sizeof(item)
        checked(first_process(snap, ctypes.byref(item)))
        while True:
            name = item.exe.casefold()
            for family, names in FAMILIES.items():
                if name in names:
                    birth = process_started(item.pid) or boot
                    if result[family] is None or birth < result[family]:
                        result[family] = birth
            if not next_process(snap, ctypes.byref(item)):
                break
    finally:
        close(snap)
    return result


def system_exe(name):
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / name


def portable_exe():
    try:
        __compiled__
    except NameError:
        return None
    return Path(sys.argv[0]).absolute()


def desktop_notice(message, error=False):
    message_box(None, str(message), APP_NAME, 0x10 if error else 0x40)


def powercfg(*args):
    proc = subprocess.run(
        [str(system_exe("powercfg.exe")), *args], capture_output=True,
        stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=6,
    )
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).decode("oem", errors="replace").strip()
        raise GuardError(f"powercfg {args[0]} failed ({proc.returncode}): {detail}")
    return proc.stdout.decode("oem", errors="replace")


class LidPlan:
    """The journal is written before creating a unique temporary power plan."""

    def __init__(self, journal: Path, command=powercfg, lease=None):
        self.journal = journal
        self.command = command
        self.lease = lease

    def active(self):
        match = GUID.search(self.command("/getactivescheme"))
        if not match:
            raise GuardError("Could not identify the current Windows power plan.")
        return match[0].lower()

    def begin(self):
        self.restore()
        original = self.active()
        # Every run owns a fresh scheme GUID, even when another scheme has identical settings.
        temporary = str(uuid.uuid4())
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        staged = self.journal.with_suffix(".tmp")
        with staged.open("w", encoding="ascii") as stream:
            json.dump({"original": original, "temporary": temporary, "lease": self.lease}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        staged.replace(self.journal)
        self.command("/duplicatescheme", original, temporary)
        self.command("/changename", temporary, f"{APP_NAME} (temporary)")
        for option in ("/setacvalueindex", "/setdcvalueindex"):
            self.command(option, temporary, "SUB_BUTTONS", LID, "0")
        if self.active() != original:
            raise GuardError("Windows changed the active plan during setup. Retry with the desired plan.")
        self.command("/setactive", temporary)
        if self.active() != temporary:
            raise GuardError("Windows did not activate the temporary lid protection plan.")
        indexes = re.findall(r"0x[0-9a-f]{8}\b", self.command("/qh", temporary, "SUB_BUTTONS", LID), re.I)
        if [value.lower() for value in indexes[-2:]] != ["0x00000000", "0x00000000"]:
            raise GuardError("Windows did not confirm the temporary AC and battery lid settings.")

    def restore(self, only_lease=None) -> bool:
        if not self.journal.is_file():
            return False
        record = json.loads(self.journal.read_bytes())
        if only_lease is not None and record.get("lease") != only_lease:
            return False
        original, temporary = record["original"], record["temporary"]
        if not GUID.fullmatch(original) or not GUID.fullmatch(temporary) or original == temporary:
            raise GuardError(f"Invalid recovery journal: {self.journal}")
        if self.active() == temporary:
            self.command("/setactive", original)
            if self.active() != original:
                raise GuardError(f"Windows could not restore the original plan. Recovery: {self.journal}")
        if temporary.casefold() in {key.lower() for key in GUID.findall(self.command("/list"))}:
            self.command("/delete", temporary)
        self.journal.unlink()
        return True


@contextmanager
def stopped(journal):
    with Control() as normal, Control(".preview") as preview:
        with normal.startup_locked(), preview.startup_locked():
            normal.stop_and_reserve()
            preview.stop_and_reserve()
            with normal.locked():
                LidPlan(journal).restore()
                journal.with_suffix(".tmp").unlink(missing_ok=True)
                if journal.parent.is_dir() and not any(journal.parent.iterdir()):
                    journal.parent.rmdir()
            print(tr("The Guard stopped and restored the original power plan."), flush=True)
            yield


def uninstall(journal, restore_devices=nullcontext):
    with stopped(journal), restore_devices():
        config = journal.with_name("config.ini")
        config.unlink(missing_ok=True)
        for name in ("run.log", "run.log.1"):
            journal.with_name(name).unlink(missing_ok=True)
        if journal.parent.is_dir() and not any(journal.parent.iterdir()):
            journal.parent.rmdir()
        executable = portable_exe()
        if executable:
            print("Removing the portable executable after it exits.", flush=True)
            delete_after_exit(executable)


def delete_after_exit(executable):
    # Windows holds the executable open until the one-file bootloader exits.
    stat = executable.stat()
    env = {
        **os.environ,
        "CODEX_S4_REMOVE_PATH": str(executable),
        "CODEX_S4_REMOVE_SIZE": str(stat.st_size),
        "CODEX_S4_REMOVE_WRITE_TIME": str(stat.st_mtime_ns // 100 + 116444736000000000),
    }
    command = r"""
$target = $env:CODEX_S4_REMOVE_PATH
for ($attempt = 0; $attempt -lt 80; $attempt++) {
    $file = [IO.FileInfo]::new($target)
    if (!$file.Exists) { exit 0 }
    if ($file.Length -ne [long]$env:CODEX_S4_REMOVE_SIZE -or
        $file.LastWriteTimeUtc.ToFileTimeUtc() -ne [long]$env:CODEX_S4_REMOVE_WRITE_TIME) {
        [Console]::Error.WriteLine('Executable changed during uninstall: ' + $target)
        exit 1
    }
    try {
        $file.Delete()
        [Console]::WriteLine('Portable executable removed.')
        exit 0
    } catch {
        Start-Sleep -Milliseconds 250
    }
}
[Console]::Error.WriteLine('Could not remove executable: ' + $target)
exit 1
"""
    subprocess.Popen(
        [str(system_exe(r"WindowsPowerShell\v1.0\powershell.exe")),
         "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        env=env, stdin=subprocess.DEVNULL, stdout=sys.stdout or subprocess.DEVNULL,
        stderr=sys.stderr or subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
    )


@contextmanager
def recover_on_exit(plan):
    """The watchdog holds no power request and uses the parent's stdin pipe as a lifetime token."""
    executable = portable_exe()
    command = [str(executable)] if executable else [sys.executable, "-I", "-B", str(Path(__file__).resolve())]
    try:
        # GUI launches can have unusable standard handles. Supply all three explicitly.
        recover = subprocess.Popen(
            [*command, "--rollback-worker", str(plan.journal), plan.lease],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise GuardError(f"{tr('Could not start the power-plan recovery process.')} {exc}") from exc
    ready = threading.Event()
    greeting = []

    def wait_for_recovery():
        try:
            greeting.append(recover.stdout.readline())
        finally:
            ready.set()

    reader = threading.Thread(target=wait_for_recovery, name="power-recovery-handshake", daemon=True)
    reader.start()
    try:
        if not ready.wait(12) or not greeting or greeting[0].strip() != b"ready":
            raise GuardError(tr("The power-plan recovery process did not initialize."))
        yield
    finally:
        recover.stdin.close()
        try:
            result = recover.wait(timeout=12)
        except subprocess.TimeoutExpired as exc:
            raise GuardError("Power-plan recovery is still running.") from exc
        finally:
            reader.join(0.5)
            if not reader.is_alive():
                recover.stdout.close()
        if result:
            raise GuardError("The power-plan recovery process could not finish.")


def rollback_worker(journal, lease):
    try:
        with Control() as control:
            print("ready", flush=True)
            sys.stdin.buffer.read()
            with control.locked():
                LidPlan(journal).restore(only_lease=lease)
    except (OSError, ValueError, KeyError, GuardError, subprocess.SubprocessError):
        return 1
    return 0


def sleep_mode():
    capabilities = PowerCapabilities()
    checked(get_power_capabilities(ctypes.byref(capabilities)))
    if capabilities.aoac:
        return "modern"
    if any(capabilities.sleep_states):
        return "traditional"
    raise GuardError("Windows sleep is unavailable. Check powercfg /a.")


def require_action_available(action):
    if action == "sleep":
        sleep_mode()
    elif action == "hibernate" and not can_hibernate():
        raise GuardError("Windows S4 hibernation is unavailable. Check powercfg /a.")


@contextmanager
def shutdown_privilege():
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    current_process = api(kernel, "GetCurrentProcess", w.HANDLE)
    open_token = api(security, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
    lookup = api(security, "LookupPrivilegeValueW", w.BOOL, w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(Luid))
    adjust = api(security, "AdjustTokenPrivileges", w.BOOL, w.HANDLE, w.BOOL,
                 ctypes.POINTER(TokenPrivilege), w.DWORD, ctypes.POINTER(TokenPrivilege), ctypes.POINTER(w.DWORD))
    token = w.HANDLE()
    checked(open_token(current_process(), 0x28, ctypes.byref(token)))  # TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY
    previous, length = TokenPrivilege(), w.DWORD()
    try:
        requested = TokenPrivilege(1, Luid(), 2)  # SE_PRIVILEGE_ENABLED
        checked(lookup(None, "SeShutdownPrivilege", ctypes.byref(requested.luid)))
        checked(adjust(token, False, ctypes.byref(requested), ctypes.sizeof(previous),
                       ctypes.byref(previous), ctypes.byref(length)))
        if error := ctypes.get_last_error():
            raise ctypes.WinError(error)
        yield
    finally:
        try:
            if previous.count:
                checked(adjust(token, False, ctypes.byref(previous), 0, None, None))
        finally:
            close(token)


def issue_power_action(action):
    if action == "sleep":
        mode = sleep_mode()
        if mode == "modern":
            # Windows enters Modern Standby after the display-off request and release of our keep-awake request.
            with message_window("modern sleep", lambda *_: None, lambda _: nullcontext()) as window:
                checked(set_execution(CONTINUOUS))
                send_message(window, 0x112, 0xF170, 2)  # WM_SYSCOMMAND, SC_MONITORPOWER, off
            return "modern_screen_off"
        with shutdown_privilege():
            checked(set_execution(CONTINUOUS))
            checked(set_suspend_state(False, False, False))
        return
    require_action_available(action)
    arguments = {"hibernate": ["/h"], "shutdown": ["/s", "/t", "0"]}[action]
    checked(set_execution(CONTINUOUS))
    process = subprocess.Popen(
        [str(system_exe("shutdown.exe")), *arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        result = process.wait(timeout=3 if action == "shutdown" else 0.6)
    except subprocess.TimeoutExpired:
        return
    if result:
        message = f"Windows rejected shutdown.exe {' '.join(arguments)} (exit {result})."
        if action == "shutdown" and result == 1271:  # ERROR_MACHINE_LOCKED
            checked(set_execution(CONTINUOUS | 1))
            raise ShutdownLocked(message)
        raise GuardError(message)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--rollback-worker":
    raise SystemExit(rollback_worker(Path(sys.argv[2]), sys.argv[3]))
