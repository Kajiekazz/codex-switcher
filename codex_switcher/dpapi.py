"""Windows DPAPI helpers.

Secrets (passwords / TOTP seeds) are encrypted with DPAPI under the current
Windows user. The ciphertext can only be decrypted by the same user on the
same machine, which is what we want for a local-only account store.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

CRYPTPROTECT_UI_FORBIDDEN = 0x1
DEFAULT_ENTROPY = b"CodexSwitcher/v1"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _make_blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array | None]:
    """Build a DATA_BLOB and return it together with the backing buffer.

    The caller must keep the buffer alive until the DPAPI call returns.
    """
    if not data:
        return DATA_BLOB(0, None), None
    buf = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


if hasattr(ctypes, "WinDLL"):
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - non-Windows import guard
    _crypt32 = None
    _kernel32 = None

if _crypt32 is not None:
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def protect(data: bytes, entropy: bytes = DEFAULT_ENTROPY) -> bytes:
    if _crypt32 is None:
        raise RuntimeError("DPAPI is only available on Windows")
    blob_in, _buf_in = _make_blob(data)
    blob_entropy, _buf_entropy = _make_blob(entropy)
    blob_out = DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "CodexSwitcher",
        ctypes.byref(blob_entropy),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def unprotect(blob: bytes, entropy: bytes = DEFAULT_ENTROPY) -> bytes:
    if _crypt32 is None:
        raise RuntimeError("DPAPI is only available on Windows")
    blob_in, _buf_in = _make_blob(blob)
    blob_entropy, _buf_entropy = _make_blob(entropy)
    blob_out = DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_entropy),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)
