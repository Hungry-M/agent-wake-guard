# Agent Wake Guard

[简体中文](README.zh.md)

Agent Wake Guard keeps a Windows 10 or 11 x64 PC awake while supported desktop agents run local tasks. It can lock the session when you close the lid, then sleep, hibernate, or shut down once tasks finish. Version 0.1.0 defaults to hibernation after 180 seconds of continuous idle.

| Client | Built-in support in 0.1.0 |
| --- | --- |
| Codex Desktop for Windows | Local desktop tasks and queue state |
| DeepSeek Harness Desktop for Windows | Local desktop tasks and queue state |
| Browser and CLI sessions | Task-source adapter required |
| Claude Code, ZCode, CodeBuddy / WorkBuddy, Kimi, TRAE, pi, Hermes and other harnesses | Task-source adapter required. [Request support or contribute an adapter](#contributing). |

## Run the portable app

1. Put `Agent-Wake-Guard.exe` in a normal writable folder. Run it before or during a task. Windows may show an unknown-publisher prompt for unsigned builds. Check where the file came from and compare its SHA-256 with the checksum published with that build.
2. After the tray or status window reports the task state, you can close the lid. The Guard temporarily changes the lid action and restores the original plan when it exits. Optional device plugins may require UAC approval before the Guard is ready.
3. Click the tray icon to open the status window and choose **After tasks finish**. Use **Stop**, run `--stop`, or launch the program a second time to cancel the running Guard.

Use `--keep-awake-only` to check lid behavior without scheduling a power action. The Guard waits for a task to be observed at least once. Running, queued, unreadable or unresolved task state keeps the countdown from running. New work and keyboard or mouse activity reset it.

## Language and settings

**Open config** opens `%LOCALAPPDATA%\CodexS4Guard\config.ini`. This state directory is retained from Codex S4 Guard so existing settings and recovery records remain usable.

| Setting | Values |
| --- | --- |
| `language` | `auto`, `en`, `zh-Hans`. Auto uses Simplified Chinese for a matching Windows display language and English otherwise. Restart to apply, or use `--language` for one launch. |
| `completion_action` | `hibernate` (default), `sleep`, `shutdown`, `none` |
| `delay_seconds`, `poll_seconds` | Idle delay and scan interval. Defaults are 180 and 2 seconds. |
| `ui_mode` | `tray` or `window` |
| `keep_lid_policy` | `true` keeps the Windows lid policy and skips the Guard's automatic lid lock. |
| `codex_dir`, `deepseek_dir` | Optional desktop data paths. Defaults are `%USERPROFILE%\.codex` and `%USERPROFILE%\.dsh` or the apps' environment overrides. |

The completion action, idle delay and scan interval reload while running. Explicit launch options take precedence.

```bat
start "" /b /wait Agent-Wake-Guard.exe --version
start "" /b /wait Agent-Wake-Guard.exe --language en --status
start "" /b /wait Agent-Wake-Guard.exe --keep-awake-only
start "" /b /wait Agent-Wake-Guard.exe --restore-only
start "" /b /wait Agent-Wake-Guard.exe --uninstall
```

`--uninstall` stops the Guard, recovers its power changes and removes its executable, config and run logs. You can copy the config first if you want to keep it. Optional plugin directories can be removed separately after recovery.

## Plugins

- **Task sources** report activity to the countdown. Place a trusted `<id>.source-plugin.exe` under `Agent-Wake-Guard.sources` beside the EXE and restart. See the [protocol and example](TASK_SOURCES.md).
- **Device plugins** apply and restore hardware-specific policies. Place a trusted `<id>.guard-plugin.exe` in `Agent-Wake-Guard.plugins`. The optional [Huawei Hiview example](huawei_plugin.py) requests UAC to change one supported AutoTurnS4 rule and reload the supervised Hiview child. It is built separately from the portable app. Compatibility depends on the installed PC Manager version and layout.

## Troubleshooting and privacy

- Check `powercfg /a` for hardware sleep support. On S0 Modern Standby, **Sleep** releases the keep-awake request and requests screen-off. Windows decides when to enter low power. If Windows rejects shutdown with locked-session error 1271, the Guard rechecks task activity and settings before requesting S4.
- Compare `lid_plan_active`, `monitoring_ready` and `exited reason=…` in `%LOCALAPPDATA%\CodexS4Guard\run.log` with Windows power events. [Windows Modern Standby](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby) · [Maintenance and device protocol (Chinese)](MAINTENANCE.zh.md)
- Built-in sources read local lifecycle metadata. The Guard does not upload it. Apps can use different journal formats by version. Keep the DeepSeek Desktop data directory separate from a CLI that writes the same journal format.
- Before attaching diagnostics to an issue, remove account names, private paths, task content, machine identifiers, network addresses and credentials. Logs and `--status` can expose local paths. Share the minimum redacted excerpt.

## Build and contribute

On Windows x64 with Python 3.10 or newer, run from a regular PowerShell:

```powershell
python -m pip install -r requirements-build.txt
python -B -m unittest discover -s tests
.\build.ps1
# Optional, independent builds
.\build.ps1 -Target huawei
.\build.ps1 -Target idle-source
Get-FileHash -Algorithm SHA256 .\dist\Agent-Wake-Guard.exe
```

The executable uses the user's Windows temporary folder and can run without a separate Python install. See the [third-party notices](THIRD_PARTY_NOTICES.md) when distributing a binary. Local self-signing can validate a test build on your own machine. Public binaries should either identify a publicly trusted signer or be described as unsigned.

### Contributing

Issues and pull requests for task sources are welcome. Use the **Task source request** issue form and include the exact client and version, how it exposes task state, and sanitized evidence of start, completion and failure. Read [CONTRIBUTING.md](CONTRIBUTING.md) for DCO sign-off and contribution terms.

Source code is available under the [MIT License](LICENSE).
