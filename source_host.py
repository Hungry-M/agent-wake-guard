"""Read task snapshots from explicitly installed adapters."""

import re
import subprocess
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from i18n import tr
from plugin_host import PLUGIN_ID, open_plugin
from windows_power import GuardError

SOURCE_SUFFIX = ".source-plugin.exe"
REVISION = re.compile(r"[a-zA-Z0-9_.:-]{1,128}")


@dataclass(frozen=True)
class SourceStatus:
    available: bool
    observed: bool
    busy: int
    attention: int
    revision: str

    @classmethod
    def parse(cls, reply):
        available, observed, busy, attention, revision = (
            reply.get(key) for key in ("available", "observed", "busy", "attention", "revision")
        )
        if (type(available) is not bool or type(observed) is not bool
                or type(busy) is not int or not 0 <= busy <= 1_000_000
                or type(attention) is not int or not 0 <= attention <= 1_000_000
                or not isinstance(revision, str) or not REVISION.fullmatch(revision)):
            raise GuardError(tr("The task source returned an invalid snapshot."))
        return cls(available, observed, busy, attention, revision)


class TaskSource:
    def __init__(self, worker=None, error=None):
        self.worker, self.error = worker, error

    def sample(self, *, since, boot):
        if self.error:
            raise self.error
        try:
            return SourceStatus.parse(self.worker.request("sample", timeout=4, since=since, boot=boot))
        except GuardError as exc:
            # A stale response could arrive after a timeout. Reopen the adapter on the next Guard launch.
            self.error = exc
            raise


@contextmanager
def open_task_sources(executable):
    directory = executable.with_suffix(".sources")
    with ExitStack() as stack:
        sources = {}
        for path in sorted(directory.glob(f"*{SOURCE_SUFFIX}")):
            source_id = path.name.removesuffix(SOURCE_SUFFIX)
            if not PLUGIN_ID.fullmatch(source_id) or not path.is_file():
                continue
            try:
                worker = stack.enter_context(open_plugin(path, None, argument="--source-plugin-v1", exit_timeout=4))
                protocol = worker.request("hello", timeout=4).get("protocol")
                if type(protocol) is not int or protocol != 1:
                    raise GuardError(tr("The task source must support protocol version 1."))
            except (OSError, GuardError, subprocess.SubprocessError) as exc:
                sources[source_id] = TaskSource(error=exc)
            else:
                sources[source_id] = TaskSource(worker=worker)
        yield sources
