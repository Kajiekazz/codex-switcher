"""Native display probes are read-only and preserve uncertain observations."""
import ctypes
import os
import unittest
from unittest import mock

from codex_switcher.windows_processes import (
    DWORD, FILETIME, PROCESSENTRY32W, PROCESS_MEMORY_COUNTERS_EX, UNICODE_STRING,
    WAIT_FAILED, WAIT_TIMEOUT, INVALID_HANDLE_VALUE, _WindowsApi,
    _decode_command_buffer, _started_at, snapshot_processes,
)


def _entry(pid=10, name="codex.exe", threads=4):
    return {"pid": pid, "ppid": 1, "name": name, "threads": threads}


def _observation(pid=10, threads=4, **updates):
    return {**_entry(pid, threads=threads), "exited": False,
            "working_set": 30 * 1024 * 1024, "private_memory": 30 * 1024 * 1024,
            "has_window": None, **updates}


def _command_buffer(text, **updates):
    encoded = text.encode("utf-16-le")
    offset = ctypes.sizeof(UNICODE_STRING)
    buffer = ctypes.create_string_buffer(offset + len(encoded) + 2)
    header = UNICODE_STRING.from_buffer(buffer)
    header.Length, header.MaximumLength = len(encoded), len(encoded) + 2
    header.Buffer = ctypes.addressof(buffer) + offset
    ctypes.memmove(header.Buffer, encoded, len(encoded))
    for name, value in updates.items():
        setattr(header, name, value)
    return buffer


class NativeBufferTests(unittest.TestCase):
    def test_command_unicode_decodes_only_inside_returned_buffer(self):
        buffer = _command_buffer('codex.exe app-server "你好"')
        self.assertEqual(_decode_command_buffer(buffer, ctypes.sizeof(buffer)), 'codex.exe app-server "你好"')

    def test_command_rejects_invalid_lengths_and_pointers(self):
        for updates in ({"Length": 3}, {"MaximumLength": 3}, {"Length": 32},
                        {"Buffer": 1}, {"Buffer": 0}, {"Buffer": 0x7FFFFFFFFFFFFFF0}):
            with self.subTest(updates=updates):
                buffer = _command_buffer("abc", **updates)
                with self.assertRaises(ValueError), mock.patch("ctypes.string_at", side_effect=AssertionError("unsafe read")):
                    _decode_command_buffer(buffer, ctypes.sizeof(buffer))
        buffer = _command_buffer("abc")
        for returned in (0, ctypes.sizeof(UNICODE_STRING) - 1, ctypes.sizeof(buffer) + 1, ctypes.sizeof(buffer) - 2):
            with self.subTest(returned=returned), self.assertRaises(ValueError):
                _decode_command_buffer(buffer, returned)

    def test_empty_command_is_valid_but_not_unknown(self):
        buffer = ctypes.create_string_buffer(ctypes.sizeof(UNICODE_STRING))
        self.assertEqual(_decode_command_buffer(buffer, ctypes.sizeof(buffer)), "")

    def test_filetime_utc_and_invalid_values(self):
        ticks = 116444736000000000
        self.assertEqual(_started_at(FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)), "1970-01-01T00:00:00.000000+00:00")
        self.assertIsNone(_started_at(FILETIME()))
        self.assertIsNone(_started_at(FILETIME(0xFFFFFFFF, 0xFFFFFFFF)))

    @unittest.skipUnless(os.name == "nt", "Windows ABI")
    def test_windows_structure_layouts_follow_pointer_size(self):
        wide = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(ctypes.sizeof(PROCESSENTRY32W), 568 if wide else 556)
        self.assertEqual(ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX), 80 if wide else 44)
        self.assertEqual(ctypes.sizeof(UNICODE_STRING), 16 if wide else 8)
        self.assertEqual(PROCESSENTRY32W.szExeFile.offset, 44 if wide else 36)
        self.assertEqual(ctypes.sizeof(DWORD), 4)


