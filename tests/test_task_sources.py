from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from activity import Monitor
from guard import Countdown
from source_host import TaskSource
from windows_power import GuardError


def sample(**fields):
    return {"available": True, "observed": False, "busy": 0, "attention": 0, "revision": "0", **fields}


class TaskSourceTests(TestCase):
    def test_task_source_arms_and_blocks_the_countdown_like_a_builtin(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            monitor = Monitor(root / "codex", root / "dsh", 0, 10)
            worker = Mock()
            worker.request.side_effect = [sample(), sample(observed=True, busy=2, revision="1"),
                                          sample(observed=True, revision="2"), sample(observed=True, revision="2"),
                                          sample(available=False, revision="3")]
            sources = {"example": TaskSource(worker)}
            deadline = Countdown(3)
            self.assertIsNone(deadline.remaining(monitor.sample({}, sources), 10, 10))
            self.assertIsNone(deadline.remaining(monitor.sample({}, sources), 11, 11))
            self.assertEqual(deadline.remaining(monitor.sample({}, sources), 12, 12), 3)
            self.assertEqual(deadline.remaining(monitor.sample({}, sources), 15, 15), 0)
            self.assertIsNone(deadline.remaining(monitor.sample({}, sources), 16, 16))
            worker.request.assert_called_with("sample", timeout=4, since=10, boot=0)

    def test_invalid_protocol_snapshot_remains_unavailable(self):
        worker = Mock()
        worker.request.return_value = sample(busy=True)
        source = TaskSource(worker)
        for _ in range(2):
            with self.assertRaises(GuardError):
                source.sample(since=10, boot=0)
        worker.request.assert_called_once()
