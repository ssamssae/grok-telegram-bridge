"""Exercise the exported Telegram boundary without credentials or network calls."""
import json
import tempfile
import unittest
from unittest import mock
from test_public_export import load_bridge


class ModelControlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = load_bridge('public_model_controls', GRB_STATE_DIR=self.tmp.name,
                                  GRB_CHAT_LANE='tui', GRB_SUGGESTED_CONFIRM='0')

    def test_menu_delivers_model_buttons_via_public_sender(self):
        b = self.bridge
        with mock.patch.object(b, 'available_models', return_value=['grok-4.7','grok-4.6']), \
             mock.patch.object(b, 'current_tui_model', return_value='grok-4.6'), \
             mock.patch.object(b, 'tg', return_value={'ok':True,'result':{'message_id':1}}) as tg:
            b.handle_message_text('/model')
        self.assertTrue(b.JOBS.empty())
        self.assertEqual(tg.call_args.args[0], 'sendMessage')
        self.assertIn('grok-4.7', tg.call_args.kwargs['text'])
        buttons = json.loads(tg.call_args.kwargs['reply_markup'])['inline_keyboard']
        self.assertEqual(buttons[0][0]['callback_data'], 'grb-model:grok-4.7')

    def test_model_callback_does_not_require_suggestion_buttons(self):
        b = self.bridge
        with mock.patch.object(b, 'tg'), mock.patch.object(b, 'apply_model_choice') as apply:
            b.handle_telegram_callback({'id':'q','data':'grb-model:grok-4.7',
                'message':{'chat':{'id':b.CHAT_ID}}})
        apply.assert_called_once_with('grok-4.7')

    def test_unknown_model_cannot_reach_terminal(self):
        b = self.bridge
        with mock.patch.object(b, 'available_models', return_value=['grok-4.7']), \
             mock.patch.object(b, 'tg'), mock.patch.object(b, '_tui_paste') as paste:
            b.apply_model_choice('unknown\ncommand')
        paste.assert_not_called()

    def test_reply_style_is_idempotent_and_preserves_commands(self):
        b = self.bridge
        text = b.with_reply_style_instruction('Hello')
        self.assertTrue(text.endswith(b.REPLY_STYLE_INSTRUCTION))
        self.assertEqual(b.with_reply_style_instruction(text), text)
        self.assertEqual(b.strip_reply_style_instruction(text), 'Hello')
        self.assertEqual(b.with_reply_style_instruction('/model grok-4.7'), '/model grok-4.7')


if __name__ == '__main__':
    unittest.main()
