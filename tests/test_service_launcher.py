"""Exercise the real Windows launcher with disposable Python services only."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows PowerShell launcher")
class ServiceLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="switcher launcher ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copy2(ROOT / "start.ps1", self.root / "start.ps1")
        shutil.copy2(ROOT / "service-launch.ps1", self.root / "service-launch.ps1")
        self.env = dict(os.environ)
        self.env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + self.env["PATH"]
        self.env["CODEX_SWITCHER_DATA"] = str(self.root / "data")
        self.env["CODEX_SWITCHER_HOST"] = "127.0.0.1"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.env["CODEX_SWITCHER_PORT"] = str(sock.getsockname()[1])

    def _run(self, script):
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", env=self.env, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def _events(self):
        return [json.loads(line) for line in (self.root / "service-launcher.log").read_text(encoding="utf-8-sig").splitlines()]

    def test_stderr_does_not_terminate_start_script_under_output_redirection(self):
        (self.root / "service.py").write_text(
            'import sys,time\nprint("started",flush=True)\n'
            'sys.stderr.write("POST /api/switch 200\\n");sys.stderr.flush()\n'
            'time.sleep(.2)\nprint("completed",flush=True)\n', encoding="utf8",
        )
        r = self._run(f'& "{self.root / "start.ps1"}" 2>&1 | Tee-Object -FilePath "{self.root / "combined.log"}"; Write-Output "LAUNCHER_RETURNED"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LAUNCHER_RETURNED", r.stdout)
        self.assertIn("completed", (self.root / "service.out.log").read_text(encoding="utf8"))
        self.assertIn("POST /api/switch 200", (self.root / "service.err.log").read_text(encoding="utf8"))
        self.assertEqual(self._events()[-1]["exit_code"], 0)

    def test_python_failure_records_exit_code_instead_of_disappearing(self):
        (self.root / "service.py").write_text('import sys\nprint("probe failure",file=sys.stderr)\nsys.exit(7)\n')
        r = self._run(f'& "{self.root / "start.ps1"}"')
        self.assertEqual(self._events()[-1]["exit_code"], 7)
        self.assertIn("code 7", r.stdout)
        self.assertIn("probe failure", (self.root / "service.err.log").read_text(encoding="utf8"))

    def test_service_survives_launcher_exit_and_keeps_its_log_handles(self):
        (self.root / "service.py").write_text(
            'import time\nprint("started",flush=True)\n'
            'time.sleep(1)\nopen("finished.txt","w").write("done")\nprint("completed",flush=True)\n', encoding="utf8",
        )
        script = f'. "{self.root / "service-launch.ps1"}"; $p = Start-CsServiceProcess -PythonPath "{sys.executable}" -ProjectRoot "{self.root}"; Write-Output "PID=$($p.Id)"'
        r = self._run(script)
        self.assertEqual(r.returncode, 0, r.stderr)
        deadline = time.monotonic() + 8
        while not (self.root / "finished.txt").exists() and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertTrue((self.root / "finished.txt").exists(), "child died with the launcher")
        self.assertIn("completed", (self.root / "service.out.log").read_text(encoding="utf8"))


if __name__ == "__main__":
    unittest.main()
