"""Clipboard memory belongs to Windows only after a successful handoff."""
import ctypes as c
import unittest
from unittest import mock

from codex_switcher.windows_clipboard import copy_text


class NativeClipboardTests(unittest.TestCase):
    def setUp(self):
        self.user, self.kernel = mock.Mock(), mock.Mock()
        self.user.CreateWindowExW.return_value = 42
        self.user.OpenClipboard.return_value = True
        self.user.EmptyClipboard.return_value = True
        self.user.SetClipboardData.return_value = 77
        self.kernel.GlobalAlloc.return_value = 77
        self.buffer = c.create_string_buffer(128)
        self.kernel.GlobalLock.return_value = c.addressof(self.buffer)
        dll = mock.patch('codex_switcher.windows_clipboard.c.WinDLL', create=True,
                         side_effect=lambda name, **_: self.user if name == 'user32' else self.kernel)
        platform = mock.patch('codex_switcher.windows_clipboard.os.name', 'nt')
        dll.start(); platform.start()
        self.addCleanup(dll.stop); self.addCleanup(platform.stop)

    def test_unicode_and_null_terminator_are_transferred_without_freeing_owned_memory(self):
        self.assertTrue(copy_text('测试 copy'))
        expected = '测试 copy\0'.encode('utf-16-le')
        self.assertEqual(self.buffer.raw[:len(expected)], expected)
        self.user.SetClipboardData.assert_called_once_with(13, 77)
        self.user.CloseClipboard.assert_called_once()
        self.user.DestroyWindow.assert_called_once_with(42)
        self.kernel.GlobalFree.assert_not_called()

    def test_failed_transfer_closes_clipboard_and_frees_memory(self):
        self.user.SetClipboardData.return_value = None
        self.assertFalse(copy_text('fixture'))
        self.kernel.GlobalFree.assert_called_once_with(77)
        self.user.CloseClipboard.assert_called_once()

    def test_busy_clipboard_is_bounded_and_preserves_original_contents(self):
        self.user.OpenClipboard.return_value = False
        with mock.patch('codex_switcher.windows_clipboard.time.sleep'):
            self.assertFalse(copy_text('fixture'))
        self.assertEqual(self.user.OpenClipboard.call_count, 5)
        self.user.EmptyClipboard.assert_not_called()
        self.user.CloseClipboard.assert_not_called()
        self.kernel.GlobalFree.assert_called_once_with(77)

    def test_allocation_failure_still_destroys_owner_window(self):
        self.kernel.GlobalAlloc.return_value = None
        self.assertFalse(copy_text('fixture'))
        self.user.OpenClipboard.assert_not_called()
        self.user.DestroyWindow.assert_called_once_with(42)


if __name__ == '__main__':
    unittest.main()
