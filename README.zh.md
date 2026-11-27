# Agent Wake Guard

[English](README.md)

Agent Wake Guard 在 Windows 10/11 x64 上守护桌面 Agent 的本机任务。它可以在合盖时熄屏、锁定并继续运行任务，等全部任务结束后再睡眠、休眠或关机。0.1.0 默认连续空闲 180 秒后休眠。

| 客户端 | 0.1.0 内置支持 |
| --- | --- |
| Windows Codex Desktop | 本机桌面任务和队列 |
| Windows DeepSeek Harness Desktop | 本机桌面任务和队列 |
| 网页端和命令行端 | 需要任务来源适配器 |
| Claude Code、ZCode、CodeBuddy / WorkBuddy、Kimi、TRAE、pi、Hermes 等 | 需要任务来源适配器。欢迎[提出需求或提交适配](#贡献)。 |

## 运行便携版

1. 将 `Agent-Wake-Guard.exe` 放在有写权限的普通目录。任务开始前或运行中都可以启动。未签名构建可能显示「未知发布者」，请先核对下载来源和随发布提供的 SHA-256。
2. 等托盘或状态窗口显示任务状态再合盖。守护会使用临时电源计划并在退出时恢复。若启用了设备插件，可能需要先在 Windows 确认 UAC。
3. 单击托盘图标打开状态窗口，从「任务结束后」下拉框选择动作。「停止守护」、`--stop` 或重复启动都可停止当前实例。

用 `--keep-awake-only` 可以先验证合盖保活。至少观察到一次任务后才会开始考虑结束动作。运行、排队、无法读取或需要处理的任务状态会暂停倒计时。新任务与键鼠活动会重置倒计时。

## 语言和配置

「打开配置」会打开 `%LOCALAPPDATA%\CodexS4Guard\config.ini`。沿用 Codex S4 Guard 的目录，以读取已有配置和恢复记录。

| 配置项 | 取值 |
| --- | --- |
| `language` | `auto`、`en`、`zh-Hans`。默认跟随 Windows 显示语言，简体中文用中文，其余用英文。重启后生效，单次可用 `--language` 指定。 |
| `completion_action` | `hibernate`（默认休眠）、`sleep`（睡眠）、`shutdown`（关机）、`none`（仅保活） |
| `delay_seconds`、`poll_seconds` | 空闲延迟与检查间隔，默认分别是 180 秒、2 秒。 |
| `ui_mode` | `tray`（托盘）或 `window`（窗口） |
| `keep_lid_policy` | 设为 `true` 时沿用 Windows 合盖设置，Guard 跳过自动合盖锁屏。 |
| `codex_dir`、`deepseek_dir` | 可选的桌面数据目录。默认使用 `%USERPROFILE%\.codex`、`%USERPROFILE%\.dsh` 或相应的应用环境变量。 |

结束动作、空闲延迟和检查间隔支持运行时重载。显式启动参数优先。

```bat
start "" /b /wait Agent-Wake-Guard.exe --version
start "" /b /wait Agent-Wake-Guard.exe --language zh-Hans --status
start "" /b /wait Agent-Wake-Guard.exe --keep-awake-only
start "" /b /wait Agent-Wake-Guard.exe --restore-only
start "" /b /wait Agent-Wake-Guard.exe --uninstall
```

`--uninstall` 会停止守护并恢复电源变更，然后删除主 EXE、配置和守护日志。要保留配置可先复制。恢复完成后可另行移除可选插件目录。

## 插件

- **任务来源**向倒计时报告任务活动。将可信的 `<id>.source-plugin.exe` 放在主 EXE 相邻的 `Agent-Wake-Guard.sources` 目录并重启。见[协议与示例](TASK_SOURCES.md)。
- **设备插件**临时应用和恢复特定设备的电源设置。将可信的 `<id>.guard-plugin.exe` 放入 `Agent-Wake-Guard.plugins`。[华为 Hiview 示例](huawei_plugin.py)独立构建，经 UAC 确认后修改兼容的 AutoTurnS4 规则，并重载电脑管家监管的 Hiview 子进程。兼容性取决于电脑管家的版本和安装布局。

## 排查与隐私

- 用 `powercfg /a` 查看本机支持的电源状态。S0 现代待机机型选择「睡眠」时，Guard 释放保活并请求熄屏，何时进入低功耗由 Windows 决定。若锁屏时关机返回 1271，Guard 会再次核对任务与配置，再请求 S4。
- 对照 `%LOCALAPPDATA%\CodexS4Guard\run.log` 的 `lid_plan_active`、`monitoring_ready`、`exited reason=…` 和 Windows 电源事件。[维护、排查与设备插件协议](MAINTENANCE.zh.md)
- 内置适配器读取本机任务状态，守护程序不会上传数据。应用版本可能改变日志格式。若 DeepSeek CLI 也写入相同格式，请为 Desktop 单独指定数据目录。
- 发 Issue 前先删除用户名、私人路径、任务内容、设备标识、网络地址和凭据。`--status` 和部分错误输出可能包含本地路径，贴脱敏后的最少片段即可。

## 构建

在 Windows x64 安装 Python 3.10 或更新版本，用普通 PowerShell 执行：

```powershell
python -m pip install -r requirements-build.txt
python -B -m unittest discover -s tests
.\build.ps1
# 两个可独立构建的可选示例
.\build.ps1 -Target huawei
.\build.ps1 -Target idle-source
Get-FileHash -Algorithm SHA256 .\dist\Agent-Wake-Guard.exe
```

便携 EXE 使用 Windows 用户临时目录，运行时无需另装 Python。分发时参照[第三方声明](THIRD_PARTY_NOTICES.md)。自签名用于本机验证。公开构建注明签名者，未签名时明确说明。

## 贡献

欢迎通过 Issue 和 PR 补充任务来源。提交「Task source request」时，请注明客户端及版本、可读状态来源和脱敏后的生命周期示例。贡献署名与再许可条款见 [CONTRIBUTING.md](CONTRIBUTING.md)。项目源码采用 [MIT 许可证](LICENSE)。
