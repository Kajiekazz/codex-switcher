import base64
import json
import tempfile
import unittest
from pathlib import Path

from codex_switcher.accounts import (
    mask_email,
    normalize_totp_secret,
    parse_accounts,
    totp_code,
)
from codex_switcher.auth_state import read_auth_state
from codex_switcher.dpapi import protect, unprotect


def b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class DpapiTests(unittest.TestCase):
    def test_roundtrip(self):
        plaintext = b"hello-codex-switcher-\x00-\xff"
        ciphertext = protect(plaintext)
        self.assertNotEqual(plaintext, ciphertext)
        self.assertNotIn(b"hello-codex-switcher", ciphertext)
        self.assertEqual(unprotect(ciphertext), plaintext)


class AccountParserTests(unittest.TestCase):
    def test_dash_format(self):
        text = "a@example.com-----pass123---JBSWY3DPEHPK3PXP"
        accounts, errors = parse_accounts(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["email"], "a@example.com")
        self.assertEqual(accounts[0]["password"], "pass123")
        self.assertEqual(accounts[0]["totp"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(accounts[0]["totp_format"], "base32")

    def test_pipe_format(self):
        text = "b@example.com|pass|JBSWY3DPEHPK3PXP"
        accounts, errors = parse_accounts(text)
        self.assertEqual(errors, [])
        self.assertEqual(accounts[0]["password"], "pass")
        self.assertEqual(accounts[0]["totp"], "JBSWY3DPEHPK3PXP")

    def test_variable_dash_counts(self):
        pairs = [(1, 1), (2, 2), (3, 2), (4, 3), (5, 4), (6, 2), (8, 5), (10, 3)]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                text = (
                    f"v@example.com{'-' * left}pass123"
                    f"{'-' * right}JBSWY3DPEHPK3PXP"
                )
                accounts, errors = parse_accounts(text)
                self.assertEqual(errors, [])
                self.assertEqual(accounts[0]["email"], "v@example.com")
                self.assertEqual(accounts[0]["password"], "pass123")
                self.assertEqual(accounts[0]["totp"], "JBSWY3DPEHPK3PXP")
                self.assertEqual(accounts[0]["totp_format"], "base32")

    def test_password_with_dashes_is_preserved(self):
        text = "d@example.com----my--pass-1----JBSWY3DPEHPK3PXP"
        accounts, errors = parse_accounts(text)
        self.assertEqual(errors, [])
        self.assertEqual(accounts[0]["password"], "my--pass-1")
        self.assertEqual(accounts[0]["totp"], "JBSWY3DPEHPK3PXP")

    def test_dash_format_without_totp(self):
        accounts, errors = parse_accounts("n@example.com-----pass123")
        self.assertEqual(errors, [])
        self.assertEqual(accounts[0]["password"], "pass123")
        self.assertEqual(accounts[0]["totp"], "")
        self.assertEqual(accounts[0]["totp_format"], "none")

    def test_trailing_separator_means_empty_totp(self):
        accounts, errors = parse_accounts("t@example.com----pass123----")
        self.assertEqual(errors, [])
        self.assertEqual(accounts[0]["password"], "pass123")
        self.assertEqual(accounts[0]["totp"], "")

    def test_grouped_totp_secret(self):
        text = "g@example.com----pass123----JBSW-Y3DP-EHPK-3PXP"
        accounts, errors = parse_accounts(text)
        self.assertEqual(errors, [])
        self.assertEqual(accounts[0]["password"], "pass123")
        self.assertEqual(accounts[0]["totp"], "JBSWY3DPEHPK3PXP")

    def test_email_with_dashes(self):
        text = "xn--test@example.com----pass123----JBSWY3DPEHPK3PXP"
        accounts, errors = parse_accounts(text)
        self.assertEqual(errors, [])
        self.assertEqual(accounts[0]["email"], "xn--test@example.com")
        self.assertEqual(accounts[0]["password"], "pass123")
        self.assertEqual(accounts[0]["totp"], "JBSWY3DPEHPK3PXP")

    def test_comments_and_blank_lines(self):
        text = "\n# comment\na@example.com-----p---JBSWY3DPEHPK3PXP\n"
        accounts, errors = parse_accounts(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(accounts), 1)

    def test_invalid_email_and_empty_password(self):
        text = "not-an-email-----p---JBSWY3DPEHPK3PXP\nx@example.com--------"
        accounts, errors = parse_accounts(text)
        self.assertEqual(accounts, [])
        self.assertGreaterEqual(len(errors), 1)

    def test_mask_email(self):
        self.assertEqual(mask_email("someone+tag@example.com"), "so***@example.com")
        self.assertEqual(mask_email("a@b.com"), "a***@b.com")

    def test_otpauth_url(self):
        value = normalize_totp_secret(
            "otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP&issuer=Test"
        )
        self.assertEqual(value, "JBSWY3DPEHPK3PXP")


class TotpTests(unittest.TestCase):
    def test_rfc6238_vector(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # ASCII 12345678901234567890
        self.assertEqual(totp_code(secret, at=59, digits=8), "94287082")
        self.assertEqual(totp_code(secret, at=59, digits=6), "287082")

    def test_invalid_secret(self):
        with self.assertRaises(ValueError):
            totp_code("not-base32!!!", at=59)


class AuthStateTests(unittest.TestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = read_auth_state(Path(tmp) / "auth.json")
        self.assertFalse(state["logged_in"])
        self.assertEqual(state["reason"], "auth_file_missing")

    def test_fake_auth_file(self):
        header = b64url({"alg": "none", "typ": "JWT"})
        payload = b64url(
            {
                "email": "x@example.com",
                "name": "X",
                "exp": 4102444800,
                "https://api.openai.com/auth": {
                    "chatgpt_plan_type": "plus",
                    "chatgpt_account_id": "acct-1",
                    "chatgpt_user_id": "user-1",
                },
            }
        )
        token = f"{header}.{payload}.sig"
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": token,
                            "access_token": token,
                            "account_id": "acct-1",
                        },
                        "last_refresh": "2026-10-30T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            state = read_auth_state(auth_path)
        self.assertTrue(state["logged_in"])
        self.assertEqual(state["email"], "x@example.com")
        self.assertEqual(state["plan"], "plus")
        self.assertEqual(state["account_id"], "acct-1")
        self.assertFalse(state["expired"])


if __name__ == "__main__":
    unittest.main()
