"""Encrypted account store backed by SQLite + Windows DPAPI.

Besides the encrypted credentials the store keeps non-secret metadata used for
account management: ``tags``, ``note``, ``priority`` (higher wins when
auto-switch picks a candidate) and ``disabled`` (kept in the pool but skipped by
auto-switch). It also keeps a small quota-sample history so the dashboard can
draw burn-rate curves and predict exhaustion.

Secrets never leave this module in plaintext: ``secret_blob`` is encrypted with
Windows DPAPI and only :meth:`AccountStore.get_secret` decrypts it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import dpapi
from .accounts import mask_email


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    email_masked TEXT NOT NULL,
    secret_blob BLOB NOT NULL,
    has_password INTEGER NOT NULL DEFAULT 0,
    has_totp INTEGER NOT NULL DEFAULT 0,
    totp_format TEXT NOT NULL DEFAULT 'none',
    status TEXT NOT NULL DEFAULT 'unknown',
    last_login_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);

CREATE TABLE IF NOT EXISTS auth_snapshots (
    email TEXT PRIMARY KEY,
    auth_blob BLOB NOT NULL,
    auth_email TEXT,
    auth_plan TEXT,
    auth_account_id TEXT,
    auth_expires_at INTEGER,
    saved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    plan TEXT,
    primary_used REAL,
    primary_reset_at INTEGER,
    primary_window_seconds INTEGER,
    secondary_used REAL,
    secondary_reset_at INTEGER,
    secondary_window_seconds INTEGER,
    limit_reached INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quota_samples_email_epoch
    ON quota_samples(email, epoch);
"""

# Columns added after v0.4 shipped; existing databases are migrated in place.
ACCOUNT_EXTRA_COLUMNS = {
    "tags": "TEXT NOT NULL DEFAULT ''",
    "note": "TEXT NOT NULL DEFAULT ''",
    "priority": "INTEGER NOT NULL DEFAULT 100",
    "disabled": "INTEGER NOT NULL DEFAULT 0",
}

SNAPSHOT_EXTRA_COLUMNS = {
    "auth_name": "TEXT",
}

