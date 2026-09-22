from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from subagent_bridge.service import prepare_delegation, show_task
from subagent_bridge.storage import Store


class DelegateServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        self.task_file = self.project / "task.md"
        self.task_file.write_text("perform bounded work")
        self.store = Store(self.state / "bridge.sqlite3")
        self.store.migrate()

    def prepare(self):
        return prepare_delegation(
            self.store,
            state_dir=self.state,
            workspace=self.project,
            task_file=self.task_file,
            context_file=None,
            agent_kind="codex",
            delivery_mode="idle",
            codex_thread_id="thread-1",
            codex_session_id="session-1",
            app_server_instance_id="app-1",
            app_server_socket="/tmp/app.sock",
            app_server_host_id="wsl",
        )

    def test_prepare_creates_bound_task_and_split_attempt_layout(self):
        result = self.prepare()
        task = show_task(self.store, result["task_id"])
        self.assertEqual(task["codex_thread_id"], "thread-1")
        self.assertEqual(task["attempt_status"], "queued")
        self.assertTrue((Path(result["control_path"]) / "task.json").is_file())
        self.assertTrue((Path(result["exchange_path"]) / "inputs/task.md").is_file())
        self.assertEqual(
            self.store.fetchone("SELECT COUNT(*) AS n FROM project_workspaces")["n"], 1
        )

    def test_same_parent_binding_is_reused(self):
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first["parent_id"], second["parent_id"])
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(
            self.store.fetchone("SELECT COUNT(*) AS n FROM parent_sessions")["n"], 1
        )

    def test_changed_session_identity_creates_new_parent_generation(self):
        first = self.prepare()
        second = prepare_delegation(
            self.store,
            state_dir=self.state,
            workspace=self.project,
            task_file=self.task_file,
            context_file=None,
            agent_kind="codex",
            delivery_mode="idle",
            codex_thread_id="thread-1",
            codex_session_id="session-replaced",
            app_server_instance_id="app-1",
            app_server_socket="/tmp/app.sock",
            app_server_host_id="wsl",
        )
        self.assertNotEqual(first["parent_id"], second["parent_id"])
        self.assertEqual(second["parent_generation"], 2)


if __name__ == "__main__":
    unittest.main()
