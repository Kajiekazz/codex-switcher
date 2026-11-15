"""Loopback binding and discovery; no live service or account access."""
import errno
import json
import os
import socket
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

from codex_switcher.api import create_server, _existing_instance


class ServicePortTests(unittest.TestCase):
    def create(self, port, auto_port=True):
        class Handler(BaseHTTPRequestHandler):
            desktop_display_cache = mock.Mock()
        self.quota = mock.Mock()
        with mock.patch("codex_switcher.api.make_handler", return_value=Handler):
            return create_server(mock.Mock(), mock.Mock(), "127.0.0.1", port,
                                 login_manager=mock.Mock(), switch_manager=mock.Mock(),
                                 quota_service=self.quota, auto_switch=mock.Mock(),
                                 desktop_client=mock.Mock(), goal_resume=mock.Mock(), auto_port=auto_port)

    def test_occupied_port_selects_another_listener_without_closing_dependencies(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            preferred = occupied.getsockname()[1]
            server = self.create(preferred)
            try:
                self.assertNotEqual(server.server_address[1], preferred)
                self.quota.close.assert_not_called()
                with socket.create_connection(server.server_address, timeout=1):
                    pass
            finally:
                server.server_close()
            self.quota.close.assert_called_once()

    def test_exhausted_discovery_range_uses_os_assigned_port(self):
        original = socket.socket.bind
        attempted = []

        def bind(sock, address):
            attempted.append(address[1])
            if address[1]:
                raise OSError(errno.EADDRINUSE, "fixture occupied")
            return original(sock, address)

        with mock.patch.object(socket.socket, "bind", bind):
            server = self.create(8787)
        try:
            self.assertEqual(attempted, list(range(8787, 8798)) + [0])
            self.assertGreater(server.server_address[1], 0)
            self.quota.close.assert_not_called()
        finally:
            server.server_close()

    @unittest.skipUnless(os.name == "nt", "Windows exclusive listener")
    def test_listener_rejects_second_bind_even_if_caller_enables_reuse(self):
        server = self.create(0)
        try:
            with socket.socket() as second:
                second.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with self.assertRaises(OSError):
                    second.bind(server.server_address)
        finally:
            server.server_close()

    def test_unrelated_service_health_is_not_mistaken_for_switcher(self):
        with mock.patch("urllib.request.build_opener") as factory:
            response = factory.return_value.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps({"ok": True, "version": "foreign"}).encode()
            self.assertIsNone(_existing_instance("127.0.0.1", 8787))
            response.read.return_value = json.dumps({"ok": True, "service": "codex-switcher"}).encode()
            self.assertEqual(_existing_instance("127.0.0.1", 8787)["service"], "codex-switcher")

    @unittest.skipUnless(os.name == "nt", "Windows shared service lock")
    def test_foreground_service_cannot_start_while_launcher_owns_data_directory(self):
        from codex_switcher.api import run
        with mock.patch("codex_switcher.application.instance_name", return_value="fixture-shared-name") as name, mock.patch(
                "codex_switcher.windows_ui.SingleInstance") as mutex, mock.patch(
                "codex_switcher.api._run_service") as start, mock.patch("builtins.print"):
            mutex.return_value.acquired = False
            run()
        name.assert_called_once()
        mutex.assert_called_once_with("fixture-shared-name")
        mutex.return_value.close.assert_called_once()
        start.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows shared service lock")
    def test_foreground_service_holds_lock_until_service_finishes(self):
        from codex_switcher.api import run
        events = []
        with mock.patch("codex_switcher.application.instance_name", return_value="fixture-shared-name"), mock.patch(
                "codex_switcher.windows_ui.SingleInstance") as mutex, mock.patch(
                "codex_switcher.api._run_service", side_effect=lambda: events.append("serve")):
            mutex.return_value.acquired = True
            mutex.return_value.close.side_effect = lambda: events.append("release")
            run()
        self.assertEqual(events, ["serve", "release"])
