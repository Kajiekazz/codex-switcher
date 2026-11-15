"""Read registered Codex app IDs with Windows AppModel APIs, without a shell.

Registration is queried for the current user, not inferred from files or URL
handlers. This is display metadata, not an assertion that every dependency is
healthy or that activation will succeed. No API here launches an application.
"""
from __future__ import annotations

import ctypes
import os
import re


CODEX_PACKAGE_FAMILIES = (
    "OpenAI.Codex_2p2nqsd0c76g0",
    "OpenAI.Codex_3k8sg7r9htsxt",
)
_SUCCESS = 0
_INSUFFICIENT_BUFFER = 122
_NOT_FOUND = 1168
_MAX_BUFFER_BYTES = 1024 * 1024
_MAX_PACKAGES = 1024
_MAX_APPLICATIONS = 100
_MAX_ATTEMPTS = 3
_UINT32 = ctypes.c_uint32
_POINTER_BYTES = ctypes.sizeof(ctypes.c_void_p)


def _error(operation, code):
    return OSError(code, f"{operation} failed ({code})")


def _load_api():
    if os.name != "nt":
        raise OSError("Windows AppModel APIs are unavailable")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        api.GetPackagesByPackageFamily.argtypes = [
            ctypes.c_wchar_p, ctypes.POINTER(_UINT32), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(_UINT32), ctypes.c_void_p,
        ]
        api.GetPackagesByPackageFamily.restype = ctypes.c_int32
        api.OpenPackageInfoByFullName.argtypes = [
            ctypes.c_wchar_p, _UINT32, ctypes.POINTER(ctypes.c_void_p),
        ]
        api.OpenPackageInfoByFullName.restype = ctypes.c_int32
        api.GetPackageApplicationIds.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_UINT32), ctypes.c_void_p,
            ctypes.POINTER(_UINT32),
        ]
        api.GetPackageApplicationIds.restype = ctypes.c_int32
        api.ClosePackageInfo.argtypes = [ctypes.c_void_p]
        api.ClosePackageInfo.restype = ctypes.c_int32
    except AttributeError as exc:
        raise OSError("Windows AppModel APIs are unavailable") from exc
    return api


def _buffer_strings(buffer, size, pointers, *, minimum_offset=0):
    """Decode only NUL-terminated UTF-16 strings inside our own API buffer."""
    if size < 0 or size > ctypes.sizeof(buffer):
        raise OSError("Invalid AppModel buffer length")
    base = ctypes.addressof(buffer)
    raw = bytes(buffer)[:size]
    strings = []
    for pointer in pointers:
        offset = int(pointer or 0) - base
        if offset < minimum_offset or offset % 2 or offset + 2 > size:
            raise OSError("Invalid AppModel string pointer")
        for end in range(offset, size - 1, 2):
            if raw[end:end + 2] == b"\0\0":
                try:
                    strings.append(raw[offset:end].decode("utf-16-le"))
                except UnicodeDecodeError as exc:
                    raise OSError("Invalid AppModel string") from exc
                break
        else:
            raise OSError("Unterminated AppModel string")
    return strings


def _package_names(api, family):
    count, chars = _UINT32(), _UINT32()
    result = api.GetPackagesByPackageFamily(family, ctypes.byref(count), None,
                                           ctypes.byref(chars), None)
    if result == _SUCCESS and count.value == 0:
        return []
    if result != _INSUFFICIENT_BUFFER:
        raise _error("GetPackagesByPackageFamily", result)
    for _ in range(_MAX_ATTEMPTS):
        capacity, buffer_bytes = count.value, chars.value * 2
        if not 0 < capacity <= _MAX_PACKAGES or not 0 < buffer_bytes <= _MAX_BUFFER_BYTES:
            raise OSError("Invalid AppModel package buffer size")
        pointers = (ctypes.c_void_p * capacity)()
        buffer = ctypes.create_string_buffer(buffer_bytes)
        result = api.GetPackagesByPackageFamily(family, ctypes.byref(count), pointers,
                                               ctypes.byref(chars), buffer)
        if result == _INSUFFICIENT_BUFFER:
            continue  # Registration can change between the sizing and read calls.
        if result != _SUCCESS:
            raise _error("GetPackagesByPackageFamily", result)
        if count.value > capacity or chars.value * 2 > buffer_bytes:
            raise OSError("Invalid AppModel package response length")
        return _buffer_strings(buffer, chars.value * 2, pointers[:count.value])
    raise OSError("AppModel package registration kept changing")


