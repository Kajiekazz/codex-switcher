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
from .auth_state import read_auth_state_bytes
from .identity import profile_id_for


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

CREATE TABLE IF NOT EXISTS auth_profiles (
    profile_id TEXT PRIMARY KEY,
    email TEXT NOT NULL COLLATE NOCASE,
    auth_blob BLOB NOT NULL,
    auth_email TEXT,
    auth_name TEXT,
    auth_plan TEXT,
    auth_user_id TEXT,
    auth_account_id TEXT,
    workspace_name TEXT,
    identity_error TEXT,
    auth_expires_at INTEGER,
    saved_at TEXT NOT NULL,
    priority INTEGER,
    disabled INTEGER
);
CREATE INDEX IF NOT EXISTS idx_auth_profiles_email ON auth_profiles(email);
CREATE TABLE IF NOT EXISTS auth_profile_aliases (
    alias_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS store_migrations (
    name TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_totp (
    email TEXT PRIMARY KEY,
    secret_blob BLOB NOT NULL,
    created_at TEXT NOT NULL
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
    ts TEXT NOT NULL,
    profile_id TEXT
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

    sample_columns = {row["name"] for row in conn.execute("PRAGMA table_info(quota_samples)")}
    if "profile_id" not in sample_columns:
        conn.execute("ALTER TABLE quota_samples ADD COLUMN profile_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quota_samples_profile_epoch ON quota_samples(profile_id, epoch)")


class AmbiguousProfileError(ValueError):
    code = "workspace_required"

    def __init__(self, email: str):
        super().__init__("该邮箱有多个工作区，请选择具体工作区")
        self.email = email


class InvalidProfileError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
        self._backup_before_profile_migration()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            _ensure_columns(self._conn)
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate_auth_profiles()
        try:
            self.backfill_auth_names()
        except Exception:  # noqa: BLE001 - display metadata must not block startup
            pass

    def _backup_before_profile_migration(self) -> None:
        """Take an SQLite-consistent encrypted backup before touching an old DB."""
        tables = {row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "accounts" not in tables:
            return
        if "store_migrations" in tables and self._conn.execute(
            "SELECT 1 FROM store_migrations WHERE name='auth_profiles_v1'"
        ).fetchone():
            return
        backup = self.path.with_name(self.path.name + ".pre-workspaces.bak")
        if backup.exists():
            return
        temporary = backup.with_name(backup.name + ".tmp")
        destination = sqlite3.connect(temporary)
        try:
            self._conn.backup(destination)
        finally:
            destination.close()
        temporary.replace(backup)

    def _migrate_auth_profiles(self) -> None:
        """Retain the old table and copy its encrypted blobs exactly once."""
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM store_migrations WHERE name='auth_profiles_v1'").fetchone():
                return
            for row in self._conn.execute("SELECT * FROM auth_snapshots").fetchall():
                try:
                    state = read_auth_state_bytes(dpapi.unprotect(bytes(row["auth_blob"])))
                except Exception:
                    state = {"identity_error": "snapshot_unreadable"}
                error = state.get("identity_error")
                if state.get("logged_in") is False and not error:
                    error = "target_snapshot_invalid"
                email = row["email"]
                actual_email = state.get("email") or row["auth_email"]
                if actual_email and actual_email.casefold() != email.casefold():
                    error = "snapshot_email_mismatch"
                identity = {
                    "email": email,
                    "user_id": state.get("user_id"),
                    "account_id": state.get("account_id") or row["auth_account_id"],
                }
                profile_id = profile_id_for(identity)
                self._conn.execute(
                    """INSERT OR IGNORE INTO auth_profiles
                       (profile_id,email,auth_blob,auth_email,auth_name,auth_plan,auth_user_id,
                        auth_account_id,workspace_name,identity_error,auth_expires_at,saved_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (profile_id, email, row["auth_blob"], actual_email,
                     row["auth_name"] or state.get("name"), row["auth_plan"] or state.get("plan"),
                     identity["user_id"], identity["account_id"], state.get("workspace_name"),
                     error, row["auth_expires_at"], row["saved_at"]),
                )
                self._conn.execute(
                    "UPDATE quota_samples SET profile_id=? WHERE profile_id IS NULL AND lower(email)=lower(?)",
                    (profile_id, email),
                )
            for row in self._conn.execute("SELECT DISTINCT email FROM quota_samples WHERE profile_id IS NULL").fetchall():
                self._conn.execute(
                    "UPDATE quota_samples SET profile_id=? WHERE profile_id IS NULL AND lower(email)=lower(?)",
                    (profile_id_for({"email": row["email"]}), row["email"]),
                )
            self._conn.execute("INSERT INTO store_migrations(name,completed_at) VALUES('auth_profiles_v1',?)", (_now(),))

    def backfill_auth_names(self) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT profile_id,auth_blob FROM auth_profiles WHERE auth_name IS NULL OR auth_name=''"
            ).fetchall()
        updated = 0
        for row in rows:
            try:
                name = (read_auth_state_bytes(dpapi.unprotect(bytes(row["auth_blob"]))).get("name") or "").strip()
            except Exception:
                name = ""
            if name:
                with self._lock, self._conn:
                    self._conn.execute("UPDATE auth_profiles SET auth_name=? WHERE profile_id=?", (name, row["profile_id"]))
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
            self._conn.execute(
                "DELETE FROM auth_profile_aliases WHERE profile_id IN (SELECT profile_id FROM auth_profiles WHERE lower(email)=lower(?))",
                (email,),
            )
            snapshot = self._conn.execute(
                "DELETE FROM auth_profiles WHERE lower(email) = lower(?)", (email,)
            )
            self._conn.execute("DELETE FROM auth_snapshots WHERE lower(email)=lower(?)", (email,))
            samples = self._conn.execute(
                "DELETE FROM quota_samples WHERE lower(email) = lower(?)", (email,)
            )
            self._conn.execute("DELETE FROM pending_totp WHERE lower(email)=lower(?)", (email,))
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
                       (SELECT p.auth_name FROM auth_profiles p WHERE p.email=a.email COLLATE NOCASE
                        ORDER BY p.saved_at DESC, p.profile_id LIMIT 1) AS name,
                       EXISTS(SELECT 1 FROM auth_profiles p WHERE p.email=a.email COLLATE NOCASE)
                           AS has_auth_snapshot
                  FROM accounts a
                 ORDER BY a.priority DESC, a.id ASC
                """
            ).fetchall()
        profiles = self.list_auth_snapshots()
        return [self._account_profiles(self._public_row(row), profiles) for row in rows]

    @staticmethod
    def _account_profiles(account: dict, profiles: list[dict]) -> dict:
        account["profiles"] = [p for p in profiles if p["email"].casefold() == account["email"].casefold()]
        account["profile_count"] = len(account["profiles"])
        return account

    def get(self, email: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT a.id, a.email, a.email_masked, a.has_password, a.has_totp,
                       a.totp_format, a.status, a.last_login_at, a.created_at,
                       a.updated_at, a.tags, a.note, a.priority, a.disabled,
                       (SELECT p.auth_name FROM auth_profiles p WHERE p.email=a.email COLLATE NOCASE
                        ORDER BY p.saved_at DESC, p.profile_id LIMIT 1) AS name,
                       EXISTS(SELECT 1 FROM auth_profiles p WHERE p.email=a.email COLLATE NOCASE)
                           AS has_auth_snapshot
                  FROM accounts a
                 WHERE lower(a.email) = lower(?)
                """,
                (email,),
            ).fetchone()
        return self._account_profiles(self._public_row(row), self.list_auth_snapshots()) if row else None

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
    def stage_totp(self, email: str, secret: str) -> None:
        """Keep an encrypted recovery copy before submitting a binding code."""
        with self._lock, self._conn:
            account = self.get(email)
            if account is None or account["has_totp"]:
                raise ValueError("账号不存在或已保存 TOTP，请勿重新绑定")
            blob = sqlite3.Binary(dpapi.protect(secret.encode("ascii")))
            self._conn.execute(
                "INSERT INTO pending_totp(email, secret_blob, created_at) VALUES(?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET secret_blob=excluded.secret_blob, created_at=excluded.created_at",
                (account["email"], blob, _now()),
            )

    def pending_totp(self, email: str) -> str | None:
        """Internal only; an unconfirmed seed is never exposed through APIs."""
        with self._lock:
            row = self._conn.execute("SELECT secret_blob FROM pending_totp WHERE lower(email)=lower(?)", (email,)).fetchone()
            return dpapi.unprotect(bytes(row[0])).decode("ascii") if row else None

    def confirm_totp(self, email: str) -> None:
        """Promote a confirmed seed and preserve every other account field."""
        with self._lock, self._conn:
            seed = self.pending_totp(email)
            secret = self.get_secret(email)
            if not seed or secret is None:
                raise ValueError("没有待确认的 TOTP 密钥")
            if secret.get("totp") and secret["totp"] != seed:
                raise ValueError("账号已保存其他 TOTP，未覆盖")
            secret["totp"] = seed
            blob = sqlite3.Binary(dpapi.protect(json.dumps(secret).encode("utf-8")))
            self._conn.execute("UPDATE accounts SET secret_blob=?, has_totp=1, totp_format='base32', updated_at=? WHERE lower(email)=lower(?)",
                               (blob, _now(), email))
            self._conn.execute("DELETE FROM pending_totp WHERE lower(email)=lower(?)", (email,))

    def import_totp(self, email: str, seed: str) -> None:
        """Save a user-supplied seed without asserting the server enabled MFA."""
        with self._lock, self._conn:
            secret = self.get_secret(email)
            if secret is None:
                raise ValueError("账号不存在")
            secret["totp"] = seed
            blob = sqlite3.Binary(dpapi.protect(json.dumps(secret).encode("utf-8")))
            self._conn.execute("UPDATE accounts SET secret_blob=?, has_totp=1, totp_format='base32', updated_at=? WHERE lower(email)=lower(?)",
                               (blob, _now(), email))
            self._conn.execute("DELETE FROM pending_totp WHERE lower(email)=lower(?)", (email,))

    def save_auth_snapshot(
        self, email: str, auth_bytes: bytes, meta: dict | None = None,
    ) -> str:
        """Save one member/workspace authorization without overwriting its siblings."""
        state = read_auth_state_bytes(auth_bytes)
        if not state.get("logged_in"):
            raise InvalidProfileError(state.get("identity_error") or "target_snapshot_invalid")
        meta = meta or {}
        email = email.strip()
        if not email:
            raise InvalidProfileError("email_required")
        actual_email = state.get("email") or meta.get("email")
        if actual_email and actual_email.casefold() != email.casefold():
            raise InvalidProfileError("snapshot_email_mismatch")
        for field in ("account_id", "user_id"):
            candidates = [meta.get(field)]
            if field == "account_id":
                candidates.append(meta.get("workspace_id"))
            if state.get(field) and any(value and state[field] != value for value in candidates):
                raise InvalidProfileError("workspace_identity_conflict" if field == "account_id" else "user_identity_conflict")
        identity = {
            "email": email,
            "user_id": state.get("user_id") or meta.get("user_id"),
            "account_id": state.get("account_id") or meta.get("workspace_id") or meta.get("account_id"),
        }
        profile_id = profile_id_for(identity)
        blob, now = sqlite3.Binary(dpapi.protect(auth_bytes)), _now()
        with self._lock, self._conn:
            # Old snapshots may lack user_id. Once official auth supplies it,
            # promote that exact email/workspace and keep old references usable.
            previous = None
            if identity["user_id"] and identity["account_id"]:
                previous = self._conn.execute(
                    "SELECT * FROM auth_profiles WHERE email=? COLLATE NOCASE AND auth_account_id=? "
                    "AND (auth_user_id IS NULL OR auth_user_id='') AND identity_error IS NULL",
                    (email, identity["account_id"]),
                ).fetchone()
            self._conn.execute(
                """INSERT INTO auth_profiles
                   (profile_id,email,auth_blob,auth_email,auth_name,auth_plan,auth_user_id,
                    auth_account_id,workspace_name,identity_error,auth_expires_at,saved_at)
                   VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?)
                   ON CONFLICT(profile_id) DO UPDATE SET
                    email=excluded.email,auth_blob=excluded.auth_blob,auth_email=excluded.auth_email,
                    auth_name=COALESCE(excluded.auth_name,auth_profiles.auth_name),
                    auth_plan=excluded.auth_plan,auth_user_id=excluded.auth_user_id,
                    auth_account_id=excluded.auth_account_id,
                    workspace_name=COALESCE(excluded.workspace_name,auth_profiles.workspace_name),
                    identity_error=NULL,auth_expires_at=excluded.auth_expires_at,saved_at=excluded.saved_at""",
                (profile_id, email, blob, actual_email,
                 (meta.get("name") or "").strip() or None,
                 state.get("plan") or meta.get("plan"), identity["user_id"], identity["account_id"],
                 state.get("workspace_name") or meta.get("workspace_name"),
                 state.get("expires_at") or meta.get("expires_at"), now),
            )
            if previous and previous["profile_id"] != profile_id:
                old_id = previous["profile_id"]
                self._conn.execute(
                    "UPDATE auth_profiles SET priority=COALESCE(priority,?),disabled=COALESCE(disabled,?),"
                    "workspace_name=COALESCE(workspace_name,?),auth_name=COALESCE(auth_name,?) WHERE profile_id=?",
                    (previous["priority"], previous["disabled"], previous["workspace_name"], previous["auth_name"], profile_id),
                )
                self._conn.execute("UPDATE quota_samples SET profile_id=? WHERE profile_id=?", (profile_id, old_id))
                self._conn.execute("UPDATE auth_profile_aliases SET profile_id=? WHERE profile_id=?", (profile_id, old_id))
                self._conn.execute("INSERT OR REPLACE INTO auth_profile_aliases(alias_id,profile_id) VALUES(?,?)", (old_id, profile_id))
                self._conn.execute("DELETE FROM auth_profiles WHERE profile_id=?", (old_id,))
            self._conn.execute(
                "UPDATE accounts SET status='ready',last_login_at=?,updated_at=? WHERE lower(email)=lower(?)",
                (now, now, email),
            )
        return profile_id

    def _profile_row(self, ref: str):
        ref = (ref or "").strip()
        row = self._conn.execute("SELECT * FROM auth_profiles WHERE profile_id=?", (ref,)).fetchone()
        if row is not None:
            return row
        row = self._conn.execute(
            "SELECT p.* FROM auth_profile_aliases a JOIN auth_profiles p ON p.profile_id=a.profile_id WHERE a.alias_id=?", (ref,)
        ).fetchone()
        if row is not None:
            return row
        rows = self._conn.execute("SELECT * FROM auth_profiles WHERE lower(email)=lower(?)", (ref,)).fetchall()
        if len(rows) > 1:
            raise AmbiguousProfileError(ref)
        return rows[0] if rows else None

    def resolve_auth_profile(self, ref: str) -> dict | None:
        with self._lock:
            row = self._profile_row(ref)
            if row is None:
                return None
            joined = self._conn.execute(
                """SELECT p.*,a.email_masked,a.status,a.tags,a.note,
                          a.priority AS account_priority,a.disabled AS account_disabled
                   FROM auth_profiles p LEFT JOIN accounts a ON a.email=p.email COLLATE NOCASE
                   WHERE p.profile_id=?""", (row["profile_id"],)
            ).fetchone()
            return self._public_profile_row(joined)

    def get_auth_snapshot(self, email: str) -> bytes | None:
        """Internal only. Email is accepted only when it identifies one profile."""
        with self._lock:
            row = self._profile_row(email)
            return dpapi.unprotect(bytes(row["auth_blob"])) if row else None

    def has_auth_snapshot(self, email: str) -> bool:
        with self._lock:
            return self._profile_row(email) is not None

    def list_auth_snapshots(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT p.*,a.email_masked,a.status,a.tags,a.note,
                          a.priority AS account_priority,a.disabled AS account_disabled
                   FROM auth_profiles p LEFT JOIN accounts a ON a.email=p.email COLLATE NOCASE
                   ORDER BY p.saved_at DESC,p.email,p.profile_id"""
            ).fetchall()
        return [self._public_profile_row(row) for row in rows]

    @staticmethod
    def _public_profile_row(row) -> dict:
        now = int(time.time())
        return {
            "profile_id": row["profile_id"],
            "email": row["email"], "email_masked": row["email_masked"] or mask_email(row["email"]),
            "name": row["auth_name"], "plan": row["auth_plan"],
            "user_id": row["auth_user_id"], "account_id": row["auth_account_id"],
            "workspace_id": row["auth_account_id"], "workspace_name": row["workspace_name"],
            "identity_error": row["identity_error"],
            "expires_at": row["auth_expires_at"],
            "expired": bool(row["auth_expires_at"] and row["auth_expires_at"] < now),
            "saved_at": row["saved_at"],
            "status": "invalid" if row["identity_error"] else ("ready" if row["email_masked"] is not None else "snapshot_only"),
            "in_pool": row["email_masked"] is not None,
            "tags": row["tags"] or "", "note": row["note"] or "",
            "priority": int(row["priority"] if row["priority"] is not None else (row["account_priority"] or 0)),
            "disabled": bool(row["identity_error"] or row["account_disabled"] or row["disabled"]),
        }

    def update_auth_profile(self, profile_id: str, *, priority: int | None = None,
                            disabled: bool | None = None, workspace_name: str | None = None) -> dict | None:
        with self._lock, self._conn:
            if not self._conn.execute("SELECT 1 FROM auth_profiles WHERE profile_id=?", (profile_id,)).fetchone():
                return None
            self._conn.execute(
                "UPDATE auth_profiles SET priority=COALESCE(?,priority),disabled=COALESCE(?,disabled),"
                "workspace_name=COALESCE(?,workspace_name) WHERE profile_id=?",
                (priority, None if disabled is None else int(disabled), workspace_name, profile_id),
            )
        return self.resolve_auth_profile(profile_id)

    def delete_auth_profile(self, profile_id: str) -> dict:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT email FROM auth_profiles WHERE profile_id=?", (profile_id,)).fetchone()
            self._conn.execute("DELETE FROM auth_profile_aliases WHERE profile_id=?", (profile_id,))
            deleted = self._conn.execute("DELETE FROM auth_profiles WHERE profile_id=?", (profile_id,))
            samples = self._conn.execute("DELETE FROM quota_samples WHERE profile_id=?", (profile_id,))
            if row and not self._conn.execute("SELECT 1 FROM auth_profiles WHERE email=? COLLATE NOCASE", (row["email"],)).fetchone():
                self._conn.execute("UPDATE accounts SET status='unknown',updated_at=? WHERE email=? COLLATE NOCASE", (_now(), row["email"]))
        return {"profile": deleted.rowcount > 0, "snapshot": deleted.rowcount > 0, "samples": max(0, samples.rowcount)}

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
        profile = self.resolve_auth_profile(email)
        if profile:
            email, profile_id = profile["email"], profile["profile_id"]
        elif email.startswith("profile_"):
            # A live account can be observed before the user imports it. Store
            # only the public identity supplied by the quota service, not auth.
            if quota.get("profile_id") != email or not isinstance(quota.get("email"), str):
                return False
            profile_id, email = email, quota["email"].strip()
            if not email or "@" not in email:
                return False
        else:
            profile_id = profile_id_for({"email": email})
        now = int(epoch if epoch is not None else time.time())
        primary = quota.get("primary_window") or {}
        secondary = quota.get("secondary_window") or {}
        if not primary and not secondary:
            return False

        with self._lock:
            last = self._conn.execute(
                "SELECT MAX(epoch) AS last FROM quota_samples WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            if last and last["last"] is not None:
                if now - int(last["last"]) < SAMPLE_MIN_INTERVAL_SECONDS:
                    return False
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO quota_samples
                        (email, profile_id, epoch, plan, primary_used, primary_reset_at,
                         primary_window_seconds, secondary_used,
                         secondary_reset_at, secondary_window_seconds,
                         limit_reached, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        email,
                        profile_id,
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
        profile = self.resolve_auth_profile(email)
        if profile:
            profile_id = profile["profile_id"]
        elif email.startswith("profile_"):
            profile_id = email
        else:
            profile_id = profile_id_for({"email": email})
        since = int(time.time()) - max(1, int(hours)) * 3600
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT email, profile_id, epoch, plan, primary_used, primary_reset_at,
                       primary_window_seconds, secondary_used,
                       secondary_reset_at, secondary_window_seconds,
                       limit_reached, ts
                  FROM quota_samples
                 WHERE profile_id = ? AND epoch >= ?
                 ORDER BY epoch ASC
                 LIMIT ?
                """,
                (profile_id, since, int(limit)),
            ).fetchall()
        return [
            {
                "email": row["email"],
                "profile_id": row["profile_id"],
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
