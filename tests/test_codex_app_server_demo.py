import argparse
import importlib.util
import pathlib
import sys
import unittest


PATH = pathlib.Path(__file__).parents[1] / "tools" / "codex_app_server_demo.py"
SPEC = importlib.util.spec_from_file_location("codex_app_server_demo", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PlannedMessagesTest(unittest.TestCase):
    def test_wake_resumes_before_starting_turn(self):
        args = argparse.Namespace(action="wake", thread_id="thr_parent", prompt="continue")
        messages = MODULE.planned_messages(args)
        self.assertEqual(messages[0]["method"], "thread/resume")
        self.assertEqual(messages[1]["params"]["threadId"], "thr_parent")

    def test_fork_targets_new_thread(self):
        args = argparse.Namespace(action="fork", thread_id="thr_parent", prompt="work")
        messages = MODULE.planned_messages(args)
        self.assertEqual(messages[0]["method"], "thread/fork")
        self.assertEqual(messages[1]["params"]["threadId"], "<fork-result>")


if __name__ == "__main__":
    unittest.main()
