# @mutate-why 틱에서 동기화가 빠지면 새 카드·만료가 있어도 고정·해제가 일어나지 않는다.
# @mutate scripts/grok-telegram-bridge.py | sync_question_pins()  # ticked | pass  # ticked
# @mutate-why 고정에 알림을 켜면 선택 카드가 두 번 울린다(원본 + 고정 알림).
# @mutate scripts/grok-telegram-bridge.py | kwargs["disable_notification"] = "true" | pass
# @mutate-why 재시도 상한이 없으면 Telegram 장애 때 고정 호출이 끝없이 반복된다.
# @mutate scripts/grok-telegram-bridge.py | >= QUESTION_PIN_MAX_ATTEMPTS | >= 99
"""Keep the unanswered model selection card reachable without duplicating it (T-261107-009)."""
import contextlib
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


BRIDGE = Path(__file__).resolve().parents[1] / "grok-telegram-bridge.py"
if not BRIDGE.exists():
    BRIDGE = Path(__file__).resolve().parents[1] / "grok_telegram_bridge.py"

NOW = 1789220000.0
CHAT = "4321"


class Telegram:
    """Records pin traffic like the Bot API would answer it."""

    def __init__(self):
        self.calls = []
        self.pinned = {999}  # A message the user pinned by hand.
        self.response = None  # None = ok, else the dict to return
        self.during = None  # callable run inside a pinChatMessage, before answering

    def once(self, method, timeout=5, **params):
        self.calls.append((method, timeout, params))
        if method == "pinChatMessage" and self.during:
            hook, self.during = self.during, None
            hook()
        if self.response is not None:
            return self.response
        if method == "pinChatMessage":
            self.pinned.add(int(params["message_id"]))
        if method == "unpinChatMessage":
            self.pinned.discard(int(params["message_id"]))
        return {"ok": True, "result": True}

    def calls_for(self, method):
        return [params for name, _timeout, params in self.calls if name == method]


class QuestionPinsTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        token = self.root / "token.json"
        token.write_text(json.dumps({"api_key": "TESTONLY-fake"}))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "GRB_TOKEN_FILE": str(token), "GRB_NAME": "test", "GRB_CHAT_ID": CHAT,
            "GRB_STATE_DIR": str(self.root), "GRB_GROK_SESSIONS_DIR": str(self.root / "sessions"),
            "GRB_GROK_CHAT_CWD": str(self.root), "GRB_TUI_SESSION_ID": "sid",
            "GRB_CHAT_LANE": "tui", "GRB_LANGUAGE": "en", "GRB_DRY_RUN": "0",
            "GRB_STDIN_INPUT": "0", "GRB_LOCAL_INPUT": "0",
        }))
        self.stack.enter_context(mock.patch.object(sys, "path", [str(BRIDGE.parent), *sys.path]))
        (self.root / "models_cache.json").write_text(json.dumps({"models": {"grok-4.7": {"info": {
            "context_windows": [256000, 500000], "supports_reasoning_effort": True,
            "reasoning_efforts": [{"value": "high"}, {"value": "low"}]}}}}))
        self.telegram = Telegram()
        self.now = NOW
        self.sent = []
        self.restart()

    def restart(self):
        """A fresh process: the picker is memory-only, the pin store is on disk."""
        self.b = {"__file__": str(BRIDGE), "__name__": "question_pins_test"}
        exec(compile(BRIDGE.read_text(), str(BRIDGE), "exec"), self.b)
        self.apply = mock.Mock()
        # The delivery surface name differs between builds; read it from the extractor itself.
        surface = inspect.signature(self.b["_first_sent_message_id"]).parameters["surface"].default

        def send(kind, body, **kw):
            self.sent.append((kind, body, kw))
            return {"deliveries": [{"surface": surface, "result": "sent", "message_id": len(self.sent)}]}

        self.b.update(GROK_HOME_EFFECTIVE=str(self.root), deliver_mesh_event=send,
                      _tg_once=self.telegram.once, _question_pin_clock=lambda: self.now,
                      available_models=lambda: ["grok-4.7"], current_tui_model=lambda: "grok-4.7",
                      tui_follow_session_rotation=lambda: False, is_awaiting_human=lambda: False,
                      _tui_paste=mock.Mock(), apply_model_choice=self.apply, tg=mock.Mock())

    # helpers -----------------------------------------------------------------
    def open_card(self):
        self.b["begin_model_choice"]("grok-4.7")
        return self.b["_MODEL_PICKER"]["message_id"]

    def choose(self, index):
        state = dict(self.b["_MODEL_PICKER"])
        data = self.b["MODEL_OPTION_CALLBACK_PREFIX"] + state["token"] + ":" + str(index)
        self.b["handle_telegram_callback"]({"id": "q", "data": data, "from": {"id": int(CHAT)},
                                            "message": {"chat": {"id": int(CHAT)},
                                                        "message_id": int(state["message_id"])}})

    def sync(self, times=1):
        for _ in range(times):
            self.b["sync_question_pins"]()

    def store(self):
        path = Path(self.b["QUESTION_PIN_FILE"])
        return json.loads(path.read_text()) if path.exists() else {}

    def expire_picker(self):
        self.b["_MODEL_PICKER"]["created"] -= self.b["MODEL_PICKER_TTL"] + 1

    # tests -------------------------------------------------------------------
    def test_open_card_is_pinned_silently_once_without_resending(self):
        mid = self.open_card()
        self.sync(3)
        pins = self.telegram.calls_for("pinChatMessage")
        self.assertEqual(pins, [{"chat_id": CHAT, "message_id": int(mid), "disable_notification": "true"}])
        self.assertEqual(self.telegram.pinned, {999, int(mid)})
        self.assertIs(self.store()[mid]["pin_confirmed"], True)
        self.assertEqual(len(self.sent), 1, "pinning must never resend the card")
        self.assertEqual([call[1] for call in self.telegram.calls], [5], "one short attempt per tick")

    def test_answer_unpins_only_that_card_and_pins_the_next_stage(self):
        first = self.open_card()
        self.sync()
        self.choose(1)  # context window → effort card
        second = self.b["_MODEL_PICKER"]["message_id"]
        self.assertNotEqual(first, second)
        self.sync()  # one operation: release the answered card
        self.assertEqual(self.telegram.calls_for("unpinChatMessage"), [{"chat_id": CHAT, "message_id": int(first)}])
        self.assertEqual(self.telegram.pinned, {999})
        self.sync()  # next operation: pin the new card
        self.assertEqual(self.telegram.pinned, {999, int(second)})
        self.assertNotIn(first, self.store())
        self.apply.assert_not_called()

    def test_final_answer_unpins_without_resubmitting(self):
        self.open_card()
        self.choose(1)
        self.sync(2)
        second = self.b["_MODEL_PICKER"]["message_id"] if self.b["_MODEL_PICKER"] else None
        self.assertIsNotNone(second)
        self.choose(0)  # effort → native submission
        self.apply.assert_called_once()
        self.sync(3)
        self.assertEqual(self.telegram.pinned, {999})
        self.assertEqual(self.store(), {})
        self.apply.assert_called_once()
        self.assertEqual(len(self.sent), 2)

    def test_expiry_unpins_even_with_no_new_events(self):
        mid = self.open_card()
        self.sync()
        self.expire_picker()
        self.sync()
        self.assertEqual(self.telegram.calls_for("unpinChatMessage"), [{"chat_id": CHAT, "message_id": int(mid)}])
        self.assertEqual(self.store(), {})
        self.sync(2)
        self.assertEqual(len(self.telegram.calls), 2)

    def test_restart_releases_the_dead_card_without_repinning_or_resending(self):
        mid = self.open_card()
        self.sync()
        self.restart()  # the picker token is gone, so the card can no longer be answered
        self.assertEqual(self.store()[mid]["pin_action"], "pinChatMessage")
        self.sync()
        self.assertEqual(self.telegram.calls_for("unpinChatMessage"), [{"chat_id": CHAT, "message_id": int(mid)}])
        self.assertEqual(self.telegram.calls_for("pinChatMessage"), [{"chat_id": CHAT, "message_id": int(mid), "disable_notification": "true"}])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store(), {})

    def test_pin_failures_are_bounded_across_restart_and_never_block_answer(self):
        self.telegram.response = {"ok": False}
        mid = self.open_card()
        self.sync(3)
        self.assertEqual(len(self.telegram.calls_for("pinChatMessage")), 1, "retry waits 60 seconds")
        self.now += 60
        self.sync()
        self.now += 60
        self.sync()
        self.now += 60
        self.sync(3)
        self.assertEqual(len(self.telegram.calls_for("pinChatMessage")), 3)
        self.assertEqual(self.store()[mid]["pin_attempts"], 3)
        self.assertIsNone(self.store()[mid]["pin_confirmed"])
        self.choose(1)  # the answer still works while pinning fails
        self.assertEqual(self.b["_MODEL_PICKER"]["stage"], "effort")
        self.restart()
        self.assertEqual(self.store()[mid]["pin_attempts"], 3, "attempt count survives a restart")
        self.now += 60
        self.sync(4)  # the closed card now needs an unpin: its own bounded ladder
        unpins = self.telegram.calls_for("unpinChatMessage")
        self.assertEqual([call["message_id"] for call in unpins], [int(mid)])
        self.now += 60
        self.sync()
        self.restart()
        self.assertEqual(self.store()[mid]["pin_attempts"], 2)
        self.now += 60
        self.sync(3)
        self.assertEqual(len(self.telegram.calls_for("unpinChatMessage")), 3)
        self.assertNotIn("unpinAllChatMessages", [call[0] for call in self.telegram.calls])

    def test_unpin_failure_retries_without_reopening_or_resubmitting(self):
        mid = self.open_card()
        self.sync()
        self.telegram.response = {"ok": False}
        self.choose(1)
        self.sync(3)
        self.assertEqual(len(self.telegram.calls_for("unpinChatMessage")), 1)
        self.assertIn(mid, self.store())
        self.telegram.response = None
        self.now += 60
        self.sync()
        self.assertEqual(len(self.telegram.calls_for("unpinChatMessage")), 2)
        self.assertNotIn(mid, self.store())
        self.assertEqual(self.b["_MODEL_PICKER"]["stage"], "effort")
        self.assertEqual(len(self.sent), 2)
        self.apply.assert_not_called()

    def test_no_pin_for_missing_id_foreign_chat_group_chat_or_stale_record(self):
        self.b["deliver_mesh_event"] = lambda kind, body, **kw: {"deliveries": []}
        self.b["begin_model_choice"]("grok-4.7")
        self.sync()
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.store(), {})
        self.b["record_question_card"]("77", chat_id="8888")
        self.sync()
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.store()["77"]["message_chat_id"], "8888")
        self.b["record_question_card"]("78")
        self.now += self.b["MODEL_PICKER_TTL"] + 1
        self.sync()
        self.assertEqual(self.telegram.calls, [])
        self.assertNotIn("78", self.store(), "a stale record is dropped without a network call")
        self.b["CHAT_ID"] = "-100123"
        self.b["record_question_card"]("79")
        self.sync()
        self.assertEqual(self.telegram.calls, [])

    def test_one_network_operation_per_tick_and_no_global_unpin(self):
        self.open_card()
        self.sync()
        self.choose(1)
        self.sync()
        self.assertEqual(len(self.telegram.calls), 2)
        self.sync()
        self.assertEqual(len(self.telegram.calls), 3)
        self.assertEqual(sorted(name for name, _t, _p in self.telegram.calls),
                         ["pinChatMessage", "pinChatMessage", "unpinChatMessage"])

    def test_chat_binding_is_required_and_config_change_does_not_touch_other_chat(self):
        mid = self.open_card()
        self.sync()
        self.b["CHAT_ID"] = "7777"
        self.expire_picker()
        self.sync(3)
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(self.store()[mid]["message_chat_id"], CHAT)
        self.assertIs(self.store()[mid]["pin_confirmed"], True)

    def test_answer_during_pin_request_is_unpinned_on_next_tick(self):
        first = self.open_card()
        self.telegram.during = lambda: self.choose(1)
        self.sync()
        self.assertIs(self.store()[first]["pin_confirmed"], True)
        self.sync()
        self.assertEqual(self.telegram.calls_for("unpinChatMessage"), [{"chat_id": CHAT, "message_id": int(first)}])
        self.assertNotIn(first, self.store())

    def test_unsuccessful_telegram_response_is_not_recorded_as_pinned(self):
        mid = self.open_card()
        self.telegram.response = {"ok": True, "result": False}
        self.sync()
        self.assertIsNone(self.store()[mid]["pin_confirmed"])
        self.telegram.response = None
        self.now += 60
        self.sync()
        self.assertIs(self.store()[mid]["pin_confirmed"], True)
        self.assertEqual(len(self.telegram.calls_for("pinChatMessage")), 2)

    def test_idle_tick_pins_and_expires_without_more_events(self):
        self.b["QUESTION_PIN_TICK_INTERVAL"] = 0.01
        mid = self.open_card()
        self.b["question_pin_tick"]()
        self.assertEqual(self.telegram.pinned, {999, int(mid)})
        self.expire_picker()
        self.b["question_pin_tick"]()
        self.assertEqual(self.telegram.pinned, {999})
        self.assertEqual(self.store(), {})

    def test_ticker_thread_is_started_by_default_and_skipped_in_dry_run(self):
        started = []
        self.b["threading"].Thread = lambda *a, **kw: started.append(kw.get("name")) or mock.Mock()
        self.b["install_thread_dump_handler"] = lambda: None
        self.b["health_mark"] = lambda **kw: None
        self.b["local_print"] = lambda *a, **kw: None
        self.b["start_workers"]()
        self.assertIn("grok-bridge-test-question-pins", started)
        started.clear()
        self.b["DRY_RUN"] = True
        self.b["start_workers"]()
        self.assertNotIn("grok-bridge-test-question-pins", started)


if __name__ == "__main__":
    unittest.main()
