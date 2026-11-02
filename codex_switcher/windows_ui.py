"""Small native Windows surfaces; no extra GUI runtime or PowerShell host."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os


def message_box(text, title="Codex Switcher"):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, str(text), title, 0x10)


def select_executable() -> str | None:
    if os.name != "nt":
        raise ValueError("请选择 Windows 上的 codex.exe")

    class OpenFileName(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("owner", w.HWND), ("instance", w.HINSTANCE),
                    ("filter", w.LPCWSTR), ("custom", w.LPWSTR), ("max_custom", w.DWORD),
                    ("index", w.DWORD), ("file", w.LPWSTR), ("max_file", w.DWORD),
                    ("file_title", w.LPWSTR), ("max_title", w.DWORD),
                    ("initial_dir", w.LPCWSTR), ("title", w.LPCWSTR), ("flags", w.DWORD),
                    ("offset", w.WORD), ("extension", w.WORD), ("default_ext", w.LPCWSTR),
                    ("data", w.LPARAM), ("hook", ctypes.c_void_p), ("template", w.LPCWSTR),
                    ("reserved", ctypes.c_void_p), ("reserved_dword", w.DWORD), ("flags_ex", w.DWORD)]

    buffer = ctypes.create_unicode_buffer(32768)
    dialog = OpenFileName(size=ctypes.sizeof(OpenFileName),
                          filter="Codex CLI (codex.exe)\0codex.exe\0\0",
                          file=ctypes.cast(buffer, w.LPWSTR), max_file=len(buffer),
                          title="选择 codex.exe", flags=0x00001000 | 0x00000800 | 0x00080000)
    library = ctypes.WinDLL("comdlg32", use_last_error=True)
    library.GetOpenFileNameW.argtypes = [ctypes.POINTER(OpenFileName)]
    if library.GetOpenFileNameW(ctypes.byref(dialog)):
        return buffer.value
    code = library.CommDlgExtendedError()
    if code:
        raise OSError(f"无法打开文件选择器 ({code})")
    return None


class SingleInstance:
    def __init__(self, name):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
        self.kernel.CreateMutexW.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateMutexW(None, False, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.acquired = ctypes.get_last_error() != 183

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class TrayIcon:
    def __init__(self, open_dashboard, startup, exit_app, stop_event, icon_path=None):
        self.open_dashboard, self.startup, self.exit_app = open_dashboard, startup, exit_app
        self.stop_event, self.icon_path = stop_event, icon_path
        self.hwnd = None
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure()

    def _configure(self):
        u = self.user
        u.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u.DefWindowProcW.restype = w.LPARAM
        u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND,
                                     w.HMENU, w.HINSTANCE, ctypes.c_void_p]
        u.CreateWindowExW.restype = w.HWND
        u.DestroyWindow.argtypes = [w.HWND]
        u.SetForegroundWindow.argtypes = [w.HWND]
        u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u.LoadIconW.argtypes = [w.HINSTANCE, ctypes.c_void_p]
        u.LoadIconW.restype = w.HICON
        u.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
        u.LoadImageW.restype = w.HANDLE
        u.DestroyIcon.argtypes = [w.HICON]
        u.CreatePopupMenu.restype = w.HMENU
        u.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
        u.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, ctypes.c_void_p]
        u.DestroyMenu.argtypes = [w.HMENU]
        u.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, ctypes.c_void_p]
        self.kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        self.kernel.GetModuleHandleW.restype = w.HMODULE
        self.shell.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.c_void_p]

    def _menu(self):
        menu = self.user.CreatePopupMenu()
        try:
            startup = self.startup.status()
            self.user.AppendMenuW(menu, 0, 1, "打开管理页")
            self.user.AppendMenuW(menu, (8 if startup["enabled"] else 0) |
                                  (1 if not startup["available"] else 0), 2, "开机自启")
            self.user.AppendMenuW(menu, 0x800, 0, None)
            self.user.AppendMenuW(menu, 0, 3, "退出 Switcher")
            point = w.POINT()
            self.user.GetCursorPos(ctypes.byref(point))
            self.user.SetForegroundWindow(self.hwnd)
            selected = self.user.TrackPopupMenu(menu, 0x102, point.x, point.y, 0, self.hwnd, None)
            self.user.PostMessageW(self.hwnd, 0, 0, 0)
            if selected == 1:
                self.open_dashboard()
            elif selected == 2:
                self.startup.set_enabled(not startup["enabled"])
            elif selected == 3:
                self.user.DestroyWindow(self.hwnd)
        except Exception as exc:
            message_box(exc)
        finally:
            self.user.DestroyMenu(menu)

    def run(self):
        callback_type = ctypes.WINFUNCTYPE(w.LPARAM, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [("style", w.UINT), ("proc", callback_type), ("class_extra", ctypes.c_int),
                        ("window_extra", ctypes.c_int), ("instance", w.HINSTANCE), ("icon", w.HICON),
                        ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]

        class NotifyIconData(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("hwnd", w.HWND), ("id", w.UINT), ("flags", w.UINT),
                        ("callback", w.UINT), ("icon", w.HICON), ("tip", w.WCHAR * 128),
                        ("state", w.DWORD), ("state_mask", w.DWORD), ("info", w.WCHAR * 256),
                        ("version", w.UINT), ("title", w.WCHAR * 64), ("info_flags", w.DWORD),
                        ("guid", ctypes.c_byte * 16), ("balloon", w.HICON)]

        taskbar_created = self.user.RegisterWindowMessageW("TaskbarCreated")

        def window_proc(hwnd, msg, wp, lp):
            if msg == 0x8001:
                if lp == 0x0203:
                    self.open_dashboard()
                elif lp in (0x0205, 0x007B):
                    self._menu()
                return 0
            if msg == taskbar_created and hasattr(self, "notification"):
                self.shell.Shell_NotifyIconW(0, ctypes.byref(self.notification))
                return 0
            if msg == 0x0113 and self.stop_event.is_set():
                self.user.DestroyWindow(hwnd)
                return 0
            if msg == 0x0002:
                self.exit_app()
                self.user.PostQuitMessage(0)
                return 0
            return self.user.DefWindowProcW(hwnd, msg, wp, lp)

        self.callback = callback_type(window_proc)
        instance = self.kernel.GetModuleHandleW(None)
        name = "CodexSwitcherTray_" + str(os.getpid())
        window_class = WindowClass(proc=self.callback, instance=instance, name=name)
        self.user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        if not self.user.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = self.user.CreateWindowExW(0, name, "Codex Switcher", 0,
                                               0, 0, 0, 0, None, None, instance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        custom_icon = self.user.LoadImageW(None, str(self.icon_path), 1, 0, 0, 0x10) if self.icon_path else None
        icon = custom_icon or self.user.LoadIconW(None, ctypes.c_void_p(32512))
        self.notification = NotifyIconData(size=ctypes.sizeof(NotifyIconData), hwnd=self.hwnd,
                                          id=1, flags=7, callback=0x8001, icon=icon,
                                          tip="Codex Switcher · 双击打开管理页")
        if not self.shell.Shell_NotifyIconW(0, ctypes.byref(self.notification)):
            self.user.DestroyWindow(self.hwnd)
            raise OSError("无法创建托盘图标")
        self.user.SetTimer(self.hwnd, 1, 500, None)
        message = w.MSG()
        try:
            while True:
                result = self.user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.user.TranslateMessage(ctypes.byref(message))
                self.user.DispatchMessageW(ctypes.byref(message))
        finally:
            self.shell.Shell_NotifyIconW(2, ctypes.byref(self.notification))
            if custom_icon:
                self.user.DestroyIcon(custom_icon)
            self.hwnd = None
