# Agent Wake Guard 维护与设备插件开发

先核对任务状态、原电源计划和 Windows 电源事件，再判断问题出在 Guard、设备插件还是系统策略。以下步骤适用于人工排查，也可作为本地 Agent 的检查顺序。[使用说明](README.zh.md)

## 确认本机支持的电源状态

```cmd
powercfg /a
powercfg /getactivescheme
powercfg /requests
powercfg /lastwake
```

| 输出 | 怎么判断 |
| --- | --- |
| `S0 低电量待机` | 本机睡眠使用现代待机。普通 S0 工作状态也允许熄屏和锁屏，持续运算时应保持在工作状态。S0 实际进入深度待机的时间可看 SleepStudy。 |
| `S1`、`S2`、`S3` | 传统睡眠的三个编号。只使用 `/a` 报告可用的睡眠模式。 |
| `休眠` | S4 可用，系统保存内存会话到磁盘，醒来时恢复。 |
| `快速启动` | Windows 可以在结束用户会话后保存内核状态，缩短下次启动时间。依据 `/a` 中的「休眠」项判断能否保存整个会话。 |
| `关机` | `completion_action = shutdown` 先请求 `shutdown.exe /s /t 0`。锁屏返回 `1271` 时，重新确认任务及配置后请求 S4。 |

