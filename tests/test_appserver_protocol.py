"""Protocol and owned-child cleanup tests, never a real Codex app-server."""

import io
import json
import socket
import subprocess
import sys
import threading
import unittest
from collections import deque
from concurrent.futures import Future
from types import SimpleNamespace
from unittest import mock

from codex_switcher.appserver import AppServerSession, SERVER_REQUEST_REJECTED


class AppServerProtocolTests(unittest.TestCase):
    def session_with_messages(self, messages):
        # Deliberately bypass the process-spawning constructor. Only the
        # protocol reader and its in-memory transport are exercised here.
        session = AppServerSession.__new__(AppServerSession)
        session._pending = {}
        session._notifications = []
        session._cond = threading.Condition()
        session._write_lock = threading.Lock()
        session._closed = False
        session._server_requests = 0
        session._stderr_tail = deque(maxlen=60)
        session.proc = SimpleNamespace(
            stdout=io.StringIO("\n".join(json.dumps(message) for message in messages) + "\n"),
            stdin=io.StringIO(),
        )
        return session

    def test_eof_still_cleans_up_owned_child_and_releases_its_port(self):
        # A synthetic child holds an ephemeral port after closing its RPC
        # output. No Codex executable, account, or fixed OAuth port is used.
        script = (
            "import json,os,socket,time\n"
            "listener=socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen()\n"
            "print(json.dumps({'method':'fixture/ready','port':listener.getsockname()[1]}),flush=True)\n"
            "os.close(1); time.sleep(20)\n"
        )
        session = self.session_with_messages([])
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", script], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        session.proc = child
        reader = threading.Thread(target=session._read_stdout, daemon=True)
        try:
            reader.start()
            reader.join(5)
            self.assertFalse(reader.is_alive(), "fixture must close its RPC output")
            self.assertTrue(session._closed)
            self.assertIsNone(child.poll(), "EOF must precede process exit")
            notice = session.wait_notification(lambda message: True, timeout=0)
            self.assertEqual(notice["method"], "fixture/ready")
            address = ("127.0.0.1", notice["port"])
            with socket.create_connection(address, timeout=1):
                pass
            with mock.patch.object(child, "terminate", wraps=child.terminate) as terminate:
                session.close()
                self.assertIsNotNone(child.poll(), "close must reap the child even after EOF")
                session.close()
                terminate.assert_called_once()
            with socket.socket() as probe:
                probe.settimeout(1)
                self.assertNotEqual(probe.connect_ex(address), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(5)
            reader.join(5)
            child.stdin.close()
            child.stdout.close()

    def test_rejected_server_request_exposes_only_routing_metadata(self):
        session = self.session_with_messages([{
            "jsonrpc": "2.0", "id": 7, "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1",
                "questions": [{"question": "PRIVATE_PROMPT"}],
            },
        }])
        session._read_stdout()
        reply = json.loads(session.proc.stdin.getvalue())
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["error"]["code"], -32601)
        event = session.wait_notification(lambda event: True, timeout=0)
        self.assertEqual(event, {"method": SERVER_REQUEST_REJECTED, "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "requestMethod": "item/tool/requestUserInput",
        }})
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(event))

    def test_request_diagnostic_does_not_consume_rpc_response(self):
        session = self.session_with_messages([
            {"id": "server-7", "method": "account/chatgptAuthTokens/refresh", "params": {}},
            {"id": 8, "result": {"goal": {"status": "active"}}},
        ])
        future = Future()
        session._pending[8] = future
        session._read_stdout()
        self.assertEqual(future.result(), {"id": 8, "result": {"goal": {"status": "active"}}})
        event = session.wait_notification(lambda event: True, timeout=0)
        self.assertEqual(event["method"], SERVER_REQUEST_REJECTED)
        self.assertIsNone(event["params"]["threadId"])
        self.assertEqual(session._server_requests, 1)

    def test_initialize_requests_the_experimental_api_capability(self):
        # ``thread/queue/list`` — how a queued resume message is confirmed as
        # taken by the desktop app — is refused without this capability.
        session = AppServerSession.__new__(AppServerSession)
        seen = {}

        def request(method, params=None, timeout=30.0):
            seen.update(method=method, params=params, timeout=timeout)
            return {"ok": True}

        session.request = request
        session.initialize()
        self.assertEqual(seen["method"], "initialize")
        self.assertEqual(seen["params"]["capabilities"], {"experimentalApi": True})
        self.assertEqual(seen["params"]["clientInfo"]["name"], "codex-switcher")
