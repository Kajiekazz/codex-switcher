"""Native package discovery checks registration and bounds every API buffer."""
import ctypes as c
import os
import unittest
from unittest import mock

from codex_switcher import windows_packages as packages


FAMILY = packages.CODEX_PACKAGE_FAMILIES[0]
FULL = "OpenAI.Codex_26.1111.2228.0_x64__2p2nqsd0c76g0"
AUMID = FAMILY + "!App"


def put(pointer, value, typ=c.c_uint32):
    c.cast(pointer, c.POINTER(typ)).contents.value = value


class PackageAPI:
    def __init__(self, entries=None, applications=None):
        self.entries = {FAMILY: [FULL]} if entries is None else entries
        self.applications = {FULL: [AUMID]} if applications is None else applications
        self.references = {}
        self.opened, self.closed = [], []
        self.queried = []
        self.open_error = 0
        self.app_error = 0
        self.close_error = 0

    def GetPackagesByPackageFamily(self, family, count, pointers, chars, buffer):
        self.queried.append(family)
        names = self.entries.get(family, [])
        encoded = [(name + "\0").encode("utf-16-le") for name in names]
        put(count, len(names))
        put(chars, sum(map(len, encoded)) // 2)
        if not names:
            return 0
        if buffer is None:
            return 122
        offset = 0
        for index, value in enumerate(encoded):
            pointers[index] = c.addressof(buffer) + offset
            c.memmove(c.addressof(buffer) + offset, value, len(value))
            offset += len(value)
        return 0

    def OpenPackageInfoByFullName(self, name, reserved, reference):
        if reserved != 0:
            raise AssertionError("reserved must remain zero")
        if self.open_error:
            return self.open_error
        handle = len(self.references) + 100
        self.references[handle] = name
        self.opened.append(handle)
        put(reference, handle, c.c_void_p)
        return 0

    def GetPackageApplicationIds(self, reference, size, buffer, count):
        if self.app_error:
            return self.app_error
        values = self.applications.get(self.references[reference.value], [])
        encoded = [(value + "\0").encode("utf-16-le") for value in values]
        pointer_bytes = len(values) * c.sizeof(c.c_void_p)
        total = pointer_bytes + sum(map(len, encoded))
        put(size, total)
        put(count, len(values))
        if not values:
            return 0
        if buffer is None:
            return 122
        pointers = (c.c_void_p * len(values)).from_buffer(buffer)
        offset = pointer_bytes
        for index, value in enumerate(encoded):
            pointers[index] = c.addressof(buffer) + offset
            c.memmove(c.addressof(buffer) + offset, value, len(value))
            offset += len(value)
        return 0

    def ClosePackageInfo(self, reference):
        self.closed.append(reference.value)
        return self.close_error


class WindowsPackageTests(unittest.TestCase):
    def test_current_user_registration_and_actual_main_application_are_required(self):
        api = PackageAPI()
        self.assertEqual(packages.codex_aumid(api=api), AUMID)
        self.assertEqual(api.opened, api.closed)
        self.assertEqual(api.queried, [FAMILY, FAMILY])

    def test_absent_package_does_not_guess_from_family_or_files(self):
        api = PackageAPI(entries={})
        self.assertIsNone(packages.codex_aumid(api=api))
        self.assertEqual(api.opened, [])

    def test_helpers_without_main_app_do_not_count_as_desktop(self):
        api = PackageAPI(applications={FULL: [FAMILY + "!CodexCoreCommandRunner"]})
        self.assertIsNone(packages.codex_aumid(api=api))
        self.assertEqual(api.opened, api.closed)

    def test_extra_confirmed_family_is_supported(self):
        family = "OpenAI.Codex_123456789abcd"
        full = "OpenAI.Codex_1.2.3.4_arm64__123456789abcd"
        api = PackageAPI(entries={family: [full]}, applications={full: [family + "!App"]})
        self.assertEqual(packages.codex_aumid(families=(family,), api=api), family + "!App")

    def test_framework_and_arbitrary_family_names_are_rejected(self):
        for family in ("OpenAI.CodexPrimaryRuntime_3k8sg7r9htsxt", "Other_123456789abcd", FAMILY + "!App"):
            with self.subTest(family=family), self.assertRaises(ValueError):
                packages.codex_aumid(families=(family,), api=PackageAPI())

    def test_newest_numeric_version_first_and_resource_packages_skipped(self):
        older = "OpenAI.Codex_1.2.0.0_x64__2p2nqsd0c76g0"
        newer = "OpenAI.Codex_1.10.0.0_x64__2p2nqsd0c76g0"
        resource = "OpenAI.Codex_2.0.0.0_neutral_en-us_2p2nqsd0c76g0"
        api = PackageAPI(entries={FAMILY: [older, newer, resource]}, applications={newer: [AUMID]})
        self.assertEqual(packages.codex_aumid(api=api), AUMID)
        self.assertEqual(list(api.references.values()), [newer])

    def test_unrelated_full_name_and_bad_version_are_not_accepted(self):
        for name in ("Other_1.0.0.0_x64__2p2nqsd0c76g0",
                     "OpenAI.Codex_1.0_x64__2p2nqsd0c76g0",
                     "OpenAI.Codex_65536.0.0.0_x64__2p2nqsd0c76g0"):
            with self.subTest(name=name), self.assertRaises(OSError):
                packages.codex_aumid(api=PackageAPI(entries={FAMILY: [name]}))

    def test_uninstall_between_enumeration_and_open_does_not_leave_reference(self):
        api = PackageAPI()
        api.open_error = 1168
        self.assertIsNone(packages.codex_aumid(api=api))
        self.assertEqual(api.closed, [])

    def test_query_error_is_not_reported_as_not_installed(self):
        api = PackageAPI()
        api.GetPackagesByPackageFamily = mock.Mock(return_value=5)
        with self.assertRaises(OSError):
            packages.codex_aumid(api=api)

    def test_open_error_is_not_reported_as_not_installed(self):
        api = PackageAPI()
        api.open_error = 5
        with self.assertRaises(OSError):
            packages.codex_aumid(api=api)
        self.assertEqual(api.closed, [])

    def test_application_error_always_closes_reference(self):
        api = PackageAPI()
        api.app_error = 5
        with self.assertRaises(OSError):
            packages.codex_aumid(api=api)
        self.assertEqual(api.opened, api.closed)

    def test_no_application_ids_closes_reference(self):
        api = PackageAPI(applications={})
        self.assertIsNone(packages.codex_aumid(api=api))
        self.assertEqual(api.opened, api.closed)

    def test_invalid_application_pointer_is_bounded_and_reference_closed(self):
        api = PackageAPI()
        original = api.GetPackageApplicationIds
        def broken(reference, size, buffer, count):
            result = original(reference, size, buffer, count)
            if buffer is not None:
                c.c_void_p.from_buffer(buffer).value = 1
            return result
        api.GetPackageApplicationIds = broken
        with self.assertRaisesRegex(OSError, "pointer"):
            packages.codex_aumid(api=api)
        self.assertEqual(api.opened, api.closed)

    def test_oversized_and_invalid_counts_are_rejected_before_allocating(self):
        for count_value, char_value in ((1025, 100), (1, 2 ** 31), (0, 100)):
            api = PackageAPI()
            def excessive(family, count, pointers, chars, buffer):
                put(count, count_value)
                put(chars, char_value)
                return 122
            api.GetPackagesByPackageFamily = excessive
            with self.subTest(count=count_value, chars=char_value), self.assertRaises(OSError):
                packages.codex_aumid(api=api)

    def test_registration_resize_retries_are_bounded(self):
        api = PackageAPI()
        original = api.GetPackagesByPackageFamily
        calls = []
        def changing(*args):
            calls.append(None)
            original(*args)
            return 122
        api.GetPackagesByPackageFamily = changing
        with self.assertRaisesRegex(OSError, "kept changing"):
            packages.codex_aumid(api=api)
        self.assertEqual(len(calls), 1 + packages._MAX_ATTEMPTS)

    def test_buffer_decoder_rejects_missing_terminator_and_header_pointer(self):
        buffer = c.create_string_buffer(b"a\0b\0", 4)
        with self.assertRaisesRegex(OSError, "Unterminated"):
            packages._buffer_strings(buffer, 4, [c.addressof(buffer)])
        with self.assertRaisesRegex(OSError, "pointer"):
            packages._buffer_strings(buffer, 4, [c.addressof(buffer)], minimum_offset=2)

    def test_api_failures_include_close_failure(self):
        api = PackageAPI()
        api.close_error = 6
        with self.assertRaisesRegex(OSError, "ClosePackageInfo"):
            packages.codex_aumid(api=api)
        self.assertEqual(api.opened, api.closed)

    @unittest.skipUnless(os.name == "nt", "Windows AppModel read-only smoke")
    def test_real_read_only_api_has_native_pointer_width_and_valid_optional_result(self):
        api = packages._load_api()
        self.assertEqual(api.OpenPackageInfoByFullName.argtypes[2], c.POINTER(c.c_void_p))
        self.assertEqual(api.GetPackageApplicationIds.argtypes[2], c.c_void_p)
        value = packages.codex_aumid(api=api)
        self.assertTrue(value is None or value in [family + "!App" for family in packages.CODEX_PACKAGE_FAMILIES])


if __name__ == "__main__":
    unittest.main()
