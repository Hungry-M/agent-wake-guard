# Task-source protocol v1

Task sources let other desktop, CLI or browser-based harnesses participate in the countdown. The Guard includes adapters for Codex Desktop and DeepSeek Harness Desktop. Contributors can add other clients independently of the core executable.

## Install an adapter

Put a trusted executable in `Agent-Wake-Guard.sources` alongside the main EXE. Name it `<id>.source-plugin.exe`. IDs contain 1–48 lowercase letters, digits, underscores or hyphens and start with a letter or digit. Restart the Guard after adding, replacing or removing an adapter. The separate `.plugins` directory is for device plugins.

## Transport

The Guard runs the adapter as the current Windows user with argument `--source-plugin-v1` and its directory as the working directory. It sends one JSON object per line on stdin and expects one response per request on stdout. Use UTF-8, flush each newline, and keep each response under 64 KiB. Close on stdin EOF. The parent sends the selected language as `AGENT_WAKE_GUARD_LANGUAGE`. Human-readable errors may use that language.

At startup:

```json
{"command":"hello"}
{"command":"hello","ok":true,"protocol":1}
```

During each poll:

```json
{"command":"sample","boot":1700000000.0,"since":1700001234.5}
{"command":"sample","ok":true,"available":true,"observed":true,"busy":1,"attention":0,"revision":"7"}
```

| Field | Meaning |
| --- | --- |
| `boot` and `since` | Windows boot and Guard start times as Unix seconds. |
| `available` | True when the result is reliable. Use false while the client is unreadable or uncertain. |
| `observed` | True after the adapter sees a running task at launch or evidence of work since `since`. A confirmed idle or closed client with no task evidence returns false. |
| `busy` | Running plus queued task count. |
| `attention` | Count of tasks waiting on approval, interrupted, or otherwise needing attention. |
| `revision` | An opaque string, 1–128 ASCII letters, digits, dots, underscores, colons or hyphens. Change it when lifecycle evidence changes. Keep it stable during quiet polls. |

Counts are integers from 0 to 1,000,000. An unavailable source, a pending task or a task needing attention blocks completion. A timeout, crash or malformed response keeps the source unavailable for the rest of the run. Restart after fixing it. A temporarily unreadable but healthy adapter can return `available:false` and recover on a later poll.

The Guard gives each request 4 seconds. Cache the latest snapshot in the adapter when polling the client takes longer. `--status` also samples installed adapters, so design them for read-only use and concurrent instances.

## Contribute a source

- Prefer an official local event API, a CLI status command, or stable lifecycle metadata. For a browser adapter, use a companion explicitly enabled by the user and scoped to the relevant site.
- Report counts and lifecycle changes. Keep cookies, credentials, prompts, chat messages, page contents and private paths out of stdout and issue attachments. Use synthetic fixtures.
- Verify startup before and during an existing task, a quick task between polls, concurrency, queues, cancellation, approval, client close, and unreadable state. Change `revision` even if a quick start/finish leaves the counts unchanged.
- Start from [the idle adapter](examples/idle_task_source.py), build it with `./build.ps1 -Target idle-source`, and check it via `--status` or `--dry-run`. This example always reports idle and never arms the countdown. Use the **Task source request** Issue form to propose a client.
