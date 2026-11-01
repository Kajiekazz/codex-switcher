"""Protocol reader tests using in-memory pipes, never a real app-server."""

import io
import json
import threading
import unittest
from collections import deque
from concurrent.futures import Future
from types import SimpleNamespace

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
