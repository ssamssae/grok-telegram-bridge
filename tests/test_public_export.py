import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1] / "grok_telegram_bridge.py"


def load_bridge(alias, **env):
    """Import the exported bridge with a controlled environment.

    GRB_DRY_RUN keeps it off the real grok binary; GRB_CHAT_ID is supplied
    because the public bridge refuses to start without one - that refusal is
    the behaviour this suite exists to lock.
    """
    base = {"GRB_DRY_RUN": "1", "GRB_CHAT_ID": "111222333"}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=False):
        spec = importlib.util.spec_from_file_location(alias, BRIDGE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[alias] = mod
        spec.loader.exec_module(mod)
    return mod


class PublicExportTest(unittest.TestCase):
    def test_imports_public_bridge(self):
        mod = load_bridge("grok_telegram_bridge")
        self.assertEqual(mod.CHAT_ID, "111222333")
        self.assertEqual(mod.CHAT_LANE, "headless")
        self.assertEqual(mod.TUI_LAUNCHER, "grok-tui-session-start.sh")

    def test_chat_id_has_no_default(self):
        """★이 티켓의 본체 — 공개본에 실 chat_id 기본값이 남으면 안 된다.

        기본값이 있으면 설정을 안 한 사용자의 봇이 남의 채팅으로 답을 보낸다.
        """
        source = BRIDGE.read_text(encoding="utf-8")
        self.assertIn('env("GRB_CHAT_ID", "")', source)
        with mock.patch.dict(os.environ, {"GRB_DRY_RUN": "1"}, clear=False):
            os.environ.pop("GRB_CHAT_ID", None)
            spec = importlib.util.spec_from_file_location("grb_no_chat_id", BRIDGE)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["grb_no_chat_id"] = mod
            with self.assertRaises(SystemExit) as caught:
                spec.loader.exec_module(mod)
        self.assertIn("GRB_CHAT_ID", str(caught.exception))

    def test_state_dir_is_public_path(self):
        mod = load_bridge("grb_state_dir")
        self.assertIn(".grok-telegram-bridge", mod.STATE_DIR)
        # adjacent-literal split keeps the leak sweep itself clean (sister convention)
        self.assertNotIn("." "claude", mod.STATE_DIR)

    def test_delivery_goes_straight_to_the_bot_api(self):
        """공개본은 내부 버스가 아니라 Bot API 직접 호출로 나간다."""
        mod = load_bridge("grb_delivery")
        # adjacent-literal split keeps the leak sweep itself clean (sister convention)
        self.assertFalse(hasattr(mod, "MESH_" "SEND_SH"))
        calls = []
        with mock.patch.object(
            mod,
            "tg",
            lambda method, timeout=60, **params: calls.append((method, params))
            or {"ok": True, "result": {"message_id": 7}},
        ):
            result = mod.deliver_mesh_event("final", "hello")
        self.assertEqual([c[0] for c in calls], ["sendMessage"])
        self.assertEqual(calls[0][1]["chat_id"], "111222333")
        self.assertEqual(calls[0][1]["text"], "hello")
        self.assertEqual(mod._first_sent_message_id(result), 7)

    def test_long_answer_is_chunked_under_the_telegram_cap(self):
        mod = load_bridge("grb_chunking")
        body = "\n".join("line %d" % i for i in range(2000))
        sent = []
        with mock.patch.object(
            mod,
            "tg",
            lambda method, timeout=60, **params: sent.append(params["text"])
            or {"ok": True, "result": {"message_id": len(sent)}},
        ):
            mod.deliver_mesh_event("final", body)
        self.assertGreater(len(sent), 1)
        for chunk in sent:
            self.assertLessEqual(len(chunk), mod.TG_CHUNK)

    def test_empty_body_sends_nothing(self):
        mod = load_bridge("grb_empty_body")
        with mock.patch.object(mod, "tg", lambda *a, **k: self.fail("must not send")):
            self.assertEqual(mod.deliver_mesh_event("final", "   "), {"deliveries": []})

    def test_answer_is_sent_whole(self):
        mod = load_bridge("grb_suggested")
        self.assertEqual(mod.split_suggested_reply("body text"), ("body text", ""))


if __name__ == "__main__":
    unittest.main()
