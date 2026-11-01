"""Account switching engine.

Every switch is deliberately boring and reversible:

1. The target auth.json snapshot is decrypted from the DPAPI store.
2. The current live auth.json is snapshotted back into the store (so the
   previous account is itself a switch target afterwards).
3. The live auth.json is backed up to an encrypted, timestamped file.
4. The target bytes are written with a same-directory temp file + os.replace.
5. The new file is re-read and verified by SHA-256 + email.
6. On any verification failure the backup is restored automatically.

Switching is opt-in through CODEX_SWITCHER_ENABLE_SWITCH=1. The engine never
returns tokens or passwords; only display claims and hashes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, dpapi
from .auth_state import read_auth_state, read_auth_state_bytes
from .store import AccountStore


class SwitchError(Exception):
    def __init__(self, code: str, message: str, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


@dataclass
class SwitchResult:
    previous_email: str | None
    previous_plan: str | None
    previous_sha256: str | None
    target_email: str
    target_plan: str | None
    target_account_id: str | None
    target_expires_at: int | None
    target_expired: bool
    target_sha256: str
    backup_path: str | None
    switched_at: str
    desktop_restart: dict | None = None

    def to_public(self) -> dict:
        return {
            "previous_email": self.previous_email,
            "previous_plan": self.previous_plan,
            "previous_sha256": self.previous_sha256,
            "target_email": self.target_email,
            "target_plan": self.target_plan,
            "target_account_id": self.target_account_id,
            "target_expires_at": self.target_expires_at,
            "target_expired": self.target_expired,
            "target_sha256": self.target_sha256,
            "backup_path": self.backup_path,
            "switched_at": self.switched_at,
            "desktop_restart": self.desktop_restart,
            "warning": (
                "目标快照里的 token 已过期。切换后 Codex 需要先用 refresh token "
                "刷新；如果刷新失败，请切回原账号或重新登录。"
                if self.target_expired
                else None
            ),
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    """Write bytes atomically on the same volume (Windows-safe)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


def backup_dir() -> Path:
    return config.backups_dir()


