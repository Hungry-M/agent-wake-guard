"""Strings and Windows UI language selection for the portable app."""

import ctypes
import os

APP_NAME = "Agent Wake Guard"
VERSION = "0.1.0"
LANGUAGE_ENV = "AGENT_WAKE_GUARD_LANGUAGE"
LANGUAGES = ("auto", "en", "zh-Hans")

ZH_HANS = {
    "The task source returned an invalid snapshot.": "任务来源返回了无效的状态。",
    "The task source must support protocol version 1.": "任务来源需要支持协议版本 1。",
    "Task source is temporarily unavailable": "任务来源暂不可用",
    "Keep Windows awake through all local Codex and DeepSeek desktop tasks.": "在 Codex 和 DeepSeek 的本机任务运行期间保持 Windows 唤醒。",
    "set the interface language for this launch": "指定本次启动使用的界面语言",
    "idle time before the power action, default 180 seconds": "执行电源动作前的空闲时间，默认 180 秒",
    "task check interval, default 2 seconds": "任务状态检查间隔，默认 2 秒",
    "custom Codex data directory": "自定义 Codex 数据目录",
    "custom DeepSeek data directory": "自定义 DeepSeek 数据目录",
    "path to a Node.js executable with Zstandard support": "支持 Zstandard 的 Node.js 可执行文件路径",
    "keep the existing Windows lid action": "沿用现有的 Windows 合盖策略",
    "power action after all tasks complete": "全部任务完成后的电源动作",
    "keep Windows awake without an automatic power action": "持续保活，任务完成后保持唤醒",
    "show either a tray icon or a status window": "选择托盘图标或状态窗口",
    "skip enabling the installed Huawei plugin": "不启用已安装的华为插件",
    "show decisions without requesting power changes": "预览判断，保持当前电源状态",
    "inspect task sources once without changing power": "读取一次任务状态，保持当前电源状态",
    "recover pending Windows and device settings": "恢复待处理的 Windows 和设备设置",
    "stop the running guard and restore the original power plan": "停止守护并恢复原电源计划",
    "stop the guard, restore power, and remove this portable executable": "停止守护，恢复电源设置并删除便携 EXE",
    "open the optional config.ini in Notepad": "用记事本打开可选配置 config.ini",
    "show this help message and exit": "显示帮助后退出",
    "show the program version and exit": "显示版本号后退出",
    "all local tasks": "全部本机任务",
    "keep-awake only": "仅保活",
    "{action} after {seconds}s idle": "空闲 {seconds} 秒后{action}",
    "Dry run: this window does not change Windows power state.": "预演：本窗口保持 Windows 当前电源状态。",
    "Sleep": "睡眠",
    "Hibernate": "休眠",
    "Shut down": "关机",
    "Keep awake": "仅保活",
    "Starting": "正在启动",
    "Starting task monitoring and device plugins.": "正在初始化任务监控和设备插件。",
    "Windows could not remove the tray icon. Try again.": "Windows 未能撤下通知区域图标，请重试。",
    "Keep window": "固定为窗口",
    "Switch to tray": "切换为托盘",
    "The tray is unavailable. The status window is still open.": "Windows 通知区域暂不可用，已保留状态窗口。",
    "After tasks finish": "任务结束后",
    "Set at launch": "启动参数指定",
    "Could not load the power actions.": "无法加载结束动作列表。",
    "Stop": "停止守护",
    "Open config": "打开配置",
    "Stopping": "正在停止",
    "Show status": "查看状态",
    "Switch to window": "切换为窗口",
    "Lid closed. Windows was asked to lock the session.": "已合盖，已请求 Windows 锁屏。",
    "The Guard stopped and restored the original power plan.": "守护已停止，原电源计划已恢复。",
    "Could not start the power-plan recovery process.": "无法启动电源恢复进程。",
    "The power-plan recovery process did not initialize.": "电源计划恢复进程未能启动。",
    "Windows did not report the lid state. Automatic locking is unavailable.": "Windows 未报告盖子状态，自动锁屏不可用。",
    "Could not safely update {key} in config.ini.": "无法安全地更新 config.ini 中的 {key}。",
    "Another program modified config.ini. Try again.": "config.ini 已被其他程序修改，请重试。",
    "{path}: use a single [guard] section.": "{path} 应只有一个 [guard] 节。",
    "{path}: unknown settings: {keys}.": "{path} 包含未知设置 {keys}。",
    "completion_action must be sleep, hibernate, shutdown or none.": "completion_action 应为 sleep、hibernate、shutdown 或 none。",
    "The delay must be at least 1 second and the poll interval at least 0.25 seconds.": "动作等待时间须至少 1 秒，检查间隔须至少 0.25 秒。",
    "ui_mode must be tray or window.": "ui_mode 应为 tray 或 window。",
    "disabled_plugins must contain comma-separated plugin IDs.": "disabled_plugins 应为逗号分隔的插件 ID。",
    "language must be auto, en or zh-Hans.": "language 应为 auto、en 或 zh-Hans。",
    "config.ini is missing. Save or restore the file.": "找不到 config.ini，请保存或恢复配置。",
    "{action} in {seconds}s": "{seconds} 秒后{action}",
    "All tasks finished. New tasks or keyboard and mouse activity reset the timer.": "任务已完成。新的任务或键鼠活动会重置倒计时。",
    "Waiting for task status": "等待任务状态",
    "Task status is temporarily unavailable. The Guard is still active.": "暂时无法确认任务状态，守护仍在运行。",
    "Tasks need attention": "有任务需要处理",
    "Check the tasks that need attention. The Guard is still active.": "任务状态需要确认，守护仍在运行。",
    "Tasks running": "任务运行中",
    "Running or queued: {tasks}": "运行或排队中：{tasks}",
    "The PC will stay awake after tasks finish.": "任务完成后保持唤醒。",
    "Ready": "已启用",
    "Waiting to confirm idle status.": "等待确认空闲状态。",
    "Waiting for the first Codex or DeepSeek task.": "正在等待首次 Codex 或 DeepSeek 任务。",
    "Waiting for the next confirmed idle snapshot.": "等待下一次空闲确认。",
    "Waiting for readable task state": "等待可读取的任务状态",
    "Attention required": "有任务需要处理",
    "Tasks active or queued": "任务运行或排队中",
    "Keep-awake only | no automatic power action.": "仅保活 | 任务结束后保持唤醒。",
    "All observed tasks finished | {action} in {seconds}s of continuous idle.": "已监控到的任务均已完成 | 连续空闲 {seconds} 秒后{action}。",
    "S4 fallback failed.": "S4 回退失败。",
    "Lid protection is ready. Closing the lid locks the session and keeps tasks running.": "合盖保护已就绪。合上盖子会自动锁屏，任务继续运行。",
    "Check configuration": "配置待修正",
    "Automatic power actions are paused.": "已暂停自动电源动作。",
    "The running Guard was stopped and its power plan was restored.": "检测到守护已在运行。已停止守护并恢复电源计划。",
    "The view changed, but config.ini could not be saved.": "已切换界面，保存 config.ini 失败。",
    "Could not save the power action.": "保存结束动作失败。",
    "The Guard stopped.": "守护已退出。",
    "Guard stopped: {reason}": "守护已退出：{reason}",
    "Dry run: would request Windows {action} now.": "预演：此时会请求 Windows {action}。",
    "Requesting Windows {action}.": "正在请求 Windows {action}。",
    "Windows rejected shutdown while locked (1271). Requesting Windows hibernate.": "Windows 在锁屏状态拒绝关机（1271），正在请求休眠。",
    "Automatic power action paused | config.ini: {reason}": "已暂停自动电源动作 | config.ini：{reason}",
    "Monitoring cancelled. Releasing the Windows power request.": "监控已取消，正在释放 Windows 保活请求。",
    "Another guard was detected. Cancellation sent.": "检测到另一守护实例，已发送停止请求。",
    "Guard stopped. The duplicate launch will close; this notice remains.": "守护已停止。关闭此提示即可退出重复启动的实例。",
    "Huawei automatic lid-to-S4 is temporarily paused.": "华为合盖自动 S4 已临时暂停。",
    "Could not read the Huawei AutoTurnS4 setting.": "华为 AutoTurnS4 配置无法读取。",
    "The Huawei AutoTurnS4 setting was not recognized.": "无法识别本机华为 AutoTurnS4 配置。",
    "Pausing Huawei automatic hibernation requires Windows administrator approval.": "暂停华为自动休眠需要通过 Windows 管理员确认。",
    "Huawei settings still need recovery. Recovery file: {path}": "华为设置恢复未完成。恢复记录：{path}",
    "The Huawei helper exited. Check that Huawei PC Manager and Hiview are running.": "华为自动休眠助手退出。请确认华为电脑管家及 Hiview 正在运行。",
    "Timed out waiting for the Huawei change to take effect.": "等待华为设置生效超时。",
    "Could not identify the process that owns the Huawei helper.": "无法确定华为设置助手的守护进程。",
    "Waiting for administrator approval": "等待管理员确认",
    "Approve the Windows prompt to temporarily pause Huawei automatic hibernation on lid close.": "请在 Windows 提示中允许助手临时暂停华为合盖自动休眠。",
    "Approve the Windows prompt to restore the Huawei lid settings.": "请在 Windows 提示中允许助手恢复华为合盖设置。",
    "The Huawei helper is still restoring settings.": "华为设置助手仍在恢复。",
    "Put this executable in the Guard's .plugins directory.": "请将此程序放在守护程序相邻的 .plugins 目录。",
    "Could not send {command}.": "无法发送 {command}。",
    "No valid reply to {command}. The plugin may have exited or returned invalid data.": "未收到 {command} 的有效回复，插件已退出或协议数据无效。",
    "Timed out waiting for {command}.": "等待 {command} 回复超时。",
    "The {command} reply is invalid JSON.": "{command} 回复不是有效的 JSON。",
    "The {command} reply must be a JSON object.": "{command} 回复必须为 JSON 对象。",
    "The status event must provide title and detail.": "status 事件必须提供 title 和 detail。",
    "Received an out-of-order reply to {command}.": "{command} 收到顺序不符的回复。",
    "{command} failed.": "{command} 执行失败。",
    "The start reply has invalid active or status fields.": "start 回复包含无效的 active 或 status。",
    "The plugin did not exit in time and was terminated.": "进程未在规定时间内退出，已结束该进程。",
    "The plugin must support protocol version 1.": "插件协议版本不兼容，需要版本 1。",
    "Plugins still need recovery: {plugins}. Reinstall them in {directory} and run --restore-only.": "以下插件仍待恢复：{plugins}。请放回 {directory} 后运行 --restore-only。",
}

_language = "en"


def language_setting(value):
    return {name.casefold(): name for name in LANGUAGES}[value.strip().casefold()]


def ui_language():
    """Report the Windows UI language, independent of console encoding."""
    if os.name == "nt":
        return ctypes.windll.kernel32.GetUserDefaultUILanguage()
    return 0


def select_language(value="auto"):
    global _language
    setting = language_setting(value)
    if setting == "auto":
        setting = "zh-Hans" if ui_language() in {0x0004, 0x0804, 0x1004, 0x7804} else "en"
    _language = setting


def current_language():
    return _language


def tr(message, **values):
    localized = ZH_HANS.get(message, message) if _language == "zh-Hans" else message
    return localized.format(**values) if values else localized


def title(message):
    return f"{APP_NAME} · {tr(message)}"
