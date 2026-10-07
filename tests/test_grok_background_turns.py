"""Background completions must not inherit an unrelated human question."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


BRIDGE = Path(__file__).resolve().parents[1] / "grok-telegram-bridge.py"
if not BRIDGE.exists():
    BRIDGE = Path(__file__).resolve().parents[1] / "grok_telegram_bridge.py"


def user(text):
    return {"type": "user", "content": [{"type": "text", "text": f"<user_query>{text}</user_query>"}]}


def answer(text):
    return {"type": "assistant", "content": text}


def completion():
    return {"type": "user", "content": [{"type": "text", "text": (
        '<system-reminder>\nBackground task "test-task" completed (exit code: 1).\n'
        'Description: Check an earlier change | Duration: 600s\n</system-reminder>'
    )}]}


class BackgroundTurnsTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        token = self.root / "token.json"
        token.write_text(json.dumps({"api_key": "TESTONLY-fake"}))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "GRB_TOKEN_FILE": str(token), "GRB_NAME": "test", "GRB_CHAT_ID": "4321",
            "GRB_STATE_DIR": str(self.root), "GRB_GROK_SESSIONS_DIR": str(self.root / "sessions"),
            "GRB_GROK_CHAT_CWD": str(self.root), "GRB_TUI_SESSION_ID": "test-session",
            "GRB_CHAT_LANE": "tui", "GRB_TUI_MIRROR_LOCAL": "1", "GRB_LANGUAGE": "en",
            "GRB_DRY_RUN": "0", "GRB_STDIN_INPUT": "0", "GRB_LOCAL_INPUT": "0",
        }))
        self.stack.enter_context(mock.patch.object(sys, "path", [str(BRIDGE.parent), *sys.path]))
        self.bridge = {"__file__": str(BRIDGE), "__name__": "background_turns_test"}
        exec(compile(BRIDGE.read_text(), str(BRIDGE), "exec"), self.bridge)
        self.events = []
        self.goals = []
        self.bridge["deliver_mesh_event"] = lambda kind, body, **kw: self.events.append((kind, body)) or {}
        self.bridge["tui_follow_session_rotation"] = lambda: ""
        self.bridge["_observe_local_progress"] = lambda *args: None
        self.bridge["_tui_cursor_save"]("test-session", 0)

    def history(self, rows, sent=0):
        path = Path(self.bridge["tui_history_path"]())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.bridge["_tui_cursor_save"]("test-session", sent)
        return path

    def assert_background_only(self):
        self.assertEqual(len(self.events), 1, self.events)
        self.assertEqual(self.events[0][0], "final")
        self.assertIn("Background task result", self.events[0][1])
        self.assertTrue(self.events[0][1].endswith("Earlier check failed."))
        self.assertNotIn("Check my worklog", self.events[0][1])

    def test_late_result_does_not_replay_previous_question(self):
        self.history([user("Check my worklog"), answer("Worklog checked."), completion(), answer("Earlier check failed.")], sent=1)
        self.assertEqual(self.bridge["mirror_local_tui_turns"](), 1)
        self.assert_background_only()
        self.events.clear()
        self.assertEqual(self.bridge["mirror_local_tui_turns"](), 0)
        self.assertEqual(self.events, [])

    def test_restart_harvest_keeps_background_origin(self):
        self.history([user("Check my worklog"), answer("Worklog checked."), completion(), answer("Earlier check failed.")], sent=1)
        self.assertEqual(self.bridge["harvest_orphaned_tui_finals"](), 1)
        self.assert_background_only()
        self.events.clear()
        self.assertEqual(self.bridge["mirror_local_tui_turns"](), 0)
        self.assertEqual(self.events, [])

    def test_completion_without_previous_user_is_still_delivered(self):
        self.history([completion(), answer("Earlier check failed.")])
        self.assertEqual(self.bridge["mirror_local_tui_turns"](), 1)
        self.assert_background_only()

    def test_next_human_request_has_its_own_answer(self):
        self.history([user("Check my worklog"), answer("Worklog checked."), completion(), answer("Earlier check failed."), user("New request"), answer("New answer")], sent=2)
        self.assertEqual(self.bridge["mirror_local_tui_turns"](), 1)
        self.assertEqual(len(self.events), 2)
        self.assertIn("New request", self.events[0][1])
        self.assertEqual(self.events[1], ("final", "New answer"))

    def test_user_quoting_completion_text_remains_a_user(self):
        quoted = completion()["content"][0]["text"]
        self.history([user(quoted), answer("This explains your quote.")])
        self.assertEqual(self.bridge["mirror_local_tui_turns"](), 1)
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[-1], ("final", "This explains your quote."))

    def test_context_reminder_does_not_cut_off_human_answer(self):
        reminder = {"type": "user", "synthetic_reason": "system_reminder", "content": "<system-reminder>Keep the context.</system-reminder>"}
        self.history([user("Current question"), reminder, answer("Current answer")])
        self.assertEqual(self.bridge["mirror_local_tui_turns"](), 1)
        self.assertIn("Current question", self.events[0][1])
        self.assertEqual(self.events[-1], ("final", "Current answer"))


    def test_foreground_wait_does_not_accept_background_final(self):
        path = self.history([user("Check my worklog"), completion(), answer("Earlier check failed.")])
        self.bridge.update(TUI_IDLE_TIMEOUT=0, TUI_LATE_HARVEST_GRACE=0, TUI_RESCUE_ON_ROTATION=False)
        self.bridge["_tui_turn_dead_confirmed"] = lambda: False
        self.bridge["tui_rotation_diagnosis"] = lambda: ""
        with self.assertRaises(self.bridge["GrokExecError"]):
            self.bridge["_tui_wait_for_final"](str(path), 0, request_text="Check my worklog")

    def test_rotation_rescue_does_not_accept_background_final(self):
        self.history([user("Check my worklog"), completion(), answer("Earlier check failed.")])
        self.bridge["tui_session_rotation"] = lambda: {"verdict": "rotated", "live_mtime": 10}
        self.bridge["tui_follow_session_rotation"] = lambda: "test-session"
        self.bridge["TUI_RESCUE_ON_ROTATION"] = True
        text, moved = self.bridge["_tui_rescue_after_rotation"]("Check my worklog", 1)
        self.assertTrue(moved)
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()
