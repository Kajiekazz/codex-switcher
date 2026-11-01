"""Guided "one click" install for the browser extension.

Chrome refuses to install anything silently unless it comes from the Web Store
or is pushed by enterprise policy, and the policy route makes Chrome show
"your browser is managed". So the honest one-click we can offer is:

    copy the unpacked folder path to the clipboard
    + open chrome://extensions
    + tell the user where the extension already lives

The remaining clicks (开发者模式 → 加载已解压的扩展程序 → 粘贴 → 选择文件夹)
have to be made by the user; no API can do them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

EXTENSIONS_URL = "chrome://extensions"

CHROME_RELATIVE_PATHS = (
    r"Google\Chrome\Application\chrome.exe",
    r"Google\Chrome Beta\Application\chrome.exe",
    r"Chromium\Application\chrome.exe",
)

USER_DATA_RELATIVE_PATHS = (
    ("Chrome", r"Google\Chrome\User Data"),
    ("Chrome Beta", r"Google\Chrome Beta\User Data"),
    ("Chromium", r"Chromium\User Data"),
    ("Edge", r"Microsoft\Edge\User Data"),
)


def extension_dir() -> Path:
    """Absolute path of the unpacked extension shipped with this project."""
    return Path(__file__).resolve().parent.parent / "extension"


def read_manifest(path: Path | None = None) -> dict:
    target = (path or extension_dir()) / "manifest.json"
    with open(target, "r", encoding="utf-8") as handle:
        return json.load(handle)


def unpacked_id(path: Path) -> str:
    """Reproduce Chrome's id for an unpacked extension.

    Chrome hashes the absolute path encoded as UTF-16LE with SHA-256 and maps
    the first 16 bytes to "a"-"p" (two letters per byte).
    """
    absolute = str(Path(path).resolve())
    digest = hashlib.sha256(absolute.encode("utf-16-le")).digest()
    letters = "".join(
        chr(ord("a") + nibble)
        for byte in digest[:16]
        for nibble in (byte >> 4, byte & 0x0F)
    )
    return letters


def chrome_binaries() -> list[Path]:
    found: list[Path] = []
    roots = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value))
    for root in roots:
        for relative in CHROME_RELATIVE_PATHS:
            candidate = root / relative
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    return found


def default_chrome() -> Path | None:
    binaries = chrome_binaries()
    return binaries[0] if binaries else None


def user_data_dirs() -> list[tuple[str, Path]]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    base = Path(local)
    result = []
    for label, relative in USER_DATA_RELATIVE_PATHS:
        candidate = base / relative
        if candidate.is_dir():
            result.append((label, candidate))
    return result


def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


# Chrome keeps extension state in ``Secure Preferences`` (an HMAC-protected JSON
# sibling of ``Preferences``). Reading only ``Preferences`` reports "not
# installed" for every extension loaded in a current Chrome — measured on
# Chrome 156: ``Preferences`` -> ``extensions.settings`` is empty (0 entries)
# while ``Secure Preferences`` holds all 25 with our unpacked one among them.
PREFERENCE_FILES = ("Secure Preferences", "Preferences")


def _extension_entries(profile: Path) -> dict:
    """Merge ``extensions.settings`` from Secure Preferences and Preferences."""
    merged: dict = {}
    for name in PREFERENCE_FILES:
        data = _load_json(profile / name)
        entries = (data.get("extensions") or {}).get("settings") or {}
        if not isinstance(entries, dict):
            continue
        for key, value in entries.items():
            if isinstance(value, dict):
                merged.setdefault(key, value)
    return merged


def _manifest_of(directory: str) -> dict:
    if not directory:
        return {}
    return _load_json(Path(directory) / "manifest.json")


def _describe(label: str, profile: Path, ext_id: str, entry: dict) -> dict:
    path = entry.get("path") or ""
    manifest = entry.get("manifest") or {}
    version = (
        ((entry.get("service_worker_registration_info") or {}).get("version"))
        or manifest.get("version")
        or _manifest_of(path).get("version")
        or ""
    )
    reasons = entry.get("disable_reasons") or []
    return {
        "browser": label,
        "profile": profile.name,
        "id": ext_id,
        "location": entry.get("location"),
        "unpacked": entry.get("location") == 4,
        "path": path,
        "version": version,
        "enabled": not reasons,
        "disable_reasons": reasons,
    }


def scan_profiles(ext_id: str, manifest: dict | None = None) -> list[dict]:
    """Find registered (unpacked or packed) copies of the extension in Chrome.

    ``manifest`` (our shipped manifest) enables a second, looser check: the same
    folder loaded from a *different* path gets a different derived id, and that
    still counts as "installed" — the card reports it as ``id_mismatch`` instead
    of claiming nothing is there.
    """
    hits: list[dict] = []
    wanted_name = str((manifest or {}).get("name") or "")
    for label, base in user_data_dirs():
        if not base.is_dir():
            continue
        try:
            profiles = sorted(base.iterdir())
        except OSError:
            continue
        for profile in profiles:
            if not profile.is_dir():
                continue
            entries = _extension_entries(profile)
            if not entries:
                continue
            entry = entries.get(ext_id)
            if isinstance(entry, dict):
                hits.append(_describe(label, profile, ext_id, entry))
                continue
            if not wanted_name:
                continue
            for other_id, other in entries.items():
                if other_id == ext_id or not isinstance(other, dict):
                    continue
                other_manifest = _manifest_of(other.get("path") or "")
                if str(other_manifest.get("name") or "") != wanted_name:
                    continue
                found = _describe(label, profile, other_id, other)
                found["id_mismatch"] = True
                found["expected_id"] = ext_id
                hits.append(found)
                break
    return hits


def copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard write; returns False instead of raising."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell:
        try:
            completed = subprocess.run(
                [powershell, "-NoProfile", "-STA", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                input=text,
                text=True,
                capture_output=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    clip = shutil.which("clip")
    if clip:
        try:
            subprocess.run(
                [clip],
                input=text.encode("utf-16-le"),
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def open_extensions_page(chrome: Path | None = None) -> dict:
    """Deprecated: Chrome 156 drops chrome:// URLs passed on the command line.

    Kept as a documented trap. `chrome.exe chrome://extensions` (also with
    --new-window / --app= / trailing slash, and when forwarded into an already
    running instance) always lands on chrome://newtab/. Measured on
    Chrome 156.0.8078.76: the only routes to the extensions page are the user
    clicking it, a CDP session on a Chrome started with --remote-debugging-port,
    or an enterprise policy install. Callers should not use this.
    """
    binary = chrome or default_chrome()
    return {"opened": False, "chrome": str(binary) if binary else None, "blocked": True}


def status() -> dict:
    """Describe the shipped extension and whether Chrome already loaded it."""
    directory = extension_dir()
    present = (directory / "manifest.json").is_file()
    payload: dict = {
        "ok": True,
        "required_manifest_version": 3,
        "install_url": EXTENSIONS_URL,
        "extension": {
            "present": present,
            "path": str(directory),
            "name": "",
            "version": "",
            "id": "",
        },
        "installed": False,
        "matches": [],
        "browsers": [str(binary) for binary in chrome_binaries()],
    }
    if not present:
        payload["message"] = "找不到 extension/ 目录（manifest.json 缺失）"
        return payload
    try:
        manifest = read_manifest(directory)
    except (OSError, ValueError) as exc:
        payload["ok"] = False
        payload["message"] = f"manifest.json 解析失败：{exc}"
        return payload
    ext_id = unpacked_id(directory)
    payload["extension"].update(
        {
            "name": manifest.get("name") or "",
            "version": manifest.get("version") or "",
            "id": ext_id,
            "min_chrome": manifest.get("minimum_chrome_version") or "",
        }
    )
    matches = scan_profiles(ext_id, manifest)
    payload["matches"] = matches
    payload["installed"] = bool(matches)
    payload["preference_files"] = list(PREFERENCE_FILES)
    disabled = [item for item in matches if not item.get("enabled")]
    mismatch = [item for item in matches if item.get("id_mismatch")]
    payload["disabled_matches"] = len(disabled)
    payload["id_mismatch"] = bool(mismatch)
    if matches:
        labels = ", ".join(
            f"{item['browser']}/{item['profile']}"
            + ("" if item.get("enabled") else "（已停用）")
            for item in matches
        )
        payload["message"] = "已安装：" + labels
        if mismatch:
            payload["message"] += " · 注意：加载的是另一个副本目录（id 不同），换路径重新加载可消除警告"
        if disabled:
            payload["message"] += " · 扩展已被停用，请在 chrome://extensions 重新启用"
    else:
        payload["message"] = "未检测到安装记录"
    return payload


def install(copy: bool = True, target: str = "path") -> dict:
    """Prepare what can be automated: put the path (or the page URL) on the
    clipboard and hand back the exact clicks that remain.

    Chrome cannot be told to open chrome://extensions from outside (see
    open_extensions_page), so there is deliberately no browser launch here.
    """
    payload = status()
    if not payload["ok"] or not payload["extension"]["present"]:
        return payload
    path = payload["extension"]["path"]
    copy_path = target != "url"
    text = path if copy_path else EXTENSIONS_URL
    payload["copy_target"] = "path" if copy_path else "url"
    payload["clipboard_text"] = text
    payload["clipboard"] = copy_to_clipboard(text) if copy else False
    payload["browser_opened"] = False
    payload["browser_blocked"] = True
    if copy_path:
        payload["steps"] = [
            "1. 点地址栏右侧的拼图图标 🧩 →「管理扩展程序」"
            "（或在地址栏输入 chrome://extensions）",
            "2. 打开右上角「开发者模式」",
            "3. 点「加载已解压的扩展程序」",
            "4. 在对话框里 Ctrl+V 粘贴路径 → 回车 →「选择文件夹」",
        ]
    else:
        payload["steps"] = [
            "1. 在地址栏 Ctrl+V 粘贴 chrome://extensions → 回车",
            "2. 打开右上角「开发者模式」 →「加载已解压的扩展程序」",
            "3. 选中目录（详见卡片上的「目录」）",
        ]
    return payload
