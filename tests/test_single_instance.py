"""Exercise native mutex ownership between isolated Python processes."""

import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from codex_switcher.windows_ui import SingleInstance


CHILD = """
import os
import sys
from codex_switcher.windows_ui import SingleInstance
instance = SingleInstance(sys.argv[1])
print(int(instance.acquired), flush=True)
if sys.argv[2] == "owner":
    command = sys.stdin.readline().strip()
    if command == "abandon":
        os._exit(0)
instance.close()
"""


@unittest.skipUnless(os.name == "nt", "Windows native single-instance mutex")
class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.name = "Local\\CodexSwitcher-test-" + uuid.uuid4().hex
        self.env = {**os.environ, "CODEX_SWITCHER_DATA": self.tmp.name,
                    "CODEX_SWITCHER_AUTH": str(Path(self.tmp.name) / "auth.json"),
                    "CODEX_SWITCHER_ENABLE_SWITCH": "0"}

    def child(self, mode):
        process = subprocess.Popen(
            [sys.executable, "-c", CHILD, self.name, mode],
            cwd=Path(__file__).resolve().parents[1], env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

        self.addCleanup(cleanup)
        messages = queue.Queue()
        threading.Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True).start()
        try:
            line = messages.get(timeout=10).strip()
        except queue.Empty:
            self.fail("Isolated mutex child did not become ready")
        self.assertIn(line, ("0", "1"))
        return process, line == "1"

    def end_owner(self, process, *, abandon=False):
        process.stdin.write("abandon\n" if abandon else "release\n")
        process.stdin.flush()
        self.assertEqual(process.wait(timeout=5), 0)

    def assert_probe_acquires(self, expected):
        process, acquired = self.child("probe")
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(acquired, expected)

    def test_waiter_takes_over_released_owner_without_closing_its_handle(self):
        owner, acquired = self.child("owner")
        self.assertTrue(acquired)
        waiter = SingleInstance(self.name)
        self.addCleanup(waiter.close)
        self.assertFalse(waiter.acquired)
        self.assertFalse(waiter.try_acquire())
        self.end_owner(owner)
        self.assertTrue(waiter.try_acquire())
        self.assertTrue(waiter.try_acquire())
        self.assert_probe_acquires(False)
        waiter.close()
        waiter.close()
        self.assert_probe_acquires(True)

    def test_waiter_takes_over_after_owner_exits_without_cleanup(self):
        owner, acquired = self.child("owner")
        self.assertTrue(acquired)
        waiter = SingleInstance(self.name)
        self.addCleanup(waiter.close)
        self.assertFalse(waiter.acquired)
        self.end_owner(owner, abandon=True)
        self.assertTrue(waiter.try_acquire())
        self.assert_probe_acquires(False)
        waiter.close()
        self.assert_probe_acquires(True)

    def test_closing_non_owner_cannot_release_the_running_instance(self):
        owner, acquired = self.child("owner")
        self.assertTrue(acquired)
        waiter = SingleInstance(self.name)
        self.addCleanup(waiter.close)
        self.assertFalse(waiter.acquired)
        waiter.close()
        self.assert_probe_acquires(False)
        self.end_owner(owner)
        self.assert_probe_acquires(True)


if __name__ == "__main__":
    unittest.main()
