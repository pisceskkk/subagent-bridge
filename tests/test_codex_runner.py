import json
import stat
from pathlib import Path
import tempfile
import unittest

from subagent_bridge.codex_runner import run_codex_attempt
from subagent_bridge.service import prepare_delegation
from subagent_bridge.storage import Store


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
output = pathlib.Path(args[args.index("--output-last-message") + 1])
props = schema["properties"]
payload = {
  "schema_version": props["schema_version"]["const"],
  "task_id": props["task_id"]["const"],
  "attempt_id": props["attempt_id"]["const"],
  "context_version": props["context_version"]["const"],
  "status": "completed", "summary": "real child protocol exercised",
  "artifacts": [],
  "verification": {"performed": ["parent env stripped=" + str("CODEX_THREAD_ID" not in os.environ)], "limitations": []},
  "needs_parent": [],
}
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "bad-result": payload["task_id"] = "wrong"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(payload))
print(json.dumps({"type":"thread.started","thread_id":"native-child-1"}), flush=True)
if mode == "bad-jsonl": print("not-json", flush=True)
print(json.dumps({"type":"turn.started"}), flush=True)
if mode != "missing-complete": print(json.dumps({"type":"turn.completed"}), flush=True)
'''


class CodexRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.state = self.root / "state"
        self.store = Store(self.state / "bridge.sqlite3")
        self.store.migrate()
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE_CODEX)
        self.fake.chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        task = self.root / "task.md"
        task.write_text("Return a short deterministic result.")
        return prepare_delegation(
            self.store,
            state_dir=self.state,
            workspace=self.root,
            task_file=task,
            context_file=None,
            agent_kind="codex",
            delivery_mode="idle",
            codex_thread_id="parent-thread",
            codex_session_id="parent-session",
            app_server_instance_id="app-test",
            app_server_socket="/tmp/test.sock",
        )

    def observations(self, attempt_id):
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT payload_json FROM observations WHERE subject_id=? ORDER BY sampled_at",
                (attempt_id,),
            ).fetchall()

    def test_success_tracks_native_session_freezes_result_and_queues_delivery(self):
        item = self.prepare()
        result = run_codex_attempt(
            self.store,
            item["attempt_id"],
            executable=str(self.fake),
            environ={"PATH": "/usr/bin", "CODEX_THREAD_ID": "must-not-leak"},
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["native_thread_id"], "native-child-1")
        self.assertEqual(result["delivery"]["status"], "pending")
        row = self.store.fetchone(
            "SELECT a.status,a.submission_status,a.result_hash,s.native_ref_json "
            "FROM attempts a JOIN agent_sessions s ON s.session_id=a.session_id WHERE a.attempt_id=?",
            (item["attempt_id"],),
        )
        self.assertEqual((row["status"], row["submission_status"]), ("done", "submitted"))
        self.assertIn("native-child-1", row["native_ref_json"])
        frozen = json.loads((Path(item["control_path"]) / "result.collected.json").read_text())
        self.assertEqual(frozen["verification"]["performed"], ["parent env stripped=True"])
        for name in ("codex.stdout.jsonl", "codex.stderr.log"):
            mode = (Path(item["control_path"]) / name).stat().st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o600)
        event_types = [json.loads(row["payload_json"])["type"] for row in self.observations(item["attempt_id"])]
        self.assertEqual(event_types, ["thread.started", "turn.started", "turn.completed"])

    def test_invalid_jsonl_is_preserved_and_fails_attempt(self):
        item = self.prepare()
        result = run_codex_attempt(
            self.store, item["attempt_id"], executable=str(self.fake),
            environ={"PATH": "/usr/bin", "FAKE_MODE": "bad-jsonl"},
        )
        self.assertEqual(result["status"], "failed")
        events = [json.loads(row["payload_json"]) for row in self.observations(item["attempt_id"])]
        self.assertIn("bridge.invalid_jsonl", [event["type"] for event in events])

    def test_missing_completion_event_fails_closed(self):
        item = self.prepare()
        result = run_codex_attempt(
            self.store, item["attempt_id"], executable=str(self.fake),
            environ={"PATH": "/usr/bin", "FAKE_MODE": "missing-complete"},
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("missing turn.completed", result["error"])

    def test_identity_mismatch_in_model_output_is_rejected(self):
        item = self.prepare()
        result = run_codex_attempt(
            self.store, item["attempt_id"], executable=str(self.fake),
            environ={"PATH": "/usr/bin", "FAKE_MODE": "bad-result"},
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("task_id", result["error"])


if __name__ == "__main__":
    unittest.main()
