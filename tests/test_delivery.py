from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from subagent_bridge.app_server import AppServerDisconnected, AppServerResponseError
from subagent_bridge.delivery import acknowledge, create_delivery, dispatch_one, recover_dispatching
from subagent_bridge.service import prepare_delegation
from subagent_bridge.storage import Store


class FakeClient:
    def __init__(self, *, thread_status="idle", turn_result=None, turn_error=None):
        self.thread_status = thread_status
        self.turn_result = turn_result or {"turn": {"id": "turn-1"}}
        self.turn_error = turn_error
        self.calls = []

    def request(self, method, params=None, timeout=15):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": {"id": "thread-1", "status": {"type": self.thread_status}}}
        if self.turn_error:
            raise self.turn_error
        return self.turn_result


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        task_file = self.project / "task.md"
        task_file.write_text("do work")
        self.store = Store(self.root / "state/bridge.sqlite3")
        self.store.migrate()
        prepared = prepare_delegation(
            self.store,
            state_dir=self.root / "state",
            workspace=self.project,
            task_file=task_file,
            context_file=None,
            agent_kind="codex",
            delivery_mode="idle",
            codex_thread_id="thread-1",
            codex_session_id="session-1",
            app_server_instance_id="app-1",
            app_server_socket="/tmp/app.sock",
        )
        self.task_id = prepared["task_id"]
        self.attempt_id = prepared["attempt_id"]
        self.parent_id = prepared["parent_id"]
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE attempts SET status='done',result_path=?,result_hash=?,result_status=? "
                "WHERE attempt_id=?",
                ("/frozen/result.json", "hash-1", "completed", self.attempt_id),
            )

    def create(self):
        return create_delivery(
            self.store, task_id=self.task_id, attempt_id=self.attempt_id
        )

    def test_create_is_deduplicated(self):
        first = self.create()
        second = self.create()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["delivery_id"], second["delivery_id"])

    def test_idle_delivery_starts_turn_and_can_be_acknowledged(self):
        delivery = self.create()
        result = dispatch_one(self.store, FakeClient(), delivery["delivery_id"])
        self.assertEqual(result["status"], "submitted")
        ack = acknowledge(
            self.store,
            delivery_id=delivery["delivery_id"],
            receipt=delivery["receipt"],
            parent_id=self.parent_id,
        )
        self.assertEqual(ack["status"], "acknowledged")
        repeated = acknowledge(
            self.store,
            delivery_id=delivery["delivery_id"],
            receipt=delivery["receipt"],
            parent_id=self.parent_id,
        )
        self.assertTrue(repeated["idempotent"])

    def test_busy_parent_keeps_delivery_pending(self):
        delivery = self.create()
        client = FakeClient(thread_status="active")
        result = dispatch_one(self.store, client, delivery["delivery_id"])
        self.assertEqual(result["status"], "pending")
        self.assertEqual([call[0] for call in client.calls], ["thread/read"])

    def test_immediate_delivery_steers_exact_active_turn(self):
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE tasks SET delivery_mode='immediate' WHERE task_id=?",
                (self.task_id,),
            )
        delivery = self.create()
        client = FakeClient(
            thread_status="active", turn_result={"turnId": "turn-active"}
        )
        result = dispatch_one(
            self.store,
            client,
            delivery["delivery_id"],
            expected_turn_id="turn-active",
        )
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(client.calls[-1][0], "turn/steer")
        self.assertEqual(client.calls[-1][1]["expectedTurnId"], "turn-active")

    def test_explicit_rejection_requeues_but_disconnect_becomes_unknown(self):
        rejected = self.create()
        result = dispatch_one(
            self.store,
            FakeClient(turn_error=AppServerResponseError({"code": 1, "message": "busy"})),
            rejected["delivery_id"],
        )
        self.assertEqual(result["status"], "pending")
        result = dispatch_one(
            self.store,
            FakeClient(turn_error=AppServerDisconnected("lost")),
            rejected["delivery_id"],
        )
        self.assertEqual(result["status"], "delivery_unknown")
        again = dispatch_one(self.store, FakeClient(), rejected["delivery_id"])
        self.assertEqual(again["status"], "delivery_unknown")

    def test_recovery_never_requeues_abandoned_dispatch(self):
        delivery = self.create()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE deliveries SET status='dispatching' WHERE delivery_id=?",
                (delivery["delivery_id"],),
            )
        self.assertEqual(recover_dispatching(self.store), [delivery["delivery_id"]])
        row = self.store.fetchone(
            "SELECT status FROM deliveries WHERE delivery_id=?", (delivery["delivery_id"],)
        )
        self.assertEqual(row["status"], "delivery_unknown")


if __name__ == "__main__":
    unittest.main()
