from __future__ import annotations

import queue
import unittest

from subagent_bridge.app_server import (
    AppServerDisconnected,
    AppServerResponseError,
    CodexAppServerClient,
)


class MemoryTransport:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.incoming = queue.Queue()
        self.sent = []
        self.closed = False

    def send_json(self, message):
        self.sent.append(message)
        if "id" not in message:
            return
        self.incoming.put(
            {
                "method": "thread/status/changed",
                "params": {"threadId": "t1"},
            }
        )
        if self.fail:
            self.incoming.put(
                {"id": message["id"], "error": {"code": 9, "message": "no"}}
            )
        else:
            self.incoming.put({"id": message["id"], "result": {"ok": True}})

    def receive_json(self):
        value = self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        if not self.closed:
            self.closed = True
            self.incoming.put(AppServerDisconnected("closed"))


class AppServerClientTest(unittest.TestCase):
    def test_request_and_notification_are_demultiplexed(self):
        transport = MemoryTransport()
        client = CodexAppServerClient(transport)
        try:
            result = client.initialize()
            self.assertTrue(result["ok"])
            notification = client.notifications.get(timeout=1)
            self.assertEqual(notification["method"], "thread/status/changed")
            self.assertEqual(transport.sent[-1]["method"], "initialized")
        finally:
            client.close()

    def test_json_rpc_error_is_preserved(self):
        client = CodexAppServerClient(MemoryTransport(fail=True))
        try:
            with self.assertRaises(AppServerResponseError) as caught:
                client.request("failing", {})
            self.assertEqual(caught.exception.error["code"], 9)
        finally:
            client.close()

    def test_server_requests_are_not_answered_implicitly(self):
        transport = MemoryTransport()
        client = CodexAppServerClient(transport)
        try:
            transport.incoming.put(
                {"id": 99, "method": "item/commandExecution/requestApproval", "params": {}}
            )
            request = client.server_requests.get(timeout=1)
            self.assertEqual(request["id"], 99)
            self.assertFalse(any(message.get("id") == 99 for message in transport.sent))
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
