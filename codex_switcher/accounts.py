"""Account parsing, masking, and TOTP helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
from urllib.parse import parse_qs, urlparse

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BASE32_RE = re.compile(r"^[A-Z2-7]+$")


def mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        local_masked = local[:1] + "***"
    else:
        local_masked = local[:2] + "***"
    return f"{local_masked}@{domain}"


def normalize_totp_secret(secret: str) -> str:
    """Accept raw base32 or an otpauth:// URL and return normalized base32."""
    value = (secret or "").strip()
    if not value:
        return ""
    if value.lower().startswith("otpauth://"):
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        value = (query.get("secret") or [""])[0]
    value = re.sub(r"[\s\-]", "", value).upper()
    value = value.rstrip("=")
    return value


def totp_code(secret: str, at: float | None = None, digits: int = 6, period: int = 30) -> str:
    normalized = normalize_totp_secret(secret)
    if not normalized or not BASE32_RE.fullmatch(normalized):
        raise ValueError("invalid base32 TOTP secret")
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + padding, casefold=True)
    counter = int(at if at is not None else time.time()) // period
    message = struct.pack(">Q", counter)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (
        struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    ) % (10**digits)
    return str(code).zfill(digits)


def totp_remaining(period: int = 30, at: float | None = None) -> int:
    now = int(at if at is not None else time.time())
    return period - (now % period)


def _extract_email_hint(line: str) -> str:
    match = re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", line)
    return mask_email(match.group(0)) if match else "<redacted>"


def _looks_like_totp(value: str) -> bool:
    """Heuristic: could this dash-separated tail be a TOTP seed?

    Raw secrets need at least 16 base32 characters so ordinary hyphenated
    password segments are not mistaken for a seed. An ``otpauth://`` URL is
    accepted whenever it carries a valid secret.
    """
    raw = (value or "").strip()
    if not raw:
        return False
    normalized = normalize_totp_secret(raw)
    if not normalized or not BASE32_RE.fullmatch(normalized):
        return False
    if raw.lower().startswith("otpauth://"):
        return True
    return len(normalized) >= 16


def _split_dash_fields(line: str) -> tuple[str, str, str]:
    """Split ``email---password---2fa`` with any dash-run lengths.

    The email is located by scanning dash runs from the left until the text
    before the run is a valid email (so ``xn--...`` domains survive). The
    password/2FA boundary is the right-most dash run whose tail looks like a
    TOTP seed; if none does, the rest of the line is the password. This keeps
    passwords containing single or double dashes intact.
    """
    email = ""
    rest = ""
    for run in re.finditer(r"-+", line):
        candidate = line[: run.start()].strip()
        if EMAIL_RE.fullmatch(candidate):
            email = candidate
            rest = line[run.end() :]
            break
    if not email:
        raise ValueError("expected email + dashes + password [+ dashes + 2FA]")

    rest = rest.strip()
    if rest.endswith("-"):
        # Exporters often leave the separator even when 2FA is empty.
        rest = rest.rstrip("-").strip()
    if not rest:
        return email, "", ""

    for run in reversed(list(re.finditer(r"-+", rest))):
        password = rest[: run.start()].strip()
        tail = rest[run.end() :].strip()
        if password and _looks_like_totp(tail):
            return email, password, tail
    return email, rest, ""


def parse_accounts(text: str) -> tuple[list[dict], list[dict]]:
    """Parse pasted accounts.

    Supported formats (dash runs are variable length, including ``-``):
      email-----password---2fa
      email----password----2fa
      email-password-2fa
      email|password|2fa

    In the dash form the password/2FA boundary is detected from the right:
    the last dash-separated segment that looks like a base32 TOTP seed. A
    password that contains dashes therefore survives. Use the pipe form when
    the password itself could be mistaken for a seed. Lines starting with '#'
    and blank lines are ignored.
    """
    accounts: list[dict] = []
    errors: list[dict] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if "|" in line:
                parts = [part.strip() for part in line.split("|")]
                if len(parts) < 2:
                    raise ValueError("expected email|password[|2fa]")
                email, password = parts[0], parts[1]
                totp_raw = parts[2] if len(parts) >= 3 else ""
            else:
                email, password, totp_raw = _split_dash_fields(line)

            if not EMAIL_RE.match(email):
                raise ValueError(f"invalid email: {email!r}")
            if not password:
                raise ValueError("empty password")

            totp = normalize_totp_secret(totp_raw)
            if totp and not BASE32_RE.fullmatch(totp):
                totp_format = "unknown"
            elif totp:
                totp_format = "base32"
            else:
                totp_format = "none"

            accounts.append(
                {
                    "email": email,
                    "password": password,
                    "totp": totp,
                    "totp_format": totp_format,
                    "line": lineno,
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "line": lineno,
                    "error": str(exc),
                    "email_masked": _extract_email_hint(raw),
                }
            )

    return accounts, errors


def normalize_tags(value) -> str:
    """Normalize tags from a list or a comma/space separated string.

    Returns a comma separated string so the value stays simple to edit in the
    dashboard. At most 8 tags of 20 characters are kept.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        raw = re.split(r"[,，\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw = []
        for item in value:
            raw.extend(re.split(r"[,，\s]+", str(item)))
    else:
        raise ValueError("tags 必须是字符串或数组")

    seen: list[str] = []
    for item in raw:
        tag = item.strip()
        if not tag:
            continue
        tag = tag[:20]
        if tag.lower() in {existing.lower() for existing in seen}:
            continue
        seen.append(tag)
        if len(seen) >= 8:
            break
    return ",".join(seen)


def parse_priority(value, default: int = 100) -> int:
    """Validate a priority. Larger values are preferred by auto-switch."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("priority 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("priority 必须是整数") from exc
    if number < 0 or number > 1000:
        raise ValueError("priority 必须在 0 到 1000 之间")
    return number


def public_account(account: dict) -> dict:
    """Serialize an account without secrets."""
    return {
        "email": account["email"],
        "email_masked": mask_email(account["email"]),
        "has_password": bool(account.get("password")),
        "has_totp": bool(account.get("totp")),
        "totp_format": account.get("totp_format", "none"),
        "tags": normalize_tags(account.get("tags")) if account.get("tags") else "",
        "note": account.get("note") or "",
        "priority": parse_priority(account.get("priority"), 100),
    }
