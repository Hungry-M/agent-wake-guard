"""Read task lifecycles without acting on Codex or DeepSeek sessions."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from i18n import tr

try:
    from deepseek_reader import read_journals
except ModuleNotFoundError as exc:
    if exc.name != "zstandard":
        raise
    read_journals = None  # Older source installs can continue to use Node.js.

HERE = Path(__file__).resolve().parent
MARKERS = (b'"task_started"', b'"task_complete"', b'"turn_aborted"')
UUID_SUFFIX = re.compile(r"([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\.jsonl$", re.I)


@dataclass(frozen=True, order=True)
class Fact:
    at: float
    status: str
    id: str


@dataclass(frozen=True)
class Snapshot:
    observed: bool
    busy: tuple[tuple[str, int], ...]
    attention: tuple[tuple[str, int], ...]
    unavailable: tuple[str, ...]
    revision: tuple

    @property
    def blocked(self):
        return bool(self.busy or self.attention or self.unavailable)


def timestamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10**11 else float(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def codex_event(line):
    if b'"event_msg"' not in line or not any(key in line for key in MARKERS):
        return None
    row = json.loads(line)
    message = row.get("payload", {})
    if row.get("type") != "event_msg":
        return None
    status = {"task_started": "running", "task_complete": "completed", "turn_aborted": "attention"}
    kind = message.get("type")
    if kind not in status:
        return None
    return Fact(timestamp(row["timestamp"]), status[kind], str(message["turn_id"]))


class Rollouts:
    def __init__(self, root):
        self.root = root
        self.cache = {}

    def read(self, path, minimum, from_desktop):
        stat = path.stat()
        if stat.st_mtime < minimum - 2:
            self.cache.pop(path, None)
            return None, False
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        previous = self.cache.get(path)
        if previous and previous[0] == signature:
            return previous[2:]
        with path.open("rb") as stream:
            append = (previous and stat.st_ino == previous[0][2]
                      and stat.st_size > previous[0][1] and stat.st_mtime_ns >= previous[0][0])
            if append:
                offset, fact = previous[1:3]
                stream.seek(offset)
                while stream.tell() < stat.st_size:
                    line = stream.readline(stat.st_size - stream.tell())
                    if not line.endswith(b"\n"):
                        break
                    offset = stream.tell()
                    event = codex_event(line)
                    if event:
                        fact = event
            else:
                if not from_desktop:
                    header = stream.readline(min(stat.st_size, 128 * 1024))
                    try:
                        meta = json.loads(header)
                    except (ValueError, RecursionError):
                        return None, False
                    payload = meta.get("payload") if isinstance(meta, dict) else None
                    if (not isinstance(meta, dict) or meta.get("type") != "session_meta"
                            or not isinstance(payload, dict) or payload.get("originator") != "Codex Desktop"):
                        return None, False
                # The latest lifecycle is usually in the tail. Grow only for a long active turn.
                length = min(stat.st_size, 256 * 1024)
                fact = None
                offset = 0
                while length:
                    start = stat.st_size - length
                    stream.seek(start)
                    data = stream.read(length)
                    end = data.rfind(b"\n")
                    offset = start + end + 1
                    lines = data[:end].split(b"\n") if end >= 0 else []
                    for line in reversed(lines[1:] if start else lines):
                        fact = codex_event(line)
                        if fact:
                            break
                    if fact or start == 0 or length >= 16 * 1024 * 1024:
                        break
                    length = min(stat.st_size, 2 * length)
                if not fact and length < stat.st_size:
                    # A newer SQLite projection can still cover this file.
                    offset = -1
        partial = offset < 0 or offset != stat.st_size
        self.cache[path] = (signature, max(0, offset), fact, partial)
        return fact, partial

    def scan(self, minimum, desktop_ids):
        facts, incomplete = {}, set()
        changes = []
        for path in self.root.glob("**/rollout-*.jsonl"):
            match = UUID_SUFFIX.search(path.name)
            if not match:
                continue
            try:
                fact, partial = self.read(path, minimum, match[1] in desktop_ids)
            except FileNotFoundError:
                continue
            if fact:
                facts[match[1]] = fact
            if partial:
                incomplete.add(match[1])
            cached = self.cache.get(path)
            if cached:
                changes.append((match[1], cached[0][:2]))
        return facts, incomplete, tuple(sorted(changes))


def latest_database(home, pattern):
    paths = list(home.glob(pattern))
    return max(paths, key=lambda path: int(path.stem.rsplit("_", 1)[1])) if paths else None


def query(path, sql, parameters=()):
    if path is None:
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.6)) as db:
        db.execute("PRAGMA query_only=ON")
        return db.execute(sql, parameters).fetchall()


class Codex:
    def __init__(self, home):
        self.home = home
        self.rollouts = Rollouts(home / "sessions")

    def scan(self, minimum, since, opened):
        state = latest_database(self.home, "state_*.sqlite")
        columns = {column[1] for column in query(state, "PRAGMA table_info(threads)")}
        desktop_ids = {row[0] for row in query(state, "SELECT id FROM threads WHERE originator = 'Codex Desktop'")} \
            if "originator" in columns else set()
        facts, incomplete, changes = self.rollouts.scan(minimum, desktop_ids)
        desktop_ids.update(thread for thread, _ in changes)
        history = latest_database(self.home, "thread_history_*.sqlite")
        projection = {}
        for thread, turn, status, started, finished in query(history, """
            SELECT thread_id, turn_id, status, started_at, completed_at
            FROM thread_turns
            WHERE MAX(COALESCE(started_at,0), COALESCE(completed_at,0)) >= ?
            ORDER BY thread_id, rollout_ordinal
        """, (int(minimum) - 2,)):
            if thread not in desktop_ids:
                continue
            phases = {"completed": "completed", "failed": "attention",
                      "interrupted": "attention", "inProgress": "running"}
            projection[thread] = Fact(timestamp(finished or started), phases[status], turn)
        for thread, fact in projection.items():
            logged = facts.get(thread)
            if (logged is None or (fact.id != logged.id and fact.at >= logged.at)
                    or (fact.id == logged.id and (fact.status == "attention"
                                                 or logged.status == "running"))):
                facts[thread] = fact
        # A partial write is a hold, even if the SQL projection currently reports idle.
        queues = query(latest_database(self.home, "queue_*.sqlite"),
                       "SELECT thread_id, id, updated_at_ms FROM queued_items") if opened else []
        queues = [row for row in queues if row[0] in desktop_ids]
        goals = query(latest_database(self.home, "goals_*.sqlite"), """
            SELECT thread_id, goal_id, status, updated_at_ms
            FROM thread_goals WHERE status != 'complete'
        """) if opened else []
        recent_goals = [row for row in goals if row[0] in desktop_ids
                        and (row[2] == "active" or timestamp(row[3]) >= since)]
        return facts, len(queues) + len(recent_goals), (len(incomplete) if opened else 0), (
            tuple(sorted(queues)), tuple(sorted(recent_goals)), changes,
        )


class DeepSeek:
    def __init__(self, home, node=None):
        self.root = home / "sessions"
        self.node = node
        self.native_reader = read_journals if node is None else None
        self.cache = {}

    def read(self, requests):
        if self.native_reader:
            return self.native_reader(requests)
        node = self.node or shutil.which("node.exe") or shutil.which("node")
        if not node:
            raise RuntimeError("Install zstandard or use a Node.js runtime with Zstandard support.")
        result = subprocess.run(
            [node, str(HERE / "deepseek_reader.cjs")], input=json.dumps(requests),
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError("DeepSeek journal reader could not run. Node.js with Zstandard is required.")
        return json.loads(result.stdout)

    def scan(self, minimum, since, opened):
        requests, keys, failures = [], [], 0
        paths = self.root.glob("**/session*.jsonl*")
        facts, pending = {}, 0
        eligible = set()
        for path in paths:
            if ".bak" in path.name:
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if stat.st_mtime < minimum - 2:
                continue
            if path.name != "session.v4.jsonl.zstd":
                failures += int(opened)
                continue
            eligible.add(path)
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            cached = self.cache.get(path)
            if cached and cached[0] == signature and cached[1].get("complete"):
                continue
            old = cached[1] if (
                cached and signature[0] > cached[0][0] and signature[2] == cached[0][2]
                and signature[1] >= cached[0][1] and "error" not in cached[1]
            ) else {}
            keys.append((path, signature))
            requests.append({"path": str(path), "size": stat.st_size, "offset": old.get("offset", 0),
                             "fact": old.get("fact"), "inbox": old.get("inbox", {})})
        if requests:
            for (path, signature), entry in zip(keys, self.read(requests), strict=True):
                self.cache[path] = signature, entry
        for path in eligible:
            _, record = self.cache[path]
            failures += int(opened and not record.get("complete"))
            value = record.get("fact")
            if value:
                facts[str(path)] = Fact(**value)
                if opened and value["at"] >= minimum - 2:
                    pending += sum(record.get("inbox", {}).values())
        changes = tuple(sorted((str(path), self.cache[path][0][:2]) for path in eligible))
        return facts, pending, failures, (pending, changes)


class Monitor:
    def __init__(self, codex, deepseek, boot, since, node=None):
        self.sources = {"Codex": Codex(codex), "DeepSeek": DeepSeek(deepseek, node)}
        self.boot, self.since = boot, since
        self.observed = False

    def sample(self, applications, task_sources=None):
        busy, attention, unavailable, revision = [], [], [], []
        for name, observer in self.sources.items():
            launch = applications.get(name)
            opened = launch is not None
            if name == "DeepSeek" and not opened:
                continue
            minimum = min(self.since, max(self.boot, launch)) if opened else self.since
            try:
                facts, queued, incomplete, queue_revision = observer.scan(minimum, self.since, opened)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError,
                    sqlite3.Error, subprocess.SubprocessError) as exc:
                unavailable.append(f"{name}: {exc}")
                continue
            active = []
            needs_attention = []
            recent = []
            for key, fact in facts.items():
                if fact.at >= self.since:
                    self.observed = True
                    recent.append((key, fact))
                if opened and fact.at >= launch - 2 and fact.status == "running":
                    active.append((key, fact))
                    self.observed = True
                elif opened and fact.at >= self.since and fact.status == "attention":
                    needs_attention.append((key, fact))
            if opened and (active or queued):
                busy.append((name, len(active) + queued))
            if needs_attention:
                attention.append((name, len(needs_attention)))
            if incomplete:
                unavailable.append(f"{name}: {incomplete} session journal(s) still being read")
            revision.append((name, launch, tuple(sorted(recent)), tuple(sorted(active)), queue_revision))
        for name, source in (task_sources or {}).items():
            try:
                state = source.sample(since=self.since, boot=self.boot)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                unavailable.append(f"{name}: {exc}")
                continue
            if not state.available:
                unavailable.append(f"{name}: {tr('Task source is temporarily unavailable')}")
                continue
            self.observed |= state.observed
            if state.busy:
                busy.append((name, state.busy))
            if state.attention:
                attention.append((name, state.attention))
            revision.append(("task-source", name, state.revision, state.busy, state.attention, state.observed))
        return Snapshot(self.observed, tuple(busy), tuple(attention), tuple(unavailable), tuple(revision))
