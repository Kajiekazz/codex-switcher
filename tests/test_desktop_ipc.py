"""Coordination framing and exact-turn stop validation, without a desktop."""
import json
import struct
import unittest

from codex_switcher.desktop_ipc import DesktopControl, DesktopIpcError


class FakePipe:
    def __init__(self, interrupt=None, owner_error=None):
        self.buffer, self.messages, self.closed = bytearray(), [], False
        self.interrupt_result = interrupt if interrupt is not None else {"ok": True, "interruptedTurnId": "turn"}
        self.owner_error = owner_error

    def write(self, data):
        size = struct.unpack("<I", data[:4])[0]
        assert size == len(data) - 4
        message = json.loads(data[4:])
        self.messages.append(message)
        if message["type"] == "client-discovery-response":
            return
        result = {"initialize": {"clientId": "switcher"}, "thread-owner-discovery": {"supportsUntrustedAppInput": True},
                  "thread-follower-interrupt-turn": self.interrupt_result}[message["method"]]
        reply = {"type": "response", "requestId": message["requestId"], "resultType": "success",
                 "handledByClientId": "actual-owner", "result": result}
        if self.owner_error and message["method"] == "thread-owner-discovery":
            reply.update(resultType="error", error=self.owner_error)
        self.enqueue({"type": "client-discovery-request", "requestId": "foreign"})
        self.enqueue({"type": "broadcast", "method": "ignore-this"})
        self.enqueue(reply)

    def enqueue(self, value):
        data = json.dumps(value).encode()
        self.buffer.extend(struct.pack("<I", len(data)) + data)

    def read(self, size, deadline):
        assert len(self.buffer) >= size
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def close(self):
        self.closed = True


class DesktopControlTests(unittest.TestCase):
    def test_stop_discovers_owner_and_targets_only_the_confirmed_turn(self):
        pipe = FakePipe()
        result = DesktopControl(lambda: pipe).interrupt("chat", "turn")
        requests = [row for row in pipe.messages if row["type"] == "request"]
        self.assertEqual([row["method"] for row in requests], ["initialize", "thread-owner-discovery", "thread-follower-interrupt-turn"])
        self.assertEqual(requests[-1]["params"], {"conversationId": "chat", "mode": "user-stop", "expectedTurnId": "turn"})
        self.assertEqual(requests[-1]["targetClientId"], "actual-owner")
        self.assertEqual(requests[-1]["version"], 4)
        self.assertEqual(result, {"stopped": True, "turn_id": "turn", "evidence": "desktop_ipc"})
        self.assertTrue(pipe.closed)
        self.assertTrue(all(row["response"] == {"canHandle": False} for row in pipe.messages if row["type"] == "client-discovery-response"))

    def test_missing_owner_never_falls_back_to_broadcasting_an_interrupt(self):
        pipe = FakePipe(owner_error="no-client-found")
        with self.assertRaisesRegex(DesktopIpcError, "no-client-found"):
            DesktopControl(lambda: pipe).interrupt("chat", "turn")
        self.assertFalse(any(row.get("method") == "thread-follower-interrupt-turn" for row in pipe.messages))
        self.assertTrue(pipe.closed)

    def test_a_different_or_absent_interrupted_turn_is_not_a_success(self):
        for result in ({"ok": True, "interruptedTurnId": "other"}, {"ok": True, "interruptedTurnId": None}, {"ok": False}):
            with self.subTest(result=result):
                pipe = FakePipe(interrupt=result)
                with self.assertRaisesRegex(DesktopIpcError, "desktop_stop_not_confirmed"):
                    DesktopControl(lambda: pipe).interrupt("chat", "turn")
                self.assertTrue(pipe.closed)

    def test_missing_turn_does_not_connect(self):
        def forbidden():
            self.fail("must not connect")
        with self.assertRaisesRegex(DesktopIpcError, "confirmed_turn_required"):
            DesktopControl(forbidden).interrupt("chat", "")
