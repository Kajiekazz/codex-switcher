"""Current-user Windows startup registration, shared by the tray and dashboard."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CodexSwitcher"


def launch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    return [str(windowless if windowless.exists() else python),
            str(Path(__file__).resolve().parents[1] / "switcher.pyw")]


class StartupRegistration:
    def __init__(self, registry=None, command=None, value_name=VALUE_NAME):
        if registry is None and os.name == "nt":
            import winreg
            registry = winreg
        self.registry = registry
        self.command = command if command is not None else launch_command()
        self.value_name = value_name

    def status(self) -> dict:
        if self.registry is None:
            return {"available": False, "enabled": False, "message": "开机自启仅支持 Windows"}
        try:
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = self.registry.QueryValueEx(key, self.value_name)
            return {"available": True, "enabled": bool(value)}
        except FileNotFoundError:
            return {"available": True, "enabled": False}
        except OSError as exc:
            return {"available": False, "enabled": False, "message": str(exc)}

    def set_enabled(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled 必须是布尔值")
        if self.registry is None:
            raise ValueError("开机自启仅支持 Windows")
        command = subprocess.list2cmdline([*self.command, "--background"])
        if enabled and len(command) > 260:
            raise ValueError("程序路径太长，请将程序安装到较短的路径后再开启自启")
        with self.registry.CreateKeyEx(self.registry.HKEY_CURRENT_USER, RUN_KEY, 0,
                                       self.registry.KEY_SET_VALUE) as key:
            if enabled:
                self.registry.SetValueEx(key, self.value_name, 0, self.registry.REG_SZ, command)
            else:
                try:
                    self.registry.DeleteValue(key, self.value_name)
                except FileNotFoundError:
                    pass
        return self.status()
