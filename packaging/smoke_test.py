"""Verify a native bundle with an empty profile and no developer tools on PATH."""
import argparse
import base64
import ctypes
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request


def pe_architecture(path):
    with Path(path).open("rb") as file:
        assert file.read(2) == b"MZ", path
        file.seek(0x3C)
        offset = struct.unpack("<I", file.read(4))[0]
        file.seek(offset)
        assert file.read(4) == b"PE\0\0", path
        machine = struct.unpack("<H", file.read(2))[0]
    return {0x8664: "x64", 0xAA64: "arm64"}.get(machine, hex(machine))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--arch", choices=["x64", "arm64"], required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve()
    architecture = pe_architecture(binary)
    assert architecture == args.arch, (architecture, args.arch)
    # Both the Python DLL and Playwright driver must match the advertised build.
    assert pe_architecture(binary.parent / "_internal/python314.dll") == args.arch
    assert pe_architecture(binary.parent / "_internal/playwright/driver/node.exe") == args.arch
    decoder = list((binary.parent / "_internal").rglob("zxingcpp*.pyd"))
    imaging = list((binary.parent / "_internal/PIL").glob("_imaging.*.pyd"))
    assert decoder and imaging, "native QR decoder or image runtime missing"
    for library in decoder + imaging:
        assert pe_architecture(library) == args.arch, library
    result = {"architecture": architecture, "developer_tools_on_path": False}
    with tempfile.TemporaryDirectory(prefix="codex-switcher-smoke-") as directory:
        folder = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {**os.environ, "CODEX_SWITCHER_DATA": str(folder / "data"),
               "CODEX_SWITCHER_AUTH": str(folder / "absent-auth.json"),
               "CODEX_SWITCHER_STATE_DB": str(folder / "absent-state.sqlite"),
               "CODEX_SWITCHER_GOALS_DB": str(folder / "absent-goals.sqlite"),
               "CODEX_SWITCHER_QUEUE_DB": str(folder / "absent-queue.sqlite"),
               "CODEX_SWITCHER_PORT": str(port), "CODEX_SWITCHER_HOST": "127.0.0.1",
               "CODEX_SWITCHER_ENABLE_AUTO_SWITCH": "0", "CODEX_SWITCHER_ENABLE_AUTO_RESET": "0",
               "CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH": "0",
               "PATH": str(Path(os.environ["SystemRoot"]) / "System32")}
        base = "http://127.0.0.1:" + str(port)
        flags = subprocess.CREATE_NO_WINDOW
        if not args.headless:
            flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
        def get(path):
            with urllib.request.urlopen(base + path, timeout=3) as response:
                return json.load(response)
        def post(path, body):
            request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        def wait_ready(proc):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if get("/api/health").get("desktop_launcher"):
                        return
                except OSError:
                    pass
                assert proc.poll() is None, "application exited before becoming ready"
                time.sleep(.2)
            raise AssertionError("application did not become ready")
        proc = None
        try:
            diagnose = folder / "diagnose.json"
            subprocess.run([str(binary), "--diagnose", str(diagnose)], env=env, timeout=20,
                           check=True, creationflags=flags)
            diagnostic = json.loads(diagnose.read_text())
            assert diagnostic["frozen"] and diagnostic["dashboard"]
            assert not diagnostic["browser"]["ready"], "browser should not be bundled"
            command = [str(binary), "--background", *(["--no-tray"] if args.headless else [])]
            proc = subprocess.Popen(command, env=env, creationflags=flags)
            wait_ready(proc)
            owner = json.loads((folder / "data/app-instance.json").read_text())["pid"]
            assert owner == proc.pid
            subprocess.run(command, env=env, timeout=20, check=True, creationflags=flags)
            assert json.loads((folder / "data/app-instance.json").read_text())["pid"] == owner
            result["duplicate_reuses_service"] = True
            assert get("/api/setup/status")["show_guide"]
            result["first_run_guide"] = True
            assert get("/api/health")["session_api_version"] >= 5
            with urllib.request.urlopen(base + "/", timeout=10) as response:
                dashboard = response.read().decode("utf-8")
            assert 'id="loginVerificationCode"' in dashboard
            assert 'id="goalResumeMode"' in dashboard
            result["verification_input_and_resume_modes"] = True
            post("/api/setup/progress", {"confirm": True, "step": 2, "browser_mode": "local", "local_selected": False})
            assert get("/api/setup/status")["step"] == 2
            post("/api/accounts/add", {"email": "fixture@example.com"})
            fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/totp-setup/binding.png"
            imported = post("/api/accounts/totp/import", {
                "confirm": True, "email": "fixture@example.com",
                "image": base64.b64encode(fixture.read_bytes()).decode("ascii"),
            })
            assert imported["account"]["has_totp"]
            assert "totp" not in imported["account"]
            assert get("/api/setup/status")["accounts"]["ready"] == 0
            result["native_qr_import"] = True
            result["saved_credentials_not_mistaken_for_authorization"] = True
            if not args.headless:
                user = ctypes.WinDLL("user32", use_last_error=True)
                user.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
                user.FindWindowW.restype = ctypes.c_void_p
                user.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
                hwnd = None
                for _ in range(50):
                    hwnd = user.FindWindowW("CodexSwitcherTray_" + str(owner), None)
                    if hwnd:
                        break
                    time.sleep(.1)
                assert hwnd, "native tray not created"
                user.PostMessageW(hwnd, 0x0010, 0, 0)
                result["native_tray"] = True
            else:
                subprocess.run([str(binary), "--exit"], env=env, timeout=20, check=True, creationflags=flags)
            proc.wait(15)
            assert proc.returncode == 0
            assert not (folder / "data/app-instance.json").exists()
            result["clean_exit"] = True
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait(10)
            if not result.get("clean_exit"):
                log = folder / "data/logs/app.log"
                if log.exists():
                    print(log.read_text(encoding="utf8")[-8000:])
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
