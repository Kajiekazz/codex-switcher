"""Import and manual login share durable tasks, even before a CLI code exists."""
import json
import threading
import time
import unittest
from unittest import mock

from codex_switcher.login import LoginError, start_device_auth
from test_login import FakeDriver
import test_login as login_fixtures


class LoginWorkflowTests(unittest.TestCase):
    setUp = login_fixtures.LoginTests.setUp
    tearDown = login_fixtures.LoginTests.tearDown
    manager = login_fixtures.LoginTests.manager

    def finish(self, manager, session_id):
        session = manager.get(session_id)
        self.assertTrue(session.done_event.wait(10), session.to_public())
        return session

    def test_startup_failure_is_persisted_with_its_source_and_step(self):
        manager = self.manager(lambda session: FakeDriver())
        with mock.patch("codex_switcher.login.start_device_auth", side_effect=LoginError("device_code_not_found", "fixture early failure")):
            info = manager.enqueue("a@example.com", source="import")
            session = self.finish(manager, info["session"]["id"])
        self.assertEqual(session.status, "failed")
        record = json.loads(manager.history_path.read_text(encoding="utf-8"))[0]
        self.assertEqual((record["source"], record["phase"], record["error"]),
                         ("import", "generating_device_code", "device_code_not_found"))
        self.assertEqual(manager.active_map(), {})

    def test_duplicate_click_during_device_code_generation_attaches_to_the_same_task(self):
        manager = self.manager(lambda session: FakeDriver())
        entered, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return start_device_auth(*args, **kwargs)
        with mock.patch("codex_switcher.login.start_device_auth", side_effect=delayed) as start:
            original = manager.enqueue("a@example.com", source="import")
            self.assertTrue(entered.wait(2))
            duplicate = manager.enqueue("a@example.com")
            self.assertEqual(duplicate["session"]["id"], original["session"]["id"])
            self.assertEqual(duplicate["session"]["status"], "starting")
            release.set()
            self.assertEqual(self.finish(manager, original["session"]["id"]).status, "success")
            self.assertEqual(start.call_count, 1)
        self.assertEqual(len(manager.list()), 1)

    def test_device_code_network_failure_retries_inside_the_same_task(self):
        manager = self.manager(lambda session: FakeDriver())
        calls = []
        def transient(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise LoginError("device_code_not_found", "fixture unavailable", "connection reset")
            return start_device_auth(*args, **kwargs)
        with mock.patch("codex_switcher.login.start_device_auth", side_effect=transient):
            info = manager.enqueue("a@example.com", source="import")
            session = self.finish(manager, info["session"]["id"])
        self.assertEqual(session.status, "success", session.message)
        self.assertEqual(session.attempt, 2)
        self.assertEqual(len(manager.list()), 1)
        self.assertEqual(session.attempt_errors[0]["phase"], "generating_device_code")

    def test_cancelling_a_queued_task_prevents_it_from_starting(self):
        gate = threading.Event()
        entered = threading.Event()
        class Held(FakeDriver):
            def complete_login(self, **kwargs):
                entered.set()
                gate.wait(4)
                super().complete_login(**kwargs)
        manager = self.manager(lambda session: Held())
        manager._concurrency = 1
        self.store.upsert({"email": "b@example.com", "password": "pw", "totp": ""})
        first = manager.enqueue("a@example.com")
        self.assertTrue(entered.wait(3))
        queued = manager.enqueue("b@example.com", source="import")
        self.assertTrue(manager.cancel(queued["session"]["id"]))
        gate.set()
        self.finish(manager, first["session"]["id"])
        self.assertEqual(manager.get(queued["session"]["id"]).status, "cancelled")
        self.assertEqual(manager.queue_info(), [])
        self.assertEqual(manager.get(queued["session"]["id"]).phase, "cancelled")

    def test_transient_page_error_retries_once_without_starting_a_second_cli(self):
        calls = []
        class Transient(FakeDriver):
            def complete_login(self, **kwargs):
                calls.append(kwargs["session"].attempt)
                if len(calls) == 1:
                    raise LoginError("navigation_failed", "fixture temporary disconnect")
                super().complete_login(**kwargs)
        manager = self.manager(lambda session: Transient())
        with mock.patch("codex_switcher.login.start_device_auth", wraps=start_device_auth) as start:
            info = manager.enqueue("a@example.com", source="import")
            session = self.finish(manager, info["session"]["id"])
            self.assertEqual(session.status, "success", session.message)
            self.assertEqual(start.call_count, 1)
        self.assertEqual(calls, [1, 2])

    def test_sms_failure_does_not_restart_login_or_buy_another_number(self):
        calls = []
        class FailedSms(FakeDriver):
            def complete_login(self, **kwargs):
                calls.append(1)
                raise LoginError("sms_timeout", "fixture no SMS")
        manager = self.manager(lambda session: FailedSms())
        info = manager.enqueue("a@example.com", source="import")
        session = self.finish(manager, info["session"]["id"])
        self.assertEqual(session.error, "sms_timeout")
        self.assertEqual(len(calls), 1)

    def test_first_imports_are_serial_even_when_warm_login_concurrency_is_two(self):
        gate, entered = threading.Event(), threading.Event()
        started = []
        class Held(FakeDriver):
            def complete_login(self, **kwargs):
                started.append(kwargs["email"])
                entered.set()
                gate.wait(4)
                super().complete_login(**kwargs)
        manager = self.manager(lambda session: Held())
        manager._concurrency = 2
        self.store.upsert({"email": "b@example.com", "password": "pw", "totp": ""})
        first = manager.enqueue("a@example.com", source="import")
        second = manager.enqueue("b@example.com", source="import")
        self.assertTrue(entered.wait(3))
        time.sleep(.1)
        self.assertEqual(started, ["a@example.com"])
        gate.set()
        self.finish(manager, first["session"]["id"])
        self.finish(manager, second["session"]["id"])
        self.assertEqual(started, ["a@example.com", "b@example.com"])
