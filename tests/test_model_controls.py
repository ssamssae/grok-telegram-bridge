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
             mock.patch.object(b, 'tui_follow_session_rotation'), \
             mock.patch.object(b, 'tui_session_id', return_value='session'), \
             mock.patch.object(b, 'tg', return_value={'ok':True,'result':{'message_id':1}}) as tg:
            b.handle_message_text('/model')
        self.assertTrue(b.JOBS.empty())
        self.assertEqual(tg.call_args.args[0], 'sendMessage')
        self.assertIn('grok-4.7', tg.call_args.kwargs['text'])
        buttons = json.loads(tg.call_args.kwargs['reply_markup'])['inline_keyboard']
        self.assertTrue(buttons[0][0]['callback_data'].startswith('grb-model-option:'))

    def test_legacy_model_callback_expires(self):
        b = self.bridge
        with mock.patch.object(b, 'tg'), mock.patch.object(b, 'begin_model_choice') as apply:
            b.handle_telegram_callback({'id':'q','data':'grb-model:grok-4.7',
                'message':{'chat':{'id':b.CHAT_ID}}})
        apply.assert_not_called()

    def test_model_window_effort_cards_submit_once_in_public_build(self):
        b = self.bridge
        mid = 10
        def send(method, **kw):
            nonlocal mid
            mid += 1
            return {'ok': True, 'result': {'message_id': mid}}
        with mock.patch.object(b, 'tg', side_effect=send), \
             mock.patch.object(b, 'available_models', return_value=['grok-4.7']), \
             mock.patch.object(b, 'available_model_settings', return_value={'windows':[256000,500000],'efforts':['high','low']}), \
             mock.patch.object(b, 'tui_follow_session_rotation'), \
             mock.patch.object(b, 'tui_session_id', return_value='session'), \
             mock.patch.object(b, 'is_awaiting_human', return_value=True), \
             mock.patch.object(b, 'apply_model_choice') as apply:
            b.handle_message_text('/model')
            menu = dict(b._MODEL_PICKER)
            b.handle_telegram_callback({'id':'q','data':b.MODEL_OPTION_CALLBACK_PREFIX+menu['token']+':0',
                'message':{'chat':{'id':b.CHAT_ID},'message_id':menu['message_id']}})
            first = dict(b._MODEL_PICKER)
            self.assertEqual(first['stage'], 'window')
            b.press_model_option(b.MODEL_OPTION_CALLBACK_PREFIX + first['token'] + ':1', first['message_id'])
            second = dict(b._MODEL_PICKER)
            self.assertEqual(second['stage'], 'effort')
            data = b.MODEL_OPTION_CALLBACK_PREFIX + second['token'] + ':0'
            b.press_model_option(data, second['message_id'])
            b.press_model_option(data, second['message_id'])
            apply.assert_called_once_with('grok-4.7',context_window=500000,effort='high',expected_session='session')
            self.assertTrue(b.is_awaiting_human())
            self.assertTrue(b.JOBS.empty())

    def test_unknown_model_cannot_reach_terminal(self):
        b = self.bridge
        with mock.patch.object(b, 'available_models', return_value=['grok-4.7']), \
             mock.patch.object(b, 'tg'), mock.patch.object(b, '_tui_paste') as paste:
            b.apply_model_choice('unknown\ncommand')
        paste.assert_not_called()

    def test_reply_style_is_idempotent_and_preserves_commands(self):
        b = self.bridge
        text = b.with_reply_style_instruction('Hello')
        self.assertEqual(text, 'Hello')
        self.assertEqual(b.with_reply_style_instruction(text), text)
        self.assertEqual(b.strip_reply_style_instruction(text), 'Hello')
        self.assertEqual(b.with_reply_style_instruction('/model grok-4.7'), '/model grok-4.7')


if __name__ == '__main__':
    unittest.main()
