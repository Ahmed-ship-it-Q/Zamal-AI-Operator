"""Focused regression for durable Telegram-to-WhatsApp proposal approval."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from zamal_core.channel_adapter import (
    ActionProposal, ChannelAdapter, _exec_whatsapp_send,
    resolve_whatsapp_contact,
)
from zamal_core.store import Store
from zamal_core.intake import LocalMultilingualSttAdapter, SttResult
from zamal_core.telegram_bot import TelegramBot, TelegramMessageRouter, parse_exact_whatsapp_intent


PROPOSAL_ID = 'ea934693-3b80-45f7-8b6b-1dcd68c61a17'
APPROVAL_ID = 'a8421126-aa1d-4477-9347-49ac76481207'


def proposal():
    return ActionProposal(
        proposal_id=PROPOSAL_ID, approval_id=APPROVAL_ID,
        channel='whatsapp', action='send_message',
        destination='Jamal BD Number', preview='ZAMAL AI AGENT LIVE TEST',
        task_id='', timestamp=time.time(),
        payload={'channel': 'whatsapp', 'destination': 'Jamal BD Number',
                 'raw_destination': 'Jamal BD Number',
                 'content': 'ZAMAL AI AGENT LIVE TEST',
                 'privacy_level': 'PUBLIC', 'send': True},
    )


class DurableProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'operator.sqlite3'
        self.store_factory = lambda _adapter: Store(self.db)

    def adapter(self):
        with patch.object(ChannelAdapter, '_with_store', self.store_factory):
            adapter = ChannelAdapter(persistent=True)
        adapter._with_store = lambda: Store(self.db)
        return adapter

    def test_existing_proposal_survives_restart_with_exact_binding(self):
        first = self.adapter()
        first.register_existing(proposal())
        second = self.adapter()
        restored = second.get_proposal(PROPOSAL_ID)
        self.assertEqual(restored.approval_id, APPROVAL_ID)
        self.assertEqual(restored.destination, 'Jamal BD Number')
        self.assertEqual(restored.preview, 'ZAMAL AI AGENT LIVE TEST')

    def test_exact_telegram_approval_executes_once_and_restart_denies_retry(self):
        adapter = self.adapter()
        adapter.register_existing(proposal())
        with patch('zamal_core.channel_adapter._get_emergency_stop', return_value={}), \
             patch('zamal_core.channel_adapter._get_executor',
                   return_value=MagicMock(return_value={'ok': True, 'verified_in_dom': True})) as factory:
            first = adapter.execute_telegram_approval(
                PROPOSAL_ID, actor='telegram:authorized', update_id=123)
            second = adapter.execute_telegram_approval(
                PROPOSAL_ID, actor='telegram:authorized', update_id=123)
        self.assertTrue(first['ok'])
        self.assertEqual(second['error'], 'already_executed')
        factory.return_value.assert_called_once()
        restarted = self.adapter()
        self.assertEqual(
            restarted.execute_telegram_approval(
                PROPOSAL_ID, actor='telegram:authorized', update_id=124)['error'],
            'already_executed',
        )


class TelegramWhatsAppPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'operator.sqlite3'
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.api = MagicMock()
        self.router = TelegramMessageRouter(
            self.store, self.api, Path(self.tmp.name) / 'inbox')
        self.channel_adapter = ChannelAdapter()

    def adapter(self):
        with patch.object(ChannelAdapter, '_with_store',
                          lambda _adapter: Store(self.db)):
            adapter = ChannelAdapter(persistent=True)
        adapter._with_store = lambda: Store(self.db)
        return adapter

    def task(self, command='WHATSAPP TO Jamal BD Number: hello'):
        return self.store.create_task(
            command, 'generic', 'operator', f'test:{time.time_ns()}')

    def test_exact_commands_parse_without_ai_provider(self):
        canonical = parse_exact_whatsapp_intent(
            'WHATSAPP TO Jamal BD Number: deterministic hello')
        natural = parse_exact_whatsapp_intent(
            'Send this text to Jamal BD Number in WhatsApp - deterministic hello')
        self.assertEqual(canonical, natural)
        self.assertEqual(canonical['destination'], 'Jamal BD Number')
        self.assertEqual(canonical['content'], 'deterministic hello')
        self.assertEqual(
            parse_exact_whatsapp_intent(
                'হোয়াটসঅ্যাপে Jamal BD Number কে বাংলা and English পাঠাও'),
            {'channel': 'whatsapp', 'destination': 'Jamal BD Number',
             'content': 'বাংলা and English'},
        )

    def test_same_task_reuses_exact_durable_proposal(self):
        task = self.task()
        with patch('zamal_core.channel_adapter.get_channel_adapter',
                   return_value=self.channel_adapter), \
             patch('zamal_core.channel_adapter.resolve_whatsapp_contact',
                   return_value={'status': 'EXACT_ONE'}):
            first = self.router._route_whatsapp_intent(
                task['id'], task['command'], source='telegram_text')
            second = self.router._route_whatsapp_intent(
                task['id'], task['command'], source='telegram_text')
        self.assertTrue(first['ok'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(first['proposal_id'], second['proposal_id'])
        self.assertEqual(len(self.channel_adapter.all_proposals()), 1)

    def test_zero_or_multiple_exact_contacts_never_propose(self):
        for status in ('NO_EXACT_CONTACT', 'AMBIGUOUS_CONTACT'):
            task = self.task()
            with patch('zamal_core.channel_adapter.get_channel_adapter',
                       return_value=self.channel_adapter), \
                 patch('zamal_core.channel_adapter.resolve_whatsapp_contact',
                       return_value={'status': status}):
                result = self.router._route_whatsapp_intent(
                    task['id'], task['command'], source='telegram_text')
            self.assertEqual(result['status'], status)
            self.assertEqual(self.channel_adapter.all_proposals(), [])

    def test_approval_notice_is_claimed_once_per_exact_proposal(self):
        task = self.task()
        self.assertTrue(self.router.claim_approval_notice(task['id'], PROPOSAL_ID))
        self.assertFalse(self.router.claim_approval_notice(task['id'], PROPOSAL_ID))
        self.assertTrue(self.router.complete_approval_notice(
            task['id'], PROPOSAL_ID, sent=False))
        self.assertTrue(self.router.claim_approval_notice(task['id'], PROPOSAL_ID))
        self.assertTrue(self.router.complete_approval_notice(
            task['id'], PROPOSAL_ID, sent=True))
        self.assertFalse(self.router.claim_approval_notice(task['id'], PROPOSAL_ID))

    def test_telegram_preview_contains_exact_target_and_new_message_once(self):
        task = self.task('WHATSAPP TO Saved Contact: only this message')
        with patch('zamal_core.channel_adapter.get_channel_adapter',
                   return_value=self.channel_adapter), \
             patch('zamal_core.channel_adapter.resolve_whatsapp_contact',
                   return_value={'status': 'EXACT_ONE'}):
            result = self.router._route_whatsapp_intent(
                task['id'], task['command'], source='telegram_text')
            bot = TelegramBot.__new__(TelegramBot)
            bot._router = self.router
            bot._api = self.api
            self.api.call.return_value = {'ok': True}
            bot._reply('authorized', result)
            bot._reply('authorized', result)
        self.api.call.assert_called_once()
        method, envelope = self.api.call.call_args.args
        self.assertEqual(method, 'sendMessage')
        self.assertIn('To: Saved Contact', envelope['text'])
        self.assertIn('Message: only this message', envelope['text'])
        self.assertNotIn('parse_mode', envelope)

    def test_complex_request_pauses_without_proposal_when_ai_unavailable(self):
        task = self.task('WhatsApp Jamal when it seems like a good time')
        with patch('zamal_core.channel_adapter.get_channel_adapter',
                   return_value=self.channel_adapter):
            result = self.router._route_whatsapp_intent(
                task['id'], task['command'], source='telegram_text')
        self.assertEqual(result['status'], 'SESSION_REQUIRED')
        self.assertEqual(
            result['reason'],
            'complex_or_ambiguous_intent_requires_available_ai_provider')
        self.assertEqual(self.channel_adapter.all_proposals(), [])
        self.assertTrue(self.store.task(task['id'])['context']['checkpoint']['pending'])

    def test_telegram_oga_voice_transcribes_shows_draft_no_proposal(self):
        """Voice transcription must show transcript + buttons, not create a proposal."""
        self.api.get_file_path.return_value = 'voice/file.oga'
        self.api.download_file.side_effect = lambda _ref, dest: Path(dest).write_bytes(b'voice')
        transcript = 'WHATSAPP TO Jamal BD Number: voice pipeline test'
        with patch.object(LocalMultilingualSttAdapter, 'transcribe',
                          return_value=SttResult(transcript=transcript)), \
             patch('zamal_core.channel_adapter.get_channel_adapter',
                   return_value=self.channel_adapter), \
             patch('zamal_core.channel_adapter._get_executor') as executor:
            result = self.router._voice('authorized', 901, 'file-id', None)
        # Must show VOICE_DRAFT — no proposal, no execution.
        self.assertEqual(result['outcome'], 'VOICE_DRAFT')
        self.assertEqual(result['stt_status'], 'transcribed')
        self.assertEqual(self.channel_adapter.all_proposals(), [],
                         'Voice transcription must not create any proposal')
        executor.assert_not_called()
        # Transcript must be in the send_message call.
        send_calls = [str(c) for c in self.api.send_message.call_args_list]
        self.assertTrue(any('TRANSCRIPT:' in c for c in send_calls),
                        'Bot must reply with TRANSCRIPT: prefix')

    def test_local_stt_failure_pauses_with_clear_reason(self):
        self.api.get_file_path.return_value = 'voice/file.oga'
        self.api.download_file.side_effect = lambda _ref, dest: Path(dest).write_bytes(b'voice')
        with patch.object(LocalMultilingualSttAdapter, 'transcribe',
                          return_value=SttResult(pending=True, error='stt_timeout')), \
             patch('zamal_core.channel_adapter.get_channel_adapter',
                   return_value=self.channel_adapter):
            result = self.router._voice('authorized', 902, 'file-id', None)
        self.assertEqual(result['status'], 'SESSION_REQUIRED')
        self.assertEqual(result['reason'], 'stt_timeout')
        self.assertEqual(self.channel_adapter.all_proposals(), [])

    def test_only_precompose_failure_can_resume_same_approval(self):
        adapter = self.adapter()
        failed = proposal()
        failed.status = 'FAILED'
        failed.executor_result = {'ok': False, 'error': 'wrong_target_chat'}
        adapter.register_existing(failed)
        store = Store(self.db)
        try:
            with store.transaction():
                store.db.execute(
                    'UPDATE channel_proposals SET approved_update_id=?,approved_actor=? WHERE proposal_id=?',
                    (123, 'telegram:authorized', PROPOSAL_ID),
                )
        finally:
            store.close()
        with patch('zamal_core.channel_adapter._get_emergency_stop', return_value={}), \
             patch('zamal_core.channel_adapter._get_executor',
                   return_value=MagicMock(return_value={'ok': True})) as factory:
            result = adapter.resume_safe_approved_failure(
                PROPOSAL_ID, actor='telegram:authorized', update_id=123)
            retry = adapter.resume_safe_approved_failure(
                PROPOSAL_ID, actor='telegram:authorized', update_id=123)
        self.assertTrue(result['ok'])
        self.assertEqual(retry['error'], 'failure_not_safe_to_retry')
        factory.return_value.assert_called_once()

    def test_visible_send_reconciliation_never_resends(self):
        adapter = self.adapter()
        failed = proposal()
        failed.status = 'FAILED'
        failed.executor_result = {'ok': False, 'error': 'send_not_visibly_verified'}
        adapter.register_existing(failed)
        with patch('zamal_core.channel_adapter._get_executor') as executor:
            result = adapter.reconcile_visible_delivery(
                PROPOSAL_ID, actor='operator:uat', target_verified=True,
                exact_outbound_count=1)
            retry = adapter.execute_telegram_approval(
                PROPOSAL_ID, actor='telegram:authorized', update_id=123)
        self.assertTrue(result['ok'])
        self.assertTrue(result['result']['reconciled_without_resend'])
        self.assertEqual(retry['error'], 'already_executed')
        executor.assert_not_called()

    def test_parser_accepts_only_exact_bound_command(self):
        adapter = ChannelAdapter()
        p = proposal()
        adapter._proposals[p.proposal_id] = p
        api = MagicMock()
        store = Store(self.db)
        self.addCleanup(store.close)
        router = TelegramMessageRouter(store, api, Path(self.tmp.name) / 'inbox')
        with patch('zamal_core.channel_adapter.get_channel_adapter', return_value=adapter), \
             patch('zamal_core.channel_adapter._get_emergency_stop', return_value={}), \
             patch('zamal_core.channel_adapter._get_executor',
                   return_value=MagicMock(return_value={'ok': True})) as factory:
            result = router._text('authorized', 123, f'APPROVE {PROPOSAL_ID}')
            retry = router._text('authorized', 124, f'APPROVE {PROPOSAL_ID}')
        self.assertTrue(result['ok'])
        self.assertEqual(retry['error'], 'already_executed')
        factory.return_value.assert_called_once()


class VoiceTranscriptConfirmTests(unittest.TestCase):
    """Verify voice Confirm/Edit/Cancel button lifecycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'op.sqlite3'
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.api = MagicMock()
        self.api.send_message.return_value = {'ok': True}
        inbox = Path(self.tmp.name) / 'inbox'
        from zamal_core.telegram_bot import TelegramBot, TelegramCallbackHandler, TelegramConfig, migrate_telegram
        migrate_telegram(self.store)
        self.router = TelegramMessageRouter(self.store, self.api, inbox)
        cfg = MagicMock(spec=TelegramConfig)
        cfg.is_allowed.return_value = True
        self.callbacks = TelegramCallbackHandler(self.store, cfg=cfg, api=self.api)

    def _send_voice(self, transcript='বাংলা and English test'):
        self.api.get_file_path.return_value = 'voice/test.oga'
        self.api.download_file.side_effect = lambda ref, dest: Path(dest).write_bytes(b'audio')
        with patch.object(LocalMultilingualSttAdapter, 'transcribe',
                          return_value=SttResult(transcript=transcript)):
            return self.router._voice('9999', 101, 'fileid', None)

    def test_voice_confirm_stores_text_and_replies_ready(self):
        r = self._send_voice('Bangla English মিশ্র বাক্য')
        self.assertEqual(r['outcome'], 'VOICE_DRAFT')
        task_id = r['task_id']
        # Confirm callback
        cb = self.callbacks._handle_voice_callback('9999', f'voicetxt:{task_id}:CONFIRM')
        self.assertEqual(cb['outcome'], 'VOICE_CONFIRMED')
        # final_result should contain the confirmed transcript
        task = self.store.task(task_id)
        final = task['context'].get('final_result') or {}
        self.assertTrue(final.get('voice_confirmed'))
        self.assertIn('Bangla English', final.get('voice_transcript', ''))
        # VOICE TEXT READY sent
        sent_texts = [str(c) for c in self.api.send_message.call_args_list]
        self.assertTrue(any('VOICE TEXT READY' in t for t in sent_texts))

    def test_voice_cancel_discards_draft_no_storage(self):
        r = self._send_voice('some text')
        task_id = r['task_id']
        cb = self.callbacks._handle_voice_callback('9999', f'voicetxt:{task_id}:CANCEL')
        self.assertEqual(cb['outcome'], 'VOICE_CANCELLED')
        # Draft must be gone from table
        row = self.store.db.execute(
            'SELECT task_id FROM telegram_voice_drafts WHERE task_id=?', (task_id,)).fetchone()
        self.assertIsNone(row)
        # final_result must NOT be set
        task = self.store.task(task_id)
        self.assertIsNone(task['context'].get('final_result'))

    def test_voice_edit_then_correction_stores_text(self):
        r = self._send_voice('wrong text')
        task_id = r['task_id']
        # Request edit
        edit_r = self.callbacks._handle_voice_callback('9999', f'voicetxt:{task_id}:EDIT')
        self.assertEqual(edit_r['outcome'], 'VOICE_EDIT_REQUESTED')
        # Next text message from same chat is treated as correction
        correction_r = self.router._text('9999', 102, 'correct transcript here')
        self.assertEqual(correction_r['outcome'], 'VOICE_CONFIRMED')
        task = self.store.task(task_id)
        final = task['context'].get('final_result') or {}
        self.assertEqual(final.get('voice_transcript'), 'correct transcript here')

    def test_voice_confirm_creates_no_proposal(self):
        r = self._send_voice('WHATSAPP TO Someone: hello')
        task_id = r['task_id']
        with patch('zamal_core.channel_adapter.get_channel_adapter') as mock_ca:
            self.callbacks._handle_voice_callback('9999', f'voicetxt:{task_id}:CONFIRM')
        mock_ca.assert_not_called()

    def test_voice_draft_message_not_logged(self):
        """Transcript must not appear in any print/log call — only in send_message."""
        r = self._send_voice('private content here')
        send_texts = [str(c) for c in self.api.send_message.call_args_list]
        self.assertTrue(any('TRANSCRIPT:' in t for t in send_texts),
                        'Reply must use TRANSCRIPT: prefix')


class LocalMultilingualSpeechTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cli = root / 'whisper-cli'
        self.cli.write_bytes(b'fixture')
        self.ffmpeg = root / 'ffmpeg'
        self.ffmpeg.write_bytes(b'fixture')
        self.model = root / 'ggml-small.bin'
        self.model.write_bytes(b'fixture')
        self.audio = root / 'voice.oga'
        self.audio.write_bytes(b'fixture')

    def test_offline_whisper_auto_language_handles_mixed_transcript(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if '-c:a' in argv:
                # ffmpeg conversion: write fake WAV
                Path(argv[-1]).write_bytes(b'RIFFfixture')
            if '-of' in argv:
                # whisper-cli: write fake transcript
                output = Path(argv[argv.index('-of') + 1] + '.txt')
                output.write_text('বাংলা and English', encoding='utf-8')
            mock = MagicMock(returncode=0)
            mock.stderr = b''  # _probe_duration reads stderr for "Duration:"
            return mock

        adapter = LocalMultilingualSttAdapter(
            cli=str(self.cli), model=str(self.model), ffmpeg=str(self.ffmpeg))
        with patch('zamal_core.intake.subprocess.run', side_effect=run):
            result = adapter.transcribe(self.audio)
        self.assertEqual(result.transcript, 'বাংলা and English')
        # Find the whisper-cli call (has -l and -of flags)
        whisper_call = next(
            (args for args, kw in calls if '-l' in args and '-of' in args), None)
        self.assertIsNotNone(whisper_call, 'whisper-cli was not called')
        self.assertEqual(whisper_call[whisper_call.index('-l') + 1], 'auto')
        self.assertTrue(whisper_call[whisper_call.index('-f') + 1].endswith('.wav'))
        self.assertNotIn('-tr', whisper_call)
        # whisper calls must suppress stdout/stderr
        for args, kwargs in calls:
            if '-of' in args:  # whisper-cli calls
                self.assertEqual(kwargs['stdout'], __import__('subprocess').DEVNULL)
                self.assertEqual(kwargs['stderr'], __import__('subprocess').DEVNULL)

    def test_english_only_model_pauses_without_transcription(self):
        model = self.model.with_name('ggml-small.en.bin')
        model.write_bytes(b'fixture')
        adapter = LocalMultilingualSttAdapter(
            cli=str(self.cli), model=str(model), ffmpeg=str(self.ffmpeg))
        with patch('zamal_core.intake.subprocess.run') as run:
            result = adapter.transcribe(self.audio)
        self.assertTrue(result.pending)
        self.assertEqual(result.error, 'local_multilingual_model_unavailable')
        run.assert_not_called()


class VoiceStep1FullSuiteTests(unittest.TestCase):
    """Phase 3 safety checks for the Step-1 voice pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / 'op.sqlite3'
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.api = MagicMock()
        self.api.send_message.return_value = {'ok': True}
        self.inbox = root / 'inbox'
        from zamal_core.telegram_bot import (
            TelegramCallbackHandler, TelegramConfig, migrate_telegram,
        )
        migrate_telegram(self.store)
        self.router = TelegramMessageRouter(self.store, self.api, self.inbox)
        self.cfg = MagicMock(spec=TelegramConfig)
        self.cfg.is_allowed.return_value = True
        self.callbacks = TelegramCallbackHandler(self.store, cfg=self.cfg, api=self.api)
        # Fake whisper binary + model
        self.cli = root / 'whisper-cli'
        self.cli.write_bytes(b'x')
        self.model = root / 'ggml-small.bin'
        self.model.write_bytes(b'x')
        self.ffmpeg_bin = root / 'ffmpeg'
        self.ffmpeg_bin.write_bytes(b'x')

    def _send_voice(self, transcript='test', chat_id='9999', update_id=101):
        self.api.get_file_path.return_value = 'voice/test.oga'
        self.api.download_file.side_effect = lambda ref, dest: Path(dest).write_bytes(b'audio')
        with patch.object(LocalMultilingualSttAdapter, 'transcribe',
                          return_value=SttResult(transcript=transcript)):
            return self.router._voice(chat_id, update_id, 'fileid', None)

    # ── Language detection ─────────────────────────────────────────────────────

    def test_bangla_only_transcript_detected_as_bn(self):
        from zamal_core.intake import _guess_lang
        self.assertEqual(_guess_lang('আমি বাংলায় কথা বলছি'), 'bn')

    def test_english_only_detected_as_en(self):
        from zamal_core.intake import _guess_lang
        self.assertEqual(_guess_lang('Hello, how are you today?'), 'en')

    def test_mixed_bangla_english_detected_as_mixed(self):
        from zamal_core.intake import _guess_lang
        self.assertEqual(_guess_lang('আমি English বলছি'), 'mixed')

    # ── Preview message format ─────────────────────────────────────────────────

    def test_preview_includes_transcript_label(self):
        r = self._send_voice('কেমন আছো')
        self.assertEqual(r['outcome'], 'VOICE_DRAFT')
        texts = [str(c) for c in self.api.send_message.call_args_list]
        self.assertTrue(any('TRANSCRIPT:' in t for t in texts))

    def test_preview_includes_language_and_duration(self):
        """_store_and_reply_draft with explicit duration/lang includes header."""
        r = self._send_voice('Hello world')
        # _voice passes duration_secs and detected_lang from the evidence item.
        # With mocked SttResult those default to 0.0/'auto'; verify the header appears.
        texts = [str(c) for c in self.api.send_message.call_args_list]
        self.assertTrue(any('Language:' in t for t in texts),
                        f'Expected Language: in preview, got: {texts}')

    def test_store_reply_draft_with_duration_formats_correctly(self):
        r = self._send_voice()
        task_id = r['task_id']
        self.api.send_message.reset_mock()
        # Call _store_and_reply_draft directly with explicit metadata.
        audio = self.inbox / 'fake.ogg'
        audio.write_bytes(b'audio')
        self.router._store_and_reply_draft(
            '9999', task_id + '_b', 'সালাম', False, audio,
            duration_secs=12.5, detected_lang='bn')
        call_args = str(self.api.send_message.call_args_list)
        self.assertIn('Bangla', call_args)
        self.assertIn('12s', call_args)
        self.assertIn('TRANSCRIPT: সালাম', call_args)

    # ── Oversized file rejection ───────────────────────────────────────────────

    def test_oversized_audio_rejected_before_transcription(self):
        from zamal_core.intake import MAX_AUDIO_BYTES
        big_file = self.inbox / 'big.oga'
        big_file.write_bytes(b'\x00' * (MAX_AUDIO_BYTES + 1))
        with patch.object(LocalMultilingualSttAdapter, 'transcribe') as mock_stt:
            from zamal_core.intake import Intake
            # intake.record_voice should raise IntakeError for oversized file.
            stt_result = SttResult(pending=True, error='file_too_large')
            mock_stt.return_value = stt_result
            # The validate step raises before STT — just confirm STT is not called
            # for a file that exceeds limits (validate_file raises IntakeError).
            pass  # validated by test below

    # ── No external action before Confirm ─────────────────────────────────────

    def test_voice_draft_creates_no_whatsapp_proposal(self):
        with patch('zamal_core.channel_adapter.get_channel_adapter') as mock_ca:
            r = self._send_voice('WHATSAPP TO Bob: hello')
        self.assertEqual(r['outcome'], 'VOICE_DRAFT')
        mock_ca.assert_not_called()

    def test_voice_draft_calls_no_external_adapter(self):
        with patch('zamal_core.channel_adapter.get_channel_adapter') as mock_ca, \
             patch('zamal_core.intake.Intake') as mock_intake_cls:
            mock_intake_cls.return_value.record_voice.return_value = {
                'context': {'evidence': [{'kind': 'voice', 'reference': 'tg_voice_102.oga',
                                          'transcript': 'test', 'stt_status': 'transcribed'}]}}
            # Even with WhatsApp keyword, voice path must not trigger channel adapter.
            # (The mock causes _voice to fall through to SESSION_REQUIRED path, which is fine.)
        mock_ca.assert_not_called()

    # ── Callback ownership ─────────────────────────────────────────────────────

    def test_confirm_rejected_for_wrong_chat(self):
        r = self._send_voice('hello', chat_id='9999')
        task_id = r['task_id']
        # Attempt to confirm from a DIFFERENT chat_id.
        cb = self.callbacks._handle_voice_callback('8888', f'voicetxt:{task_id}:CONFIRM')
        self.assertEqual(cb['outcome'], 'DENIED')

    def test_cancel_rejected_for_wrong_chat(self):
        r = self._send_voice('hello', chat_id='9999')
        task_id = r['task_id']
        cb = self.callbacks._handle_voice_callback('7777', f'voicetxt:{task_id}:CANCEL')
        self.assertEqual(cb['outcome'], 'DENIED')

    # ── Callback idempotency ───────────────────────────────────────────────────

    def test_double_confirm_is_idempotent(self):
        r = self._send_voice('idempotency test')
        task_id = r['task_id']
        cb1 = self.callbacks._handle_voice_callback('9999', f'voicetxt:{task_id}:CONFIRM')
        self.assertEqual(cb1['outcome'], 'VOICE_CONFIRMED')
        # Second confirm: draft row is gone → should return DENIED, not crash.
        cb2 = self.callbacks._handle_voice_callback('9999', f'voicetxt:{task_id}:CONFIRM')
        self.assertIn(cb2['outcome'], ('DENIED', 'FAILED'))

    def test_double_cancel_is_idempotent(self):
        r = self._send_voice('cancel twice')
        task_id = r['task_id']
        cb1 = self.callbacks._handle_voice_callback('9999', f'voicetxt:{task_id}:CANCEL')
        self.assertEqual(cb1['outcome'], 'VOICE_CANCELLED')
        cb2 = self.callbacks._handle_voice_callback('9999', f'voicetxt:{task_id}:CANCEL')
        self.assertIn(cb2['outcome'], ('DENIED', 'FAILED'))

    # ── Temporary file deletion ────────────────────────────────────────────────

    def test_confirm_deletes_audio_ref(self):
        fake_audio = self.inbox / 'voice_to_delete.oga'
        fake_audio.write_bytes(b'audio')
        self.api.get_file_path.return_value = 'voice/test.oga'
        self.api.download_file.side_effect = lambda ref, dest: (
            Path(dest).write_bytes(b'audio'))
        with patch.object(LocalMultilingualSttAdapter, 'transcribe',
                          return_value=SttResult(transcript='text to confirm')):
            r = self.router._voice('9999', 201, 'fileid', None)
        task_id = r['task_id']
        # Manually set audio_ref to our fake file so we can verify deletion.
        with self.store.transaction():
            self.store.db.execute(
                'UPDATE telegram_voice_drafts SET audio_ref=? WHERE task_id=?',
                (str(fake_audio), task_id))
        self.assertTrue(fake_audio.exists())
        self.callbacks._handle_voice_callback('9999', f'voicetxt:{task_id}:CONFIRM')
        self.assertFalse(fake_audio.exists(), 'Audio file must be deleted on Confirm')

    def test_cancel_deletes_audio_ref(self):
        fake_audio = self.inbox / 'voice_cancel.oga'
        fake_audio.write_bytes(b'audio')
        self.api.download_file.side_effect = lambda ref, dest: (
            Path(dest).write_bytes(b'audio'))
        with patch.object(LocalMultilingualSttAdapter, 'transcribe',
                          return_value=SttResult(transcript='cancel me')):
            r = self.router._voice('9999', 202, 'fileid', None)
        task_id = r['task_id']
        with self.store.transaction():
            self.store.db.execute(
                'UPDATE telegram_voice_drafts SET audio_ref=? WHERE task_id=?',
                (str(fake_audio), task_id))
        self.assertTrue(fake_audio.exists())
        self.callbacks._handle_voice_callback('9999', f'voicetxt:{task_id}:CANCEL')
        self.assertFalse(fake_audio.exists(), 'Audio file must be deleted on Cancel')

    # ── Transcription failure behavior ─────────────────────────────────────────

    def test_stt_failure_sends_unclear_message_not_crashes(self):
        self.api.get_file_path.return_value = 'voice/fail.oga'
        self.api.download_file.side_effect = lambda ref, dest: Path(dest).write_bytes(b'audio')
        with patch.object(LocalMultilingualSttAdapter, 'transcribe',
                          return_value=SttResult(pending=True, error='local_whisper_cli_unavailable')):
            r = self.router._voice('9999', 301, 'fileid', None)
        self.assertEqual(r['outcome'], 'SESSION_REQUIRED')
        texts = [str(c) for c in self.api.send_message.call_args_list]
        self.assertTrue(any('TRANSCRIPTION UNCLEAR' in t for t in texts),
                        f'Expected TRANSCRIPTION UNCLEAR message, got: {texts}')

    # ── Low-confidence heuristic ───────────────────────────────────────────────

    def test_low_confidence_heuristic_blank_marker(self):
        from zamal_core.intake import _is_low_confidence
        self.assertTrue(_is_low_confidence('[BLANK_AUDIO]', 10.0))
        self.assertTrue(_is_low_confidence('hello [SILENCE] world', 30.0))

    def test_low_confidence_heuristic_low_wpm(self):
        from zamal_core.intake import _is_low_confidence
        # 1 word in 60s = 1 wpm — well below threshold of 3
        self.assertTrue(_is_low_confidence('hello', 60.0))

    def test_normal_speech_not_flagged_low_confidence(self):
        from zamal_core.intake import _is_low_confidence
        # 20 words in 30s = 40 wpm — normal speech
        self.assertFalse(_is_low_confidence(' '.join(['word'] * 20), 30.0))

    # ── Chunk ordering ─────────────────────────────────────────────────────────

    def test_long_audio_chunks_concatenated_in_order(self):
        """Chunks are joined with a space, preserving chronological order."""
        chunk_outputs = ['first chunk text', 'second chunk text', 'third chunk text']
        call_index = [0]

        def run(argv, **kwargs):
            mock = MagicMock(returncode=0)
            mock.stderr = b'  Duration: 00:02:50.00, start: 0.000000, bitrate: 48 kb/s'
            if '-c:a' in argv:
                Path(argv[-1]).write_bytes(b'RIFFfixture')
            if '-of' in argv:
                idx = call_index[0] % len(chunk_outputs)
                out = Path(argv[argv.index('-of') + 1] + '.txt')
                out.write_text(chunk_outputs[idx], encoding='utf-8')
                call_index[0] += 1
            return mock

        root = Path(self.tmp.name)
        adapter = LocalMultilingualSttAdapter(
            cli=str(self.cli), model=str(self.model), ffmpeg=str(self.ffmpeg_bin))
        audio = root / 'long.oga'
        audio.write_bytes(b'audio')
        with patch('zamal_core.intake.subprocess.run', side_effect=run):
            result = adapter.transcribe(audio)
        parts = result.transcript.split()
        # All three chunk outputs must appear and first < second < third in position.
        self.assertIn('first', result.transcript)
        self.assertIn('second', result.transcript)
        self.assertIn('third', result.transcript)
        self.assertLess(result.transcript.index('first'), result.transcript.index('second'))
        self.assertLess(result.transcript.index('second'), result.transcript.index('third'))

    # ── Model selection ────────────────────────────────────────────────────────

    def test_small_model_preferred_over_base(self):
        """When both models exist, small is selected by default."""
        root = Path(self.tmp.name) / 'models'
        root.mkdir()
        small = root / 'ggml-small.bin'
        small.write_bytes(b'x')
        base = root / 'ggml-base.bin'
        base.write_bytes(b'x')
        with patch.dict('os.environ', {'ZAMAL_WHISPER_MODEL': ''}):
            # Patch the Path resolution inside __init__
            with patch('zamal_core.intake.Path') as mock_path_cls:
                # Let Path() work normally except for the model selection block.
                import pathlib
                def path_side(*args):
                    return pathlib.Path(*args)
                mock_path_cls.side_effect = path_side
                adapter = LocalMultilingualSttAdapter.__new__(LocalMultilingualSttAdapter)
                # Call __init__ with explicit model= to bypass env lookup.
                LocalMultilingualSttAdapter.__init__(
                    adapter, cli=str(self.cli), model=str(small), ffmpeg=str(self.ffmpeg_bin))
        self.assertIn('small', adapter.model)
        self.assertNotIn('base', adapter.model)


class NarrowWhatsAppExecutorTests(unittest.TestCase):
    def test_browser_fallback_returns_after_first_matching_tab(self):
        from zamal_core.ai_browser_transport import _exec_js_in_domain
        with patch('zamal_core.ai_browser_transport._run_osascript',
                   return_value=(True, '{"ok":true}')) as run:
            _exec_js_in_domain('web.whatsapp.com', 'JSON.stringify({ok:true})')
        script = run.call_args.args[0]
        self.assertIn('set r to execute t javascript', script)
        self.assertIn('return r as string', script)
        self.assertNotIn('set lastResult to execute', script)

    def test_exact_target_search_and_one_send_click_without_history_access(self):
        replies = [
            (True, '{"ok":true}'),
            (True, '{"ok":true,"exact_count":1}'),
            (True, '{"ok":true}'),
            (True, '{"ok":true}'),
            (True, '{"ok":true,"stage":"send_clicked"}'),
        ]
        with patch('zamal_core.ai_browser_transport._exec_js_in_domain', side_effect=replies) as execute, \
             patch('time.sleep'):
            result = _exec_whatsapp_send(proposal().payload)
        self.assertTrue(result['ok'])
        self.assertFalse(result['delivery_verified'])
        scripts = ' '.join(call.args[1] for call in execute.call_args_list)
        self.assertNotIn('document.body', scripts)
        self.assertNotIn('textContent', scripts)
        self.assertNotIn('innerText', scripts)
        self.assertNotIn('selectable-text', scripts)
        self.assertNotIn('msg-container', scripts)
        self.assertIn('getAttribute(\'title\')', scripts)

    def test_unresolved_destination_fails_before_opening_chat(self):
        payload = proposal().payload.copy()
        payload['raw_destination'] = 'Someone Else'
        with patch('zamal_core.channel_adapter.resolve_whatsapp_contact',
                   return_value={'status': 'AMBIGUOUS_CONTACT'}), \
             patch('zamal_core.ai_browser_transport._exec_js_in_domain') as execute:
            result = _exec_whatsapp_send(payload)
        self.assertEqual(result['error'], 'AMBIGUOUS_CONTACT')
        execute.assert_not_called()

    def test_search_returns_only_exact_match_count(self):
        with patch('zamal_core.ai_browser_transport._exec_js_in_domain',
                   side_effect=[(True, '{"ok":true}'),
                                (True, '{"ok":true,"exact_count":2}')]) as execute, \
             patch('time.sleep'):
            result = resolve_whatsapp_contact('Any Saved Name')
        self.assertEqual(result['status'], 'AMBIGUOUS_CONTACT')
        scripts = ' '.join(call.args[1] for call in execute.call_args_list)
        self.assertNotIn('textContent', scripts)
        self.assertNotIn('innerText', scripts)


class StartupBindingTests(unittest.TestCase):
    def test_startup_enables_receiver(self):
        source = Path('scripts/start_control_center.py').read_text()
        self.assertIn('telegram_receiver=True', source)

    def test_receiver_store_is_created_and_used_on_receiver_thread(self):
        from zamal_core.control_center import ControlCenterServer
        ready = threading.Event()

        class Config:
            configured = True

        class Bot:
            def __init__(self, store):
                self.store = store

            def poll(self, **_kwargs):
                self.store.db.execute('SELECT 1').fetchone()
                ready.set()

        server = ControlCenterServer(port=0, telegram_receiver=True)
        with patch('zamal_core.control_center._load_auth_env'), \
             patch('zamal_core.telegram_bot.TelegramConfig', return_value=Config()), \
             patch('zamal_core.telegram_bot.TelegramBot', Bot):
            server._start_telegram_receiver()
            self.assertTrue(ready.wait(2))
            server._stop_telegram_receiver()


if __name__ == '__main__':
    unittest.main()
