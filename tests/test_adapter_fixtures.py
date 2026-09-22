from pathlib import Path
import tempfile
import unittest

from subagent_bridge.claude_runner import _parse_claude_events
from subagent_bridge.kimi_runner import _parse_kimi_events
from subagent_bridge.storage import Store


FIXTURES = Path(__file__).parent / "fixtures/agent_events"


class AdapterFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "bridge.sqlite3")
        self.store.migrate()

    def test_claude_success_fixture(self):
        session, payload, errors = _parse_claude_events(
            self.store,
            "attempt-fixture",
            (FIXTURES / "claude_success.jsonl").read_text(),
        )
        self.assertEqual(session, "claude-fixture-session")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(errors, [])

    def test_kimi_success_fixture(self):
        session, payload, errors = _parse_kimi_events(
            self.store,
            "attempt-fixture",
            (FIXTURES / "kimi_success.jsonl").read_text(),
        )
        self.assertEqual(session, "session_fixture")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(errors, [])

    def test_gemini_failure_fixture_is_stable_and_redacted(self):
        message = (FIXTURES / "gemini_auth_failure.txt").read_text()
        self.assertIn("UNSUPPORTED_CLIENT", message)
        self.assertNotIn("@", message)


if __name__ == "__main__":
    unittest.main()