def _application_ids(api, full_name):
    reference = ctypes.c_void_p()
    result = api.OpenPackageInfoByFullName(full_name, 0, ctypes.byref(reference))
    if result == _NOT_FOUND:
        return []  # Uninstalled between enumeration and opening the reference.
    if result != _SUCCESS:
        raise _error("OpenPackageInfoByFullName", result)
    if not reference.value:
        raise OSError("AppModel returned an empty package reference")
    try:
        size, count = _UINT32(), _UINT32()
        result = api.GetPackageApplicationIds(reference, ctypes.byref(size), None,
                                              ctypes.byref(count))
        if result == _SUCCESS and count.value == 0:
            return []
        if result != _INSUFFICIENT_BUFFER:
            raise _error("GetPackageApplicationIds", result)
        for _ in range(_MAX_ATTEMPTS):
            capacity = size.value
            if not 0 < capacity <= _MAX_BUFFER_BYTES:
                raise OSError("Invalid AppModel application buffer size")
            buffer = ctypes.create_string_buffer(capacity)
            result = api.GetPackageApplicationIds(reference, ctypes.byref(size), buffer,
                                                  ctypes.byref(count))
            if result == _INSUFFICIENT_BUFFER:
                continue
            if result != _SUCCESS:
                raise _error("GetPackageApplicationIds", result)
            header_bytes = count.value * _POINTER_BYTES
            if (size.value > capacity or count.value > _MAX_APPLICATIONS
                    or header_bytes > size.value):
                raise OSError("Invalid AppModel application response length")
            pointers = (ctypes.c_void_p * count.value).from_buffer(buffer)
            return _buffer_strings(buffer, size.value, pointers, minimum_offset=header_bytes)
        raise OSError("AppModel application registration kept changing")
    finally:
        result = api.ClosePackageInfo(reference)
        if result != _SUCCESS:
            raise _error("ClosePackageInfo", result)


def _version(full_name, family):
    """Reject unrelated results and ignore language/resource-only packages."""
    parts = full_name.split("_")
    expected_name, expected_publisher = family.rsplit("_", 1)
    if len(parts) != 5 or parts[0] != expected_name or parts[4] != expected_publisher:
        raise OSError("Unexpected AppModel package identity")
    if parts[3]:
        return None
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[1]):
        raise OSError("Invalid AppModel package version")
    version = tuple(int(part) for part in parts[1].split("."))
    if any(part > 65535 for part in version):
        raise OSError("Invalid AppModel package version")
    return version


def codex_aumid(*, families=CODEX_PACKAGE_FAMILIES, api=None):
    """Return the registered main Codex app ID, or None when not installed.

    Native query failures raise OSError so the caller can use its existing
    fallback. The optional API argument enables isolated tests without touching
    package registration. Only a returned ``!App`` ID is accepted; helper apps
    and framework packages never count as an installed desktop app.
    """
    api = _load_api() if api is None else api
    for family in families:
        if not re.fullmatch(r"OpenAI\.Codex_[a-z0-9]{13}", family):
            raise ValueError("Invalid Codex package family")
        candidates = []
        for full_name in _package_names(api, family):
            version = _version(full_name, family)
            if version is not None:
                candidates.append((version, full_name))
        for _, full_name in sorted(candidates, reverse=True):
            expected = family + "!App"
            if expected in _application_ids(api, full_name):
                return expected
    return None
