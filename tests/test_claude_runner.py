from pathlib import Path
import tempfile
import unittest

from subagent_bridge.claude_runner import run_claude_attempt
from subagent_bridge.service import prepare_delegation
from subagent_bridge.storage import Store


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv
schema = json.loads(args[args.index("--json-schema") + 1])
p = schema["properties"]
payload = {
  "schema_version": p["schema_version"]["const"],
  "task_id": p["task_id"]["const"], "attempt_id": p["attempt_id"]["const"],
  "context_version": p["context_version"]["const"], "status": "completed",
  "summary": "Claude adapter completed", "artifacts": [],
  "verification": {"performed": ["fake protocol"], "limitations": []},
  "needs_parent": []}
print(json.dumps({"type":"system","subtype":"init","session_id":"claude-session-1"}))
print(json.dumps({"type":"system","subtype":"thinking_tokens","estimated_tokens":2}))
print(json.dumps({"type":"result","subtype":"success","is_error":False,
                  "session_id":"claude-session-1","structured_output":payload}))
'''


class ClaudeRunnerTest(unittest.TestCase):
    def test_structured_result_is_frozen_and_delivered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            task_file = root / "task.md"
            task_file.write_text("Perform a deterministic check.")
            fake = root / "claude"
            fake.write_text(FAKE_CLAUDE)
            fake.chmod(0o700)
            store = Store(root / "state/bridge.sqlite3")
            store.migrate()
            prepared = prepare_delegation(
                store, state_dir=root / "state", workspace=root,
                task_file=task_file, context_file=None, agent_kind="claude",
                delivery_mode="idle", codex_thread_id="parent",
                codex_session_id="session", app_server_instance_id="app",
                app_server_socket="/tmp/app.sock",
            )
            result = run_claude_attempt(
                store, prepared["attempt_id"], executable=str(fake),
                environ={"PATH": "/usr/bin"},
            )
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["native_session_id"], "claude-session-1")
            self.assertEqual(result["delivery"]["status"], "pending")
            with store.connect() as connection:
                events = connection.execute(
                    "SELECT payload_json FROM observations ORDER BY sampled_at"
                ).fetchall()
            self.assertEqual(len(events), 2)


if __name__ == "__main__":
    unittest.main()