SAMPLE_RETENTION_SECONDS = 30 * 86400
SAMPLE_MIN_INTERVAL_SECONDS = 55


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
    }
    for name, declaration in ACCOUNT_EXTRA_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {declaration}")

    snapshot_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(auth_snapshots)").fetchall()
    }
    for name, declaration in SNAPSHOT_EXTRA_COLUMNS.items():
        if name not in snapshot_columns:
            conn.execute(
                f"ALTER TABLE auth_snapshots ADD COLUMN {name} {declaration}"
            )


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class AccountStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            _ensure_columns(self._conn)
            self._conn.execute("PRAGMA journal_mode=WAL")
        try:
            self.backfill_auth_names()
        except Exception:  # noqa: BLE001 - display metadata must not block startup
            pass

    def backfill_auth_names(self) -> int:
        """Fill ``auth_name`` for snapshots saved before the column existed."""
        from .auth_state import read_auth_state_bytes

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT email, auth_blob FROM auth_snapshots
                 WHERE auth_name IS NULL OR auth_name = ''
                """
            ).fetchall()
        updated = 0
        for row in rows:
            try:
                state = read_auth_state_bytes(
                    dpapi.unprotect(bytes(row["auth_blob"]))
                )
                name = (state.get("name") or "").strip()
            except Exception:  # noqa: BLE001 - undecryptable snapshots stay unnamed
                name = ""
            if not name:
                continue
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE auth_snapshots SET auth_name = ? WHERE email = ?",
                    (name, row["email"]),
                )
            updated += 1
        return updated

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ writes
    def upsert(self, account: dict) -> str:
        """Insert or update an account. Returns 'inserted' or 'updated'.

        Metadata that is omitted from ``account`` (tags/note/priority) is left
        untouched when the account already exists.
        """
        email = account["email"]
        secret_payload = json.dumps(
            {
                "password": account.get("password", ""),
                "totp": account.get("totp", ""),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        blob = sqlite3.Binary(dpapi.protect(secret_payload))
        now = _now()
        masked = mask_email(email)
        has_password = 1 if account.get("password") else 0
        has_totp = 1 if account.get("totp") else 0
        totp_format = account.get("totp_format", "none")

        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id FROM accounts WHERE email = ?", (email,)
            ).fetchone()
            if row:
                self._conn.execute(
                    """
                    UPDATE accounts
                       SET email_masked = ?,
                           secret_blob = ?,
                           has_password = ?,
                           has_totp = ?,
                           totp_format = ?,
                           tags = COALESCE(?, tags),
                           note = COALESCE(?, note),
                           priority = COALESCE(?, priority),
                           updated_at = ?
                     WHERE email = ?
                    """,
                    (
                        masked,
                        blob,
                        has_password,
                        has_totp,
                        totp_format,
                        account.get("tags"),
                        account.get("note"),
                        account.get("priority"),
                        now,
                        email,
                    ),
                )
                return "updated"
            self._conn.execute(
                """
                INSERT INTO accounts
                    (email, email_masked, secret_blob, has_password, has_totp,
                     totp_format, status, tags, note, priority, created_at,
                     updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    masked,
                    blob,
                    has_password,
                    has_totp,
                    totp_format,
                    account.get("tags") or "",
                    account.get("note") or "",
                    _as_int(account.get("priority"), 100),
                    now,
                    now,
                ),
            )
            return "inserted"

    def update_account(
        self,
        email: str,
        *,
        password: str | None = None,
        totp: str | None = None,
        clear_totp: bool = False,
        tags: str | None = None,
        note: str | None = None,
        priority: int | None = None,
        disabled: bool | None = None,
    ) -> dict | None:
        """Update non-secret metadata and/or rotate credentials.

        Returns the public row (never the secrets), or ``None`` if missing.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM accounts WHERE lower(email) = lower(?)", (email,)
            ).fetchone()
            if not row:
                return None

            current = self.get_secret(email) or {}
            wants_secret = password is not None or totp is not None or clear_totp
            if wants_secret:
                merged = {
                    "password": current.get("password", ""),
                    "totp": current.get("totp", ""),
                }
                if password is not None:
                    merged["password"] = password
                if totp is not None:
                    merged["totp"] = totp
                if clear_totp:
                    merged["totp"] = ""
                blob = sqlite3.Binary(
                    dpapi.protect(json.dumps(merged, ensure_ascii=False).encode("utf-8"))
                )
            else:
                blob = None

            now = _now()
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE accounts
                       SET secret_blob = COALESCE(?, secret_blob),
                           has_password = CASE
                               WHEN ? IS NULL THEN has_password
                               ELSE ?
                           END,
                           has_totp = CASE
                               WHEN ? IS NULL THEN has_totp
                               ELSE ?
                           END,
                           totp_format = CASE
                               WHEN ? IS NULL THEN totp_format
                               ELSE ?
                           END,
                           tags = COALESCE(?, tags),
                           note = COALESCE(?, note),
                           priority = COALESCE(?, priority),
                           disabled = COALESCE(?, disabled),
                           email_masked = ?,
                           updated_at = ?
                     WHERE lower(email) = lower(?)
                    """,
                    (
                        blob,
                        None if not wants_secret else 1,
                        None
                        if not wants_secret
                        else (1 if merged.get("password") else 0),
                        None if not wants_secret else 1,
                        None if not wants_secret else (1 if merged.get("totp") else 0),
                        None if not wants_secret else 1,
                        None
                        if not wants_secret
                        else ("base32" if merged.get("totp") else "none"),
                        tags,
                        note,
                        priority,
                        None if disabled is None else (1 if disabled else 0),
                        mask_email(email),
                        now,
                        email,
                    ),
                )
        return self.get(email)

    def delete(self, email: str) -> dict:
        """Remove an account, its auth snapshot and its quota samples."""
        with self._lock, self._conn:
            account = self._conn.execute(
                "DELETE FROM accounts WHERE lower(email) = lower(?)", (email,)
            )
            snapshot = self._conn.execute(
                "DELETE FROM auth_snapshots WHERE lower(email) = lower(?)", (email,)
            )
            samples = self._conn.execute(
                "DELETE FROM quota_samples WHERE lower(email) = lower(?)", (email,)
            )
        return {
            "account": account.rowcount > 0,
            "snapshot": snapshot.rowcount > 0,
            "samples": max(0, samples.rowcount),
        }

    # ------------------------------------------------------------------- reads
    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT a.id, a.email, a.email_masked, a.has_password, a.has_totp,
                       a.totp_format, a.status, a.last_login_at, a.created_at,
                       a.updated_at, a.tags, a.note, a.priority, a.disabled,
                       s.auth_name AS name,
                       CASE WHEN s.email IS NOT NULL THEN 1 ELSE 0 END
                           AS has_auth_snapshot
                  FROM accounts a
                  LEFT JOIN auth_snapshots s ON s.email = a.email
                 ORDER BY a.priority DESC, a.id ASC
                """
            ).fetchall()
        return [self._public_row(row) for row in rows]

    def get(self, email: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT a.id, a.email, a.email_masked, a.has_password, a.has_totp,
                       a.totp_format, a.status, a.last_login_at, a.created_at,
                       a.updated_at, a.tags, a.note, a.priority, a.disabled,
                       s.auth_name AS name,
                       CASE WHEN s.email IS NOT NULL THEN 1 ELSE 0 END
                           AS has_auth_snapshot
                  FROM accounts a
                  LEFT JOIN auth_snapshots s ON s.email = a.email
                 WHERE lower(a.email) = lower(?)
                """,
                (email,),
            ).fetchone()
        return self._public_row(row) if row else None

    @staticmethod
    def _public_row(row) -> dict:
        return {
            "id": row["id"],
            "email": row["email"],
            "email_masked": row["email_masked"],
            "name": row["name"],
            "has_password": bool(row["has_password"]),
            "has_totp": bool(row["has_totp"]),
            "totp_format": row["totp_format"],
            "status": row["status"],
            "tags": row["tags"] or "",
            "note": row["note"] or "",
            "priority": int(row["priority"] or 0),
            "disabled": bool(row["disabled"]),
            "last_login_at": row["last_login_at"],
            "has_auth_snapshot": bool(row["has_auth_snapshot"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()
        return int(row["n"])

    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM accounts GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def get_secret(self, email: str) -> dict | None:
        """Internal use only: decrypt password/TOTP for the login engine."""
        with self._lock:
            row = self._conn.execute(
                "SELECT secret_blob FROM accounts WHERE lower(email) = lower(?)",
                (email,),
            ).fetchone()
        if not row:
            return None
        raw = dpapi.unprotect(bytes(row["secret_blob"]))
        return json.loads(raw.decode("utf-8"))

    # --------------------------------------------------------------- snapshots
    def save_auth_snapshot(
        self,
        email: str,
        auth_bytes: bytes,
        meta: dict | None = None,
    ) -> None:
        """Store an encrypted auth.json snapshot. Never returns its contents."""
        blob = sqlite3.Binary(dpapi.protect(auth_bytes))
        now = _now()
        meta = meta or {}
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO auth_snapshots
                    (email, auth_blob, auth_email, auth_name, auth_plan,
                     auth_account_id, auth_expires_at, saved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    auth_blob = excluded.auth_blob,
                    auth_email = excluded.auth_email,
                    auth_name = COALESCE(excluded.auth_name, auth_snapshots.auth_name),
                    auth_plan = excluded.auth_plan,
                    auth_account_id = excluded.auth_account_id,
                    auth_expires_at = excluded.auth_expires_at,
                    saved_at = excluded.saved_at
                """,
                (
                    email,
                    blob,
                    meta.get("email"),
                    (meta.get("name") or "").strip() or None,
                    meta.get("plan"),
                    meta.get("account_id"),
                    meta.get("expires_at"),
                    now,
                ),
            )
            self._conn.execute(
                """
                UPDATE accounts
                   SET status = 'ready', last_login_at = ?, updated_at = ?
                 WHERE email = ?
                """,
                (now, now, email),
            )

    def get_auth_snapshot(self, email: str) -> bytes | None:
        """Internal use only: decrypt an auth.json snapshot."""
        with self._lock:
            row = self._conn.execute(
                "SELECT auth_blob FROM auth_snapshots WHERE lower(email) = lower(?)",
                (email,),
            ).fetchone()
        if not row:
            return None
        return dpapi.unprotect(bytes(row["auth_blob"]))

    def has_auth_snapshot(self, email: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM auth_snapshots WHERE lower(email) = lower(?) LIMIT 1",
                (email,),
            ).fetchone()
        return row is not None

    def list_auth_snapshots(self) -> list[dict]:
        """List switchable snapshots without exposing encrypted contents."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.email, s.auth_email, s.auth_name, s.auth_plan,
                       s.auth_account_id, s.auth_expires_at, s.saved_at,
                       a.email_masked, a.status, a.tags, a.note, a.priority,
                       a.disabled
                  FROM auth_snapshots s
                  LEFT JOIN accounts a ON a.email = s.email
                 ORDER BY s.saved_at DESC, s.email ASC
                """
            ).fetchall()
        now = int(datetime.now(timezone.utc).timestamp())
        result = []
        for row in rows:
            expires = row["auth_expires_at"]
            result.append(
                {
                    "email": row["email"],
                    "email_masked": row["email_masked"]
                    or mask_email(row["email"]),
                    "name": row["auth_name"],
                    "plan": row["auth_plan"],
                    "account_id": row["auth_account_id"],
                    "expires_at": expires,
                    "expired": bool(expires and expires < now),
                    "saved_at": row["saved_at"],
                    "status": row["status"] or "snapshot_only",
                    "in_pool": row["email_masked"] is not None,
                    "tags": row["tags"] or "",
                    "note": row["note"] or "",
                    "priority": int(row["priority"] or 0),
                    "disabled": bool(row["disabled"]),
                }
            )
        return result

    def set_status(
        self,
        email: str,
        status: str,
        last_login_at: str | None = None,
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            if last_login_at is None:
                self._conn.execute(
                    "UPDATE accounts SET status = ?, updated_at = ? WHERE email = ?",
                    (status, now, email),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE accounts
                       SET status = ?, last_login_at = ?, updated_at = ?
                     WHERE email = ?
                    """,
                    (status, last_login_at, now, email),
                )

    # ------------------------------------------------------------ quota history
    def record_quota_sample(self, email: str, quota: dict, epoch: int | None = None) -> bool:
        """Persist one quota observation (throttled). Never stores tokens."""
        if not isinstance(quota, dict) or not quota.get("ok"):
            return False
        email = (email or "").strip()
        if not email:
            return False
        now = int(epoch if epoch is not None else time.time())
        primary = quota.get("primary_window") or {}
        secondary = quota.get("secondary_window") or {}
        if not primary and not secondary:
            return False

        with self._lock:
            last = self._conn.execute(
                "SELECT MAX(epoch) AS last FROM quota_samples WHERE lower(email) = lower(?)",
                (email,),
            ).fetchone()
            if last and last["last"] is not None:
                if now - int(last["last"]) < SAMPLE_MIN_INTERVAL_SECONDS:
                    return False
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO quota_samples
                        (email, epoch, plan, primary_used, primary_reset_at,
                         primary_window_seconds, secondary_used,
                         secondary_reset_at, secondary_window_seconds,
                         limit_reached, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        email,
                        now,
                        quota.get("plan"),
                        _as_float(primary.get("used_percent")),
                        _as_int(primary.get("reset_at"), 0) or None,
                        _as_int(primary.get("window_seconds"), 0) or None,
                        _as_float(secondary.get("used_percent")),
                        _as_int(secondary.get("reset_at"), 0) or None,
                        _as_int(secondary.get("window_seconds"), 0) or None,
                        1 if quota.get("limit_reached") else 0,
                        datetime.fromtimestamp(now, timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                    ),
                )
                self._conn.execute(
                    "DELETE FROM quota_samples WHERE epoch < ?",
                    (now - SAMPLE_RETENTION_SECONDS,),
                )
        return True

    def quota_history(self, email: str, hours: int = 24, limit: int = 2000) -> list[dict]:
        """Return quota samples in ascending time order (display fields only)."""
        since = int(time.time()) - max(1, int(hours)) * 3600
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT email, epoch, plan, primary_used, primary_reset_at,
                       primary_window_seconds, secondary_used,
                       secondary_reset_at, secondary_window_seconds,
                       limit_reached, ts
                  FROM quota_samples
                 WHERE lower(email) = lower(?) AND epoch >= ?
                 ORDER BY epoch ASC
                 LIMIT ?
                """,
                (email, since, int(limit)),
            ).fetchall()
        return [
            {
                "email": row["email"],
                "epoch": int(row["epoch"]),
                "ts": row["ts"],
                "plan": row["plan"],
                "primary_used": row["primary_used"],
                "primary_reset_at": row["primary_reset_at"],
                "primary_window_seconds": row["primary_window_seconds"],
                "secondary_used": row["secondary_used"],
                "secondary_reset_at": row["secondary_reset_at"],
                "secondary_window_seconds": row["secondary_window_seconds"],
                "limit_reached": bool(row["limit_reached"]),
            }
            for row in rows
        ]