def create_backup(
    auth_path: Path, target_dir: Path | None = None
) -> tuple[Path | None, str | None]:
    """Encrypt the live auth.json into a timestamped backup file.

    Returns (backup_path, sha256). backup_path is None when auth.json does not
    exist yet (first login); the rollback then removes the newly created file.
    """
    auth_path = Path(auth_path)
    if not auth_path.exists():
        return None, None
    data = auth_path.read_bytes()
    digest = _sha256(data)
    state = read_auth_state_bytes(data)
    target_dir = Path(target_dir or backup_dir())
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = target_dir / f"auth-{stamp}-{digest[:12]}.bak.json"
    envelope = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": digest,
        "email": state.get("email"),
        "plan": state.get("plan"),
        "account_id": state.get("account_id"),
        "ciphertext": base64.b64encode(dpapi.protect(data)).decode("ascii"),
    }
    path.write_text(
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return path, digest


def read_backup(path: Path) -> bytes:
    """Decrypt and integrity-check a backup created by create_backup()."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise SwitchError("backup_invalid", "不支持的备份格式")
    ciphertext = base64.b64decode(payload["ciphertext"])
    data = dpapi.unprotect(ciphertext)
    expected = payload.get("sha256")
    if expected and _sha256(data) != expected:
        raise SwitchError("backup_corrupt", "备份校验失败，内容可能已损坏")
    return data


def restore_backup(backup_path: Path, auth_path: Path) -> dict:
    """Restore a backup into the live auth file, then verify it."""
    data = read_backup(backup_path)
    _write_atomic(auth_path, data)
    state = read_auth_state(auth_path)
    if state.get("file_sha256") != _sha256(data):
        raise SwitchError("restore_verify_failed", "恢复后 auth.json 校验失败")
    return state


class SwitchManager:
    def __init__(
        self,
        store: AccountStore,
        auth_path: Path,
        target_backup_dir: Path | None = None,
        enabled=None,
        desktop_client=None,
        on_switched=None,
        defer_restart=None,
    ):
        self.store = store
        self.auth_path = Path(auth_path)
        self.backup_dir = Path(target_backup_dir or config.backups_dir())
        self._enabled = enabled or config.switching_enabled
        self.desktop_client = desktop_client
        self.on_switched = on_switched
        # Returns True when a post-switch goal resume is about to run: the
        # resume needs the thread writer lock the desktop holds, so restarting
        # the desktop here only to kill it again seconds later is wasted work.
        self.defer_restart = defer_restart
        self._lock = threading.Lock()

    def enabled(self) -> bool:
        return bool(self._enabled())

    def status(self) -> dict:
        enabled = self.enabled()
        return {
            "implemented": config.SWITCHING_IMPLEMENTED,
            "enabled": enabled,
            "block_reason": None if enabled else config.SWITCH_BLOCK_REASON,
            "message": None if enabled else config.SWITCH_BLOCK_MESSAGE,
            "backup_dir": str(self.backup_dir),
        }

    def current(self) -> dict:
        return read_auth_state(self.auth_path)

    def list_targets(self) -> list[dict]:
        current = self.current()
        current_email = (
            current.get("email") if current.get("logged_in") else None
        )
        snapshots = self.store.list_auth_snapshots()
        for snapshot in snapshots:
            snapshot["is_current"] = bool(
                current_email
                and snapshot["email"].lower() == current_email.lower()
            )
        return snapshots

    def _snapshot_current(self) -> dict | None:
        if not self.auth_path.exists():
            return None
        data = self.auth_path.read_bytes()
        state = read_auth_state_bytes(data)
        if not state.get("logged_in") or not state.get("email"):
            return None
        self.store.save_auth_snapshot(
            state["email"],
            data,
            {
                "email": state.get("email"),
                "name": state.get("name"),
                "plan": state.get("plan"),
                "account_id": state.get("account_id"),
                "expires_at": state.get("expires_at"),
            },
        )
        return state

    def _rollback(self, backup_path: Path | None, previous_sha: str | None) -> bool:
        try:
            if backup_path is not None and Path(backup_path).exists():
                restore_backup(Path(backup_path), self.auth_path)
                if previous_sha is None:
                    return True
                restored = read_auth_state(self.auth_path)
                return restored.get("file_sha256") == previous_sha
            # There was no live auth before the switch; remove the new file.
            try:
                self.auth_path.unlink()
            except FileNotFoundError:
                pass
            return True
        except Exception:
            return False

    def switch_to(
        self,
        target_email: str,
        confirm: bool = False,
        restart_desktop: bool | None = None,
    ) -> SwitchResult:
        if not self.enabled():
            raise SwitchError(
                "switching_disabled",
                config.SWITCH_BLOCK_MESSAGE,
                config.SWITCH_BLOCK_REASON,
            )
        if not confirm:
            raise SwitchError(
                "confirmation_required",
                "切换账号会替换 ~/.codex/auth.json，必须显式确认。",
            )
        target_email = (target_email or "").strip()
        if not target_email:
            raise SwitchError("email_required", "email 不能为空")

        with self._lock:
            current = self.current()
            current_email = current.get("email") if current.get("logged_in") else None
            if (
                current_email
                and current_email.lower() == target_email.lower()
            ):
                raise SwitchError(
                    "already_active",
                    f"{target_email} 已经是当前账号，无需切换。",
                )

            target_bytes = self.store.get_auth_snapshot(target_email)
            if target_bytes is None:
                raise SwitchError(
                    "snapshot_not_found",
                    f"账号池里没有 {target_email} 的可切换 auth 快照。",
                )
            target_state = read_auth_state_bytes(target_bytes)
            if not target_state.get("logged_in"):
                raise SwitchError(
                    "target_snapshot_invalid",
                    "目标快照不是有效的 auth.json。",
                    target_state.get("error") or target_state.get("reason"),
                )
            snapshot_email = target_state.get("email")
            if not snapshot_email:
                raise SwitchError(
                    "target_snapshot_invalid",
                    "目标快照缺少邮箱信息，不能按账号切换。",
                )
            if snapshot_email.lower() != target_email.lower():
                raise SwitchError(
                    "snapshot_email_mismatch",
                    f"目标快照属于 {snapshot_email}，不是 {target_email}。",
                )

            # Make the outgoing account switchable before touching the live file.
            if current_email:
                try:
                    self._snapshot_current()
                except Exception as exc:
                    raise SwitchError(
                        "current_snapshot_failed",
                        "保存当前账号快照失败，已中止切换，live auth 未改动。",
                        str(exc),
                    ) from exc

            try:
                backup_path, previous_sha = create_backup(
                    self.auth_path, self.backup_dir
                )
            except Exception as exc:
                raise SwitchError(
                    "backup_failed",
                    "创建 auth.json 备份失败，已中止切换，live auth 未改动。",
                    str(exc),
                ) from exc

            try:
                _write_atomic(self.auth_path, target_bytes)
            except Exception as exc:
                rolled_back = self._rollback(backup_path, previous_sha)
                raise SwitchError(
                    "write_failed",
                    "写入 auth.json 失败。"
                    + ("已自动回滚。" if rolled_back else "回滚失败，请用备份手动恢复。"),
                    f"{exc}; backup={backup_path}",
                ) from exc

            verified = read_auth_state(self.auth_path)
            expected_sha = _sha256(target_bytes)
            verified_email = verified.get("email")
            if (
                not verified.get("logged_in")
                or verified.get("file_sha256") != expected_sha
                or not verified_email
                or verified_email.lower() != target_email.lower()
            ):
                rolled_back = self._rollback(backup_path, previous_sha)
                raise SwitchError(
                    "verify_failed",
                    "切换后校验失败。"
                    + ("已自动回滚。" if rolled_back else "回滚失败，请用备份手动恢复。"),
                    f"backup={backup_path}; expected_sha={expected_sha}; "
                    f"actual_sha={verified.get('file_sha256')}; "
                    f"email={verified_email}",
                )

            if current_email:
                self.store.set_status(current_email, "inactive")
            self.store.set_status(target_email, "active")

            desktop_restart = self._maybe_restart_desktop(
                restart_desktop, deferred=self._should_defer_restart(restart_desktop)
            )

            result = SwitchResult(
                previous_email=current_email,
                previous_plan=current.get("plan"),
                previous_sha256=previous_sha,
                target_email=target_email,
                target_plan=target_state.get("plan"),
                target_account_id=target_state.get("account_id"),
                target_expires_at=target_state.get("expires_at"),
                target_expired=bool(target_state.get("expired")),
                target_sha256=expected_sha,
                backup_path=str(backup_path) if backup_path else None,
                switched_at=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                desktop_restart=desktop_restart,
            )
            # Post-switch hook (auto resume of pre-selected goals). It must never
            # turn a successful switch into a failure.
            if self.on_switched is not None:
                try:
                    self.on_switched(result)
                except Exception:
                    pass
            return result

    def _should_defer_restart(self, requested: bool | None) -> bool:
        """True when a queued goal resume will need the thread lock anyway."""
        if self.desktop_client is None or requested is False:
            return False
        if self.defer_restart is None:
            return False
        try:
            return bool(self.defer_restart())
        except Exception:  # noqa: BLE001 - a probe failure must not block a switch
            return False

    def _maybe_restart_desktop(
        self, requested: bool | None, deferred: bool = False
    ) -> dict | None:
        """Restart the desktop app after a switch; never raise into the caller."""
        if self.desktop_client is None:
            return None
        if requested is False:
            return {
                "ok": True,
                "skipped": True,
                "reason": "not_requested",
                "message": "本次切换未要求重启桌面端。",
            }
        if deferred:
            # Close instead of restart: the resume job runs headless and would
            # otherwise have to kill the freshly restarted app for its lock.
            # The job reopens the app when it finishes (see goal_resume).
            try:
                closed = self.desktop_client.close_app(force=True)
            except Exception as exc:  # noqa: BLE001 - switch already succeeded
                return {
                    "ok": False,
                    "skipped": False,
                    "reason": "close_error",
                    "error": str(exc)[:300],
                    "message": "auth.json 已切换，但关闭桌面端失败，续跑可能撞上写入锁。",
                }
            return {
                "ok": True,
                "skipped": False,
                "deferred": True,
                "reason": "deferred_to_goal_resume",
                "previous_pids": (closed or {}).get("previous_pids") or [],
                "killed": (closed or {}).get("killed") or [],
                "message": "切号后要续跑：桌面端已关闭，续跑结束后自动重开。",
            }
        try:
            return self.desktop_client.restart(force=bool(requested))
        except Exception as exc:  # noqa: BLE001 - switch already succeeded
            return {
                "ok": False,
                "skipped": False,
                "reason": "restart_error",
                "error": str(exc)[:300],
                "message": "auth.json 已切换，但重启桌面端失败，请手动重启 ChatGPT。",
            }
