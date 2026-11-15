"""Incremental rollout evidence with synthetic logs, never real chats."""
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from codex_switcher import execution


def event(kind, turn="turn-1", *, stamp="2026-11-15T12:00:00Z", **fields):
    return (json.dumps({"type": "event_msg", "timestamp": stamp,
                       "payload": {"type": kind, "turn_id": turn, **fields}}) + "\n").encode()


def output(size=4096):
    return (json.dumps({"type": "response_item", "payload": {
        "type": "function_call_output", "output": "synthetic tool output " + "x" * size}}) + "\n").encode()


class CountingReader:
    def __init__(self, stream, counts):
        self.stream, self.counts = stream, counts

    def read(self, *args):
        data = self.stream.read(*args)
        self.counts.append(len(data))
        return data

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()


class ExecutionPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "rollout.jsonl"
        with execution._lock:
            execution._cache.clear()

    def append(self, data):
        with self.path.open("ab") as stream:
            stream.write(data)

    def state(self):
        return execution._rollout_state(self.path)

    def count_reads(self, counts):
        original = Path.open

        def opened(path, mode="r", *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            return CountingReader(stream, counts) if path == self.path and mode == "rb" else stream

        return mock.patch.object(Path, "open", opened)

    def test_append_reads_only_new_bytes_and_preserves_turn_evidence(self):
        self.path.write_bytes(event("task_started", turn_attribution={"turn_trigger": "queue"}) + output() * 800)
        initial = self.state()
        self.assertEqual((initial["state"], initial["trigger"]), ("running", "queue"))
        addition = output(512)
        self.append(addition)
        reads = []
        with self.count_reads(reads):
            updated = self.state()
        self.assertEqual(updated, initial)
        self.assertLessEqual(sum(reads), len(addition) + 1024)
        self.assertGreater(self.path.stat().st_size, 3 * 1024 * 1024)
        with mock.patch.object(Path, "open", side_effect=AssertionError("unchanged file reopened")):
            self.assertEqual(self.state(), initial)
        updated["state"] = "caller modification"
        self.assertEqual(self.state()["state"], "running")

    def test_incremental_errors_endings_and_new_turn_reset_match_execution_semantics(self):
        self.path.write_bytes(event("task_started", turn_attribution={"turn_trigger": "composer"}))
        self.state()
        self.append(event("error", message="Error running remote compact task: Selected model is at capacity. Please try a different model."))
        self.assertTrue(self.state()["capacity_limited"])
        self.append(event("task_completed", stamp="2026-11-15T12:01:00Z",
                          error={"codex_error_info": "usage_limit_reached"}))
        ended = self.state()
        self.assertEqual((ended["state"], ended["turn_id"], ended["at"]),
                         ("ended", "turn-1", "2026-11-15T12:01:00Z"))
        self.assertTrue(ended["capacity_limited"] and ended["usage_limited"])
        self.append(event("task_started", "turn-2", stamp="2026-11-15T12:02:00Z",
                          turn_attribution={"turn_trigger": "goal"}))
        running = self.state()
        self.assertEqual((running["state"], running["turn_id"], running["trigger"]), ("running", "turn-2", "goal"))
        self.assertFalse(running["capacity_limited"] or running["usage_limited"])
        self.append(event("turn_aborted", "turn-2"))
        self.assertEqual(self.state()["state"], "interrupted")

    def test_partial_lines_are_retried_and_complete_json_without_newline_remains_visible(self):
        self.path.write_bytes(event("task_started"))
        self.state()
        ending = event("task_complete", error={"message": "quota exceeded"})
        self.append(ending[:len(ending) // 2])
        self.assertEqual(self.state()["state"], "running")
        self.append(ending[len(ending) // 2:-1])
        self.assertEqual(self.state()["state"], "ended")
        self.assertTrue(self.state()["usage_limited"])
        self.append(b"\n" + event("task_started", "turn-2"))
        self.assertEqual(self.state()["turn_id"], "turn-2")
        self.assertFalse(self.state()["usage_limited"])

    def test_truncation_and_growing_rewrite_discard_previous_turn_errors(self):
        self.path.write_bytes(event("task_started") + event("error", message="usage limit") + output())
        self.assertTrue(self.state()["usage_limited"])
        self.path.write_bytes(event("task_started", "replacement"))
        self.assertEqual(self.state()["turn_id"], "replacement")
        self.assertFalse(self.state()["usage_limited"])
        # Larger content on the same inode is not necessarily an append.
        self.path.write_bytes(event("task_started", "rewritten") + output(8192))
        self.assertEqual(self.state()["turn_id"], "rewritten")

    def test_same_size_rewrite_and_file_replacement_are_not_cached_as_unchanged(self):
        original, replacement = event("task_started", "turn-1"), event("task_started", "turn-2")
        self.assertEqual(len(original), len(replacement))
        self.path.write_bytes(original)
        self.state()
        before = self.path.stat()
        self.path.write_bytes(replacement)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        self.assertEqual(self.state()["turn_id"], "turn-2")
        before = self.path.stat()
        other = self.root / "replacement.jsonl"
        other.write_bytes(original)
        os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(other, self.path)
        self.assertEqual(self.state()["turn_id"], "turn-1")

    def test_boundary_rewrite_is_detected_even_if_the_prefix_is_unchanged(self):
        prefix = output(1024)
        self.path.write_bytes(prefix + event("task_started", "turn-1"))
        self.state()
        self.path.write_bytes(prefix + event("task_started", "turn-2") + output(1024))
        self.assertEqual(self.state()["turn_id"], "turn-2")

    def test_large_append_uses_bounded_tail_fallback(self):
        self.path.write_bytes(event("task_started"))
        self.state()
        self.append(output(execution.MAX_TAIL_BYTES + 1000) + event("task_complete"))
        reads = []
        with self.count_reads(reads):
            result = self.state()
        self.assertEqual(result["state"], "ended")
        self.assertLessEqual(sum(reads), execution.MAX_TAIL_BYTES + 1024)

    def test_known_turn_stays_known_when_normal_appends_grow_beyond_the_tail_window(self):
        self.path.write_bytes(event("task_started"))
        self.state()
        addition = output(300_000)
        for _ in range(30):
            self.append(addition)
            self.assertEqual(self.state()["state"], "running")
        self.assertGreater(self.path.stat().st_size, execution.MAX_TAIL_BYTES)
        self.append(event("task_complete", error={"codex_error_info": "server_overloaded"}))
        self.assertTrue(self.state()["capacity_limited"])

    def test_simultaneous_reads_scan_the_same_file_once(self):
        self.path.write_bytes(event("task_started") + output() * 100)
        barrier = threading.Barrier(8)

        def read():
            barrier.wait(3)
            return self.state()

        with mock.patch.object(execution, "_tail_state", wraps=execution._tail_state) as scan:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: read(), range(8)))
        scan.assert_called_once()
        self.assertTrue(all(result == results[0] for result in results))

    def test_cache_is_bounded_and_missing_files_drop_stale_evidence(self):
        for index in range(260):
            path = self.root / f"fixture-{index}.jsonl"
            path.write_bytes(event("task_started"))
            execution._rollout_state(path)
        self.assertEqual(len(execution._cache), 256)
        path.unlink()
        self.assertEqual(execution._rollout_state(path), {"state": "unknown"})
        self.assertNotIn(str(path), execution._cache)


if __name__ == "__main__":
    unittest.main()