class NativeSnapshotTests(unittest.TestCase):
    def test_only_one_inventory_and_no_window_enumeration_for_healthy_processes(self):
        api = mock.Mock()
        api.entries.return_value = [_entry()]
        api.inspect.return_value = _observation()
        self.assertEqual(snapshot_processes(api=api), [_observation()])
        api.entries.assert_called_once_with({"chatgpt.exe", "codex.exe"})
        api.window_pids.assert_not_called()

    def test_low_memory_single_thread_uses_one_window_enumeration(self):
        api = mock.Mock()
        api.entries.return_value = [_entry(10), _entry(11), _entry(12)]
        api.inspect.side_effect = [_observation(10, name="ChatGPT.exe", threads=1, working_set=1024, private_memory=2048),
                                   _observation(11, name="ChatGPT.exe", threads=1, working_set=1024, private_memory=2048),
                                   _observation(12, name="ChatGPT.exe", threads=1, working_set=1024, private_memory=2048, exited=True)]
        api.window_pids.return_value = {11}
        rows = snapshot_processes(api=api)
        api.window_pids.assert_called_once_with({10, 11})
        self.assertEqual([row["has_window"] for row in rows], [False, True, None])

    def test_window_failure_preserves_unknown_and_does_not_fail_snapshot(self):
        api = mock.Mock()
        api.entries.return_value = [_entry()]
        api.inspect.return_value = _observation(name="ChatGPT.exe", threads=1, working_set=None, private_memory=None)
        api.window_pids.side_effect = OSError("denied")
        self.assertIsNone(snapshot_processes(api=api)[0]["has_window"])

    def test_single_threaded_cli_does_not_enumerate_windows(self):
        api = mock.Mock()
        api.entries.return_value = [_entry()]
        api.inspect.return_value = _observation(threads=1, working_set=1024, private_memory=2048)
        self.assertIsNone(snapshot_processes(api=api)[0]["has_window"])
        api.window_pids.assert_not_called()

    def test_reused_pid_omitted_but_inventory_failure_propagates(self):
        api = mock.Mock()
        api.entries.return_value = [_entry()]
        api.inspect.return_value = None
        self.assertEqual(snapshot_processes(api=api), [])
        api.entries.side_effect = OSError("snapshot unavailable")
        with self.assertRaises(OSError):
            snapshot_processes(api=api)