`/a` 同时列出可用和不可用项。固件、Windows 策略和休眠文件状态都可能影响结果。`/requests` 看当前阻止待机的请求，`/lastwake` 看最近的唤醒来源。[Windows 电源状态](https://learn.microsoft.com/en-us/windows/win32/power/system-power-states) · [Powercfg 选项](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/powercfg-command-line-options)

## 排查未保活、未执行动作或提前休眠

1. 在 EXE 所在目录的 CMD 执行 `start "" /b /wait Agent-Wake-Guard.exe --status`。保存 `completion_action`、`observed`、`busy`、`attention`、`unavailable`、`task_sources` 和 `plugins` 字段。`--status` 是单次采样，运行中的实例以自己的任务历史、启动参数和最后成功重载的设置为准。若托盘显示「配置待修正」，修正并保存 `config.ini`，守护会自动重读并重新判断倒计时。向外部提供诊断时先删除本地路径和账号信息。
2. 用 `--dry-run` 复现任务判断。第一次检测到任务前程序会等待。观察常驻或排队任务、待处理错误、日志尚未写完和是否有人操作键鼠。`--dry-run` 只预览决定。测试合盖时可用 `--keep-awake-only`，程序仍会实际接管电源计划和已启用的设备保护。
3. 查看 `%LOCALAPPDATA%\CodexS4Guard\run.log`。`lid_plan_active` 表示临时电源计划已生效，`monitoring_ready` 表示设备插件和任务监控也就绪。`exited reason=stop_requested` 表示程序收到停止请求，`reason=error` 表示 Guard 遇到异常，`reason=completion` 表示已发出结束动作。日志随正常运行写入，保留当前文件和一个轮换文件。
4. 将本地任务时间、Guard 日志和 Windows 系统事件对照。合盖后任务无进度时，对照合盖与 `lid_plan_active` 的时间，并检查 Kernel-Power 507 的 `SleepEntered` 和 `SleepDurationInUs`。合盖先于电源计划生效时，原合盖策略可能已经让 Windows 进入待机。

```powershell
$guardSince = (Get-Date).AddHours(-12)
Get-WinEvent -FilterHashtable @{
  LogName = 'System'
  ProviderName = 'Microsoft-Windows-Kernel-Power'
  Id = 42,107,187,506,507
  StartTime = $guardSince
} | Select-Object TimeCreated, Id, Message
```

S0 机型可在管理员终端用 `powercfg /sleepstudy /output "$env:TEMP\CodexS4Guard-SleepStudy.html"` 对照 `Screen Off` 与 `Sleep` 阶段。`power_dispatched action=sleep mode=modern_screen_off` 表示 Guard 已释放保活并发出熄屏请求，何时进入待机由 Windows 根据电源策略和活动决定。若排查关机请求，可查询系统日志中的 User32 1074，并阅读事件中的发起程序和原因。[SleepStudy](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby-sleepstudy) · [Windows 关机事件排查](https://learn.microsoft.com/en-us/troubleshoot/windows-server/performance/troubleshoot-unexpected-reboots-system-event-logs)

遇到 `1271`（`ERROR_MACHINE_LOCKED`）时，在 `run.log` 中查找 `shutdown_locked` 后的 `power_requested action=hibernate`，再核对 Kernel-Power 187、42 和实际唤醒记录。若回退失败，结合提示及 `powercfg /a` 核对 S4 是否可用。其他关机错误保留原始错误码。[Windows 错误码](https://learn.microsoft.com/en-us/windows/win32/debug/system-error-codes--1000-1299-)

## 排查插件与恢复设置

| 现象 | 检查 |
| --- | --- |
| `plugins.installed` 为空 | `plugins.directory` 是主 EXE 相邻的实际目录。核对文件名是否为 `<id>.guard-plugin.exe`，扩展名是否完整。目录为空时可正常运行通用守护。 |
| 文件已被识别，插件未开启保护 | 查看 `disabled_plugins` 和旧配置 `block_huawei_s4`。华为插件只在识别到 `AutoTurnS4` 启用时申请修改。恢复旧记录时也可能出现 UAC。 |
| 启动报协议、超时或 UAC 错误 | 使用 CMD 启动查看插件文件名和失败命令。独立检查插件能否运行，再核对协议版本及是否有助手等待授权。 |
| Guard 停止后 `plugins.pending` 仍有条目 | 将同 ID 的兼容插件放回目录，执行 `--restore-only`，等成功后再移除或更新。启用中的插件也会显示在 `pending`，表示其设置在退出前需要恢复。 |
| 原电源计划没有恢复 | 保留 `%LOCALAPPDATA%\CodexS4Guard\power-plan.json`，停止 Guard 后运行 `--restore-only`。用 `powercfg /getactivescheme` 和 `/list` 核对原计划和 Guard 创建的临时 GUID。 |

兼容华为机型可对照 `%ProgramData%\Comms\Hiview\HiviewService.log` 中的 `Force turn to S4`、系统电源事件和任务时间线。在 `%ProgramFiles%\Huawei\Hiview\updateDir\Dubai.xml` 中核对 `AnomalyMs` 下的 `AutoTurnS4`，并查看 Hiview 安装目录中的 `.CodexS4Guard-AutoTurnS4`。该恢复记录是插件恢复原值的依据。旧版内置模块留下的记录也可由新版外置插件处理。

新版本第一次使用插件时，先执行 `--keep-awake-only` 并批准所需 UAC。合盖超过 Hiview 触发窗口后，核对任务时间线是否连续，再停止 Guard，确认 Hiview 原值、恢复记录和 Windows 电源计划都已恢复。最后在允许中断的时段验收实际睡眠、休眠或关机。

## 编写设备插件

若要接入新的桌面应用、CLI 或网页任务，使用独立的[任务来源协议](TASK_SOURCES.md)。

设备插件是由 Guard 以同一普通用户身份启动的独立 EXE。文件名使用 `<id>.guard-plugin.exe`。ID 可用 1–48 个小写字母、数字、连字符或下划线，首字符须为字母或数字。替换同 ID 的兼容 EXE 即可更新。可用任意语言实现协议，Python 插件可参照 [华为入口](huawei_plugin.py) 和 [通用协议端](device_plugins/worker.py)。

Guard 用 `--guard-plugin-v1` 启动插件。标准输入和输出每行一个 UTF-8 JSON 对象，使用换行结束并立即刷新，每行最多 65,536 字节。标准输出保留给协议。插件需要长期日志时应写入自己独立的本地日志，错误摘要通过响应返回。

| Guard 发送 | 插件执行 | 成功回复 |
| --- | --- | --- |
| `{"command":"hello"}` | 检查协议，保持系统设置原状 | `{"command":"hello","ok":true,"protocol":1}` |
| `{"command":"start","enabled":true}` | 检测设备，先处理自己的旧恢复记录，再对匹配设备启用保护 | `{"command":"start","ok":true,"active":true,"status":"保护已开启"}` |
| `{"command":"start","enabled":false}` | 仅检查并恢复自己的遗留设置 | `{"command":"start","ok":true,"active":false}` |
| `{"command":"check"}` | 确认活动会话和独立助手仍可用 | `{"command":"check","ok":true}` |
| `{"command":"restore"}` | 在系统电源转换之前恢复设置并验证 | `{"command":"restore","ok":true}` |
| `{"command":"suspend"}` | 新任务取消转换时重新启用设备保护 | `{"command":"suspend","ok":true}` |
| `{"command":"stop"}` | 恢复并校验所有改动，随后退出 | `{"command":"stop","ok":true}` |

设备无匹配时 `start` 返回 `active: false`。每个请求只发一个终结回复，`command` 必须与请求一致。失败使用 `{"command":"restore","ok":false,"error":"具体失败原因"}` 这类回复。等待 UAC 时可先发状态事件 `{"event":"status","title":"等待管理员确认","detail":"需要恢复设备设置"}`。默认时限为 `hello` 20 秒、`start` 140 秒、`check` 8 秒，`restore`、`suspend`、`stop` 各 70 秒。`check` 应轻量，保持幂等的 `restore`、`suspend` 和 `stop`。

修改前先持久化插件自己的原值和恢复记录。输入 EOF 表示 Guard 退出或崩溃，插件应执行与 `stop` 相同的恢复。若插件或整机突然退出，下次 `start` 必须能从持久化记录恢复。Guard 仅在收到成功的 `stop` 后清除主程序的待恢复标记。切换目标电源状态由 Guard 统一决定，插件只管理特定设备的设置。需要管理员权限的修改应通过系统 UAC 请求，仅让完成修改和恢复的助手获得权限。

验收顺序建议为无插件启动、安装测试插件、禁用时恢复、完成前恢复、复核出现新任务时重新保护、断开输入管道时恢复、替换插件后保持主 EXE 哈希一致。对真实设备先使用可撤销的测试设置。保留系统事件和插件错误原文，向他人或 AI 提供日志时可先去掉任务正文和用户私有路径。