@unittest.skipUnless(os.name == "nt", "Win32 mock error state")
class NativeApiTests(unittest.TestCase):
    def setUp(self):
        self.api = _WindowsApi.__new__(_WindowsApi)
        self.api.kernel = mock.Mock()
        self.api.ntdll = mock.Mock()
        self.api.kernel.OpenProcess.return_value = 123
        self.api.kernel.WaitForSingleObject.return_value = WAIT_TIMEOUT
        self.api.kernel.QueryFullProcessImageNameW.side_effect = self._image
        self.api.kernel.K32GetProcessMemoryInfo.side_effect = self._memory
        self.api.kernel.GetProcessHandleCount.side_effect = self._handles
        self.api.kernel.GetProcessTimes.return_value = False
        self.api._command = mock.Mock(return_value="codex.exe app-server")
        self.api._package_family = mock.Mock(return_value="OpenAI.Codex_fixture")
        ctypes.set_last_error(5)

    @staticmethod
    def _image(handle, flags, buffer, length):
        buffer.value = r"C:\runtime\codex.exe"
        return True

    @staticmethod
    def _memory(handle, memory, size):
        memory._obj.WorkingSetSize = 1024
        memory._obj.PrivateUsage = 4096
        return True

    @staticmethod
    def _handles(handle, handles):
        handles._obj.value = 7
        return True

    def test_inspection_uses_query_only_rights_closes_handle_and_reads_command(self):
        row = self.api.inspect(_entry())
        self.api.kernel.OpenProcess.assert_called_once_with(0x101000, False, 10)
        self.api.kernel.CloseHandle.assert_called_once_with(123)
        self.assertTrue(row["identity_confirmed"])
        self.assertTrue(row["command_available"])
        self.assertEqual((row["working_set"], row["private_memory"], row["handles"]), (1024, 4096, 7))
        self.assertEqual(row["wait_status"], WAIT_TIMEOUT)
        self.assertFalse(row["exited"])

    def test_open_denied_is_unknown_not_exited_and_no_fake_zero_memory(self):
        self.api.kernel.OpenProcess.return_value = None
        row = self.api.inspect(_entry())
        self.assertFalse(row["exited"])
        self.assertFalse(row["identity_confirmed"])
        self.assertIsNone(row["working_set"])
        self.assertEqual(row["probe_error"], 5)
        self.api.kernel.CloseHandle.assert_not_called()

    def test_only_signaled_handle_is_exited_including_exit_during_queries(self):
        for statuses, expected in (([WAIT_FAILED, WAIT_FAILED], False), ([0], True), ([WAIT_TIMEOUT, 0], True)):
            with self.subTest(statuses=statuses):
                self.api.kernel.WaitForSingleObject.side_effect = statuses
                self.api.kernel.CloseHandle.reset_mock()
                row = self.api.inspect(_entry())
                self.assertEqual(row["exited"], expected)
                self.api.kernel.CloseHandle.assert_called_once_with(123)

    def test_image_mismatch_discards_reused_pid_and_closes_handle(self):
        self.assertIsNone(self.api.inspect(_entry(name="ChatGPT.exe")))
        self.api.kernel.CloseHandle.assert_called_once_with(123)
        self.api.kernel.K32GetProcessMemoryInfo.assert_not_called()

    def test_image_denied_preserves_unknown_identity_and_closes_handle(self):
        self.api.kernel.QueryFullProcessImageNameW.side_effect = None
        self.api.kernel.QueryFullProcessImageNameW.return_value = False
        row = self.api.inspect(_entry())
        self.assertFalse(row["identity_confirmed"])
        self.assertEqual(row["detail_errors"]["image"], 5)
        self.api.kernel.CloseHandle.assert_called_once_with(123)

    def test_optional_command_failure_does_not_turn_unknown_into_cli(self):
        for outcome in (None, ValueError("invalid buffer"), OSError("not supported")):
            with self.subTest(outcome=outcome):
                self.api._command.side_effect = outcome if isinstance(outcome, Exception) else None
                self.api._command.return_value = outcome
                row = self.api.inspect(_entry())
                self.assertFalse(row["command_available"])
                self.assertEqual(row["command"], "")
                self.assertNotIn("kind", row)

    def test_unexpected_query_exception_still_closes_handle(self):
        self.api.kernel.K32GetProcessMemoryInfo.side_effect = RuntimeError("fixture failure")
        with self.assertRaises(RuntimeError):
            self.api.inspect(_entry())
        self.api.kernel.CloseHandle.assert_called_once_with(123)

    def test_inventory_closes_snapshot_on_mid_iteration_failure(self):
        self.api.kernel.CreateToolhelp32Snapshot.return_value = 555
        def first(handle, entry):
            entry._obj.szExeFile = "ChatGPT.exe"
            entry._obj.th32ProcessID = 10
            entry._obj.cntThreads = 2
            return True
        self.api.kernel.Process32FirstW.side_effect = first
        self.api.kernel.Process32NextW.return_value = False
        with self.assertRaises(OSError):
            self.api.entries({"chatgpt.exe"})
        self.api.kernel.CloseHandle.assert_called_once_with(555)

    def test_inventory_end_is_success_and_only_selected_images_returned(self):
        self.api.kernel.CreateToolhelp32Snapshot.return_value = 555
        self.api.kernel.Process32FirstW.return_value = False
        ctypes.set_last_error(18)
        self.assertEqual(self.api.entries({"chatgpt.exe"}), [])
        self.api.kernel.CloseHandle.assert_called_once_with(555)

    def test_invalid_snapshot_not_closed_as_if_valid(self):
        self.api.kernel.CreateToolhelp32Snapshot.return_value = INVALID_HANDLE_VALUE
        with self.assertRaises(OSError):
            self.api.entries({"chatgpt.exe"})
        self.api.kernel.CloseHandle.assert_not_called()

    def test_command_buffer_growth_is_bounded_and_optional(self):
        del self.api._command
        def huge(handle, kind, buffer, size, returned):
            returned._obj.value = 2 * 1024 * 1024
            return -1073741820  # STATUS_INFO_LENGTH_MISMATCH
        self.api.ntdll.NtQueryInformationProcess.side_effect = huge
        self.assertIsNone(self.api._command(123))
        self.api.ntdll.NtQueryInformationProcess.assert_called_once()


if __name__ == "__main__":
    unittest.main()
