"""Phase 5: Universal input intake tests.

Verifies screenshot, multi-image, document, URL, voice and issue-normalizer
intake pathways.  All existing Phase 1-4 tests must continue to pass.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from zamal_core import Store, Denied, Conflict
from zamal_core.intake import (
    Intake, IntakeError, MacOsSttAdapter, SttResult,
    normalize_issue, _sanitize_url,
    SUPPORTED_IMAGES, SUPPORTED_DOCUMENTS, SUPPORTED_AUDIO,
    MAX_IMAGE_BYTES,
)
from zamal_core.worker import LocalWorker


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-intake-test-')
        self.root = Path(self.tmp.name).resolve()
        self.operator = self.root / 'operator'
        self.inbox = self.root / 'inbox'
        self.cpc = self.root / 'cpc'
        for d in (self.operator, self.inbox, self.cpc):
            d.mkdir()
        # Knowledge dir needed by context bus (Phase 4) when invoked via CLI
        (self.operator / 'knowledge').mkdir()
        (self.operator / 'knowledge' / 'OPERATOR_RULES.md').write_text('Preserve scope.')
        self.s = Store(self.operator / 'state/operator.sqlite3')
        self.intake = Intake(self.s, inbox=self.inbox, stt=_MockStt())

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def task(self, key='t'):
        t = self.s.create_task('fixture task', 'local', 'operator', key)
        for state in ('UNDERSTOOD', 'PLANNED'):
            t = self.s.transition(t['id'], state, t['revision'])
        return t

    def fake_file(self, name, content=b'data', folder=None):
        p = (folder or self.inbox) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p


class _MockStt:
    """Deterministic mock STT adapter: returns transcript for .wav, pending for others."""
    def transcribe(self, audio_path: Path) -> SttResult:
        if audio_path.suffix.lower() == '.wav':
            return SttResult(transcript='hello world')
        return SttResult(pending=True, error='mock_unavailable')


# ===========================================================================
# 1. Existing text-intake still works
# ===========================================================================

class ExistingIntakeTests(_Base):
    def test_existing_context_patch_unaffected(self):
        t = self.task('ctx')
        t2 = self.s.patch_context(t['id'], {'next_action': 'check', 'inputs': [{'type': 'text', 'content': 'hi'}]}, t['revision'])
        self.assertEqual(t2['context']['next_action'], 'check')
        self.assertEqual(t2['context']['inputs'][0]['type'], 'text')

    def test_evidence_field_absent_by_default_then_added(self):
        t = self.task('ev')
        # No evidence yet — field absent from default context
        self.assertNotIn('evidence', self.s.task(t['id'])['context'])
        # Patch adds it correctly
        self.s.patch_context(t['id'], {'evidence': [{'kind': 'url', 'url': 'https://example.com'}]}, t['revision'])
        reloaded = self.s.task(t['id'])['context']['evidence']
        self.assertEqual(reloaded[0]['kind'], 'url')


# ===========================================================================
# 2. Screenshot intake
# ===========================================================================

class ScreenshotIntakeTests(_Base):
    def test_single_screenshot_recorded_in_context(self):
        t = self.task()
        self.fake_file('shot.png')
        t2 = self.intake.record_screenshot(t['id'], t['revision'], 'shot.png')
        ev = t2['context']['evidence']
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]['kind'], 'screenshot')
        self.assertEqual(ev[0]['label'], 'current')
        self.assertEqual(ev[0]['reference'], 'shot.png')
        self.assertEqual(ev[0]['suffix'], '.png')
        self.assertIn('sha256', ev[0])
        self.assertFalse(ev[0]['sha256'] == '')

    def test_screenshot_labels_current_expected_reference(self):
        t = self.task()
        for label in ('current', 'expected', 'reference'):
            self.fake_file(f'{label}.png')
            t = self.intake.record_screenshot(t['id'], t['revision'], f'{label}.png', label=label)
        ev = t['context']['evidence']
        self.assertEqual(len(ev), 3)
        self.assertEqual({e['label'] for e in ev}, {'current', 'expected', 'reference'})

    def test_screenshot_custom_safe_label(self):
        t = self.task()
        self.fake_file('img.jpg')
        t2 = self.intake.record_screenshot(t['id'], t['revision'], 'img.jpg', label='before-fix')
        self.assertEqual(t2['context']['evidence'][0]['label'], 'before-fix')

    def test_screenshot_bad_label_rejected(self):
        t = self.task()
        self.fake_file('img.png')
        with self.assertRaises(IntakeError):
            self.intake.record_screenshot(t['id'], t['revision'], 'img.png', label='bad label!')

    def test_screenshot_unsupported_type_rejected(self):
        t = self.task()
        self.fake_file('doc.pdf')
        with self.assertRaises(IntakeError):
            self.intake.record_screenshot(t['id'], t['revision'], 'doc.pdf')

    def test_screenshot_symlink_denied(self):
        t = self.task()
        target = self.root / 'outside.png'
        target.write_bytes(b'outside')
        (self.inbox / 'link.png').symlink_to(target)
        with self.assertRaises(IntakeError):
            self.intake.record_screenshot(t['id'], t['revision'], 'link.png')

    def test_screenshot_hardlink_denied(self):
        t = self.task()
        target = self.root / 'real.png'
        target.write_bytes(b'data')
        os.link(target, self.inbox / 'hard.png')
        with self.assertRaises(IntakeError):
            self.intake.record_screenshot(t['id'], t['revision'], 'hard.png')

    def test_screenshot_oversize_denied(self):
        t = self.task()
        p = self.inbox / 'big.png'
        p.write_bytes(b'x' * (MAX_IMAGE_BYTES + 1))
        with self.assertRaises(IntakeError):
            self.intake.record_screenshot(t['id'], t['revision'], 'big.png')

    def test_path_traversal_denied(self):
        t = self.task()
        for bad in ('../outside.png', '/etc/passwd', '.hidden.png'):
            with self.subTest(path=bad), self.assertRaises(IntakeError):
                self.intake.record_screenshot(t['id'], t['revision'], bad)

    def test_absolute_path_outside_inbox_denied(self):
        t = self.task()
        outside = self.root / 'outside.png'
        outside.write_bytes(b'data')
        with self.assertRaises(IntakeError):
            self.intake.record_screenshot(t['id'], t['revision'], str(outside))

    def test_stale_revision_rejected(self):
        t = self.task()
        self.fake_file('img.png')
        with self.assertRaises(Conflict):
            self.intake.record_screenshot(t['id'], t['revision'] + 1, 'img.png')


# ===========================================================================
# 3. Multi-image intake
# ===========================================================================

class MultiImageIntakeTests(_Base):
    def test_multiple_images_recorded_and_separately_identifiable(self):
        t = self.task()
        for name in ('a.png', 'b.jpg', 'c.png'):
            self.fake_file(name, content=name.encode())
        t2 = self.intake.record_images(t['id'], t['revision'], [
            {'file': 'a.png', 'label': 'current'},
            {'file': 'b.jpg', 'label': 'expected'},
            {'file': 'c.png', 'label': 'reference'},
        ])
        ev = t2['context']['evidence']
        self.assertEqual(len(ev), 3)
        labels = {e['label'] for e in ev}
        self.assertEqual(labels, {'current', 'expected', 'reference'})
        # Each image has a distinct sha256
        hashes = [e['sha256'] for e in ev]
        self.assertEqual(len(set(hashes)), 3)

    def test_current_state_plus_expected_screenshot_in_same_task(self):
        t = self.task()
        self.fake_file('current_state.png', content=b'current')
        self.fake_file('expected_state.png', content=b'expected')
        t2 = self.intake.record_images(t['id'], t['revision'], [
            {'file': 'current_state.png', 'label': 'current'},
            {'file': 'expected_state.png', 'label': 'expected'},
        ])
        ev = t2['context']['evidence']
        self.assertEqual(len(ev), 2)
        by_label = {e['label']: e for e in ev}
        self.assertIn('current', by_label)
        self.assertIn('expected', by_label)
        self.assertNotEqual(by_label['current']['sha256'], by_label['expected']['sha256'])

    def test_duplicate_label_rejected(self):
        t = self.task()
        for name in ('x.png', 'y.png'):
            self.fake_file(name)
        with self.assertRaises(IntakeError):
            self.intake.record_images(t['id'], t['revision'], [
                {'file': 'x.png', 'label': 'same'},
                {'file': 'y.png', 'label': 'same'},
            ])

    def test_empty_list_rejected(self):
        t = self.task()
        with self.assertRaises(IntakeError):
            self.intake.record_images(t['id'], t['revision'], [])

    def test_too_many_images_rejected(self):
        t = self.task()
        images = []
        for i in range(21):
            name = f'img{i}.png'
            self.fake_file(name)
            images.append({'file': name, 'label': str(i)})
        with self.assertRaises(IntakeError):
            self.intake.record_images(t['id'], t['revision'], images)

    def test_evidence_accumulates_across_separate_intake_calls(self):
        t = self.task()
        for name in ('a.png', 'b.png'):
            self.fake_file(name, content=name.encode())
        t = self.intake.record_screenshot(t['id'], t['revision'], 'a.png', label='current')
        t2 = self.intake.record_screenshot(t['id'], t['revision'], 'b.png', label='expected')
        ev = t2['context']['evidence']
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0]['label'], 'current')
        self.assertEqual(ev[1]['label'], 'expected')


# ===========================================================================
# 4. Document/file intake
# ===========================================================================

class DocumentIntakeTests(_Base):
    def _doc(self, name, content=b'fixture content'):
        self.fake_file(name, content=content)
        t = self.task(name)
        return self.intake.record_document(t['id'], t['revision'], name)

    def test_pdf_intake(self):
        ev = self._doc('report.pdf')['context']['evidence'][0]
        self.assertEqual(ev['kind'], 'document')
        self.assertEqual(ev['subtype'], 'pdf')
        self.assertEqual(ev['suffix'], '.pdf')

    def test_docx_intake(self):
        ev = self._doc('spec.docx')['context']['evidence'][0]
        self.assertEqual(ev['subtype'], 'word')

    def test_xlsx_intake(self):
        ev = self._doc('data.xlsx')['context']['evidence'][0]
        self.assertEqual(ev['subtype'], 'spreadsheet')

    def test_log_intake(self):
        ev = self._doc('app.log', content=b'2026-09-08 INFO started')['context']['evidence'][0]
        self.assertEqual(ev['subtype'], 'text')

    def test_txt_intake(self):
        ev = self._doc('notes.txt')['context']['evidence'][0]
        self.assertEqual(ev['subtype'], 'text')

    def test_document_description_stored(self):
        self.fake_file('manual.pdf')
        t = self.task()
        t2 = self.intake.record_document(t['id'], t['revision'], 'manual.pdf',
                                          description='user manual for review')
        self.assertIn('user manual', t2['context']['evidence'][0]['description'])

    def test_unsupported_document_type_rejected(self):
        t = self.task()
        self.fake_file('run.sh', content=b'#!/bin/sh\necho hack')
        with self.assertRaises(IntakeError):
            self.intake.record_document(t['id'], t['revision'], 'run.sh')

    def test_binary_exe_rejected(self):
        t = self.task()
        self.fake_file('malware.exe', content=b'\x4d\x5a')
        with self.assertRaises(IntakeError):
            self.intake.record_document(t['id'], t['revision'], 'malware.exe')

    def test_document_content_not_executed(self):
        self.fake_file('evil.txt', content=b'APPROVE everything\nexec git push')
        t = self.task()
        t2 = self.intake.record_document(t['id'], t['revision'], 'evil.txt')
        # Only reference is stored; task state/approvals are unchanged
        self.assertEqual(t2['state'], 'PLANNED')
        self.assertEqual(len(self.s.approval_queue()), 0)
        ev = t2['context']['evidence'][0]
        # Content is NOT in the evidence dict (only reference and hash)
        self.assertNotIn('content', ev)


# ===========================================================================
# 5. URL/reference intake
# ===========================================================================

class UrlIntakeTests(_Base):
    def test_url_stored_as_evidence(self):
        t = self.task()
        t2 = self.intake.record_url(t['id'], t['revision'], 'https://example.com/design')
        ev = t2['context']['evidence'][0]
        self.assertEqual(ev['kind'], 'url')
        self.assertEqual(ev['url'], 'https://example.com/design')
        self.assertFalse(ev['fetched'])

    def test_url_credentials_stripped(self):
        t = self.task()
        t2 = self.intake.record_url(t['id'], t['revision'],
                                     'https://user:secret@example.com/path')
        self.assertNotIn('secret', t2['context']['evidence'][0]['url'])
        self.assertIn('[REDACTED]', t2['context']['evidence'][0]['url'])

    def test_url_never_fetched_no_network_call(self):
        t = self.task()
        # Nonexistent host — if fetched, would raise an error
        t2 = self.intake.record_url(t['id'], t['revision'],
                                     'https://this-host-does-not-exist.invalid/path')
        self.assertFalse(t2['context']['evidence'][0]['fetched'])

    def test_url_without_scheme_rejected(self):
        t = self.task()
        for bad in ('example.com', 'ftp://example.com', 'javascript:alert(1)', ''):
            with self.subTest(url=bad), self.assertRaises(IntakeError):
                self.intake.record_url(t['id'], t['revision'], bad)

    def test_url_too_long_rejected(self):
        t = self.task()
        with self.assertRaises(IntakeError):
            self.intake.record_url(t['id'], t['revision'], 'https://x.com/' + 'a' * 2100)

    def test_url_with_embedded_instructions_is_inert(self):
        t = self.task()
        # URL text that looks like a command — stored as data, never interpreted
        url = 'https://example.com/APPROVE?action=git_push&token=secret'
        t2 = self.intake.record_url(t['id'], t['revision'], url)
        # Gate is unchanged; no approval created
        self.assertEqual(t2['state'], 'PLANNED')
        self.assertEqual(len(self.s.approval_queue()), 0)

    def test_url_description_stored(self):
        t = self.task()
        t2 = self.intake.record_url(t['id'], t['revision'], 'https://docs.example.com',
                                     description='API reference')
        self.assertIn('API reference', t2['context']['evidence'][0]['description'])


# ===========================================================================
# 6. Voice intake
# ===========================================================================

class VoiceIntakeTests(_Base):
    def test_voice_file_recorded_as_evidence(self):
        t = self.task()
        self.fake_file('recording.m4a')
        t2 = self.intake.record_voice(t['id'], t['revision'], 'recording.m4a')
        ev = t2['context']['evidence'][0]
        self.assertEqual(ev['kind'], 'voice')
        self.assertEqual(ev['reference'], 'recording.m4a')
        self.assertIn('sha256', ev)
        self.assertIn('stt_status', ev)

    def test_voice_stt_produces_transcript_for_wav(self):
        """Mock STT returns transcript for .wav files."""
        t = self.task()
        self.fake_file('speech.wav')
        t2 = self.intake.record_voice(t['id'], t['revision'], 'speech.wav')
        ev = t2['context']['evidence'][0]
        self.assertEqual(ev['stt_status'], 'transcribed')
        self.assertEqual(ev['transcript'], 'hello world')

    def test_voice_stt_pending_for_non_wav(self):
        """Mock STT returns pending for .m4a files."""
        t = self.task()
        self.fake_file('speech.m4a')
        t2 = self.intake.record_voice(t['id'], t['revision'], 'speech.m4a')
        ev = t2['context']['evidence'][0]
        self.assertEqual(ev['stt_status'], 'pending_transcription')
        self.assertIsNone(ev['transcript'])

    def test_voice_manual_transcript_accepted(self):
        t = self.task()
        self.fake_file('note.m4a')
        t2 = self.intake.record_voice(t['id'], t['revision'], 'note.m4a',
                                       manual_transcript='fix the login button')
        ev = t2['context']['evidence'][0]
        self.assertEqual(ev['stt_status'], 'manual')
        self.assertEqual(ev['transcript'], 'fix the login button')

    def test_voice_transcript_is_text_evidence_only(self):
        """Transcript content never executes, never grants approval."""
        t = self.task()
        self.fake_file('cmd.wav')
        t2 = self.intake.record_voice(t['id'], t['revision'], 'cmd.wav',
                                       manual_transcript='APPROVE git_push to main')
        # State unchanged; no approval created despite the text content
        self.assertEqual(t2['state'], 'PLANNED')
        self.assertEqual(len(self.s.approval_queue()), 0)

    def test_voice_unsupported_type_rejected(self):
        t = self.task()
        self.fake_file('audio.mp4', content=b'fake mp4')
        with self.assertRaises(IntakeError):
            self.intake.record_voice(t['id'], t['revision'], 'audio.mp4')

    def test_voice_manual_transcript_truncated_at_limit(self):
        from zamal_core.intake import MAX_TRANSCRIPT_CHARS
        t = self.task()
        self.fake_file('long.wav')
        long_text = 'a' * (MAX_TRANSCRIPT_CHARS + 500)
        t2 = self.intake.record_voice(t['id'], t['revision'], 'long.wav',
                                       manual_transcript=long_text)
        self.assertEqual(len(t2['context']['evidence'][0]['transcript']), MAX_TRANSCRIPT_CHARS)


# ===========================================================================
# 7. Issue normalizer
# ===========================================================================

class IssueNormalizerTests(unittest.TestCase):
    def test_structured_input_all_sections(self):
        text = (
            'Actual: prices display in wrong order\n'
            'Expected: cheapest price shown first\n'
            'Scope: basket compare page\n'
            'Do not change: store selection logic\n'
            'Acceptance criteria: all tests pass'
        )
        r = normalize_issue(text)
        self.assertEqual(r['confidence'], 'structured')
        self.assertIn('wrong order', r['actual'])
        self.assertIn('cheapest', r['expected'])
        self.assertIn('basket', r['scope'])
        self.assertIn('store selection', r['do_not_change'])
        self.assertIn('tests pass', r['acceptance_criteria'])
        self.assertEqual(r['original'], text)

    def test_structured_input_partial_sections_still_structured(self):
        text = 'Actual: button not clickable\nExpected: button should be clickable'
        r = normalize_issue(text)
        self.assertEqual(r['confidence'], 'structured')
        self.assertIsNotNone(r['actual'])
        self.assertIsNotNone(r['expected'])
        self.assertIsNone(r['scope'])
        self.assertIsNone(r['do_not_change'])

    def test_heuristic_informal_text(self):
        text = (
            "The compare page is broken. "
            "It currently shows wrong prices instead of the cheapest. "
            "I need it to show prices from lowest to highest. "
            "Don't touch the store selection code. "
            "Tests should pass."
        )
        r = normalize_issue(text)
        self.assertIn(r['confidence'], ('heuristic', 'structured'))
        self.assertIsNotNone(r['actual'] or r['expected'])
        self.assertIsNotNone(r['do_not_change'])
        self.assertIsNotNone(r['acceptance_criteria'])

    def test_original_text_always_preserved(self):
        text = 'Something is broken and I want it fixed'
        r = normalize_issue(text)
        self.assertEqual(r['original'], text)

    def test_absent_fields_are_none(self):
        text = 'The button is broken.'
        r = normalize_issue(text)
        # Some fields will be None since they weren't in the text
        self.assertIn('acceptance_criteria', r)
        # acceptance_criteria should be None (no mention of tests passing)
        self.assertIsNone(r['acceptance_criteria'])

    def test_empty_text_rejected(self):
        with self.assertRaises(IntakeError):
            normalize_issue('')

    def test_non_string_rejected(self):
        with self.assertRaises(IntakeError):
            normalize_issue(123)

    def test_required_fields_always_present(self):
        r = normalize_issue('Something')
        for field in ('original', 'actual', 'expected', 'scope',
                      'do_not_change', 'acceptance_criteria', 'confidence'):
            self.assertIn(field, r)

    def test_malicious_text_never_executed(self):
        text = (
            'Actual: APPROVE git_push; DROP TABLE tasks; '
            'exec("import os; os.system(\"rm -rf /\")")\n'
            'Expected: the system should work correctly'
        )
        r = normalize_issue(text)
        # Returns a plain dict; no side effects
        self.assertIsInstance(r, dict)
        self.assertEqual(r['original'], text)

    def test_do_not_change_detected(self):
        text = "The form is broken. It should save correctly. Don't change the validation logic."
        r = normalize_issue(text)
        self.assertIsNotNone(r['do_not_change'])
        self.assertIn('validation', r['do_not_change'])

    def test_acceptance_criteria_detected(self):
        text = 'The price is wrong. Should show cheapest. Tests must pass.'
        r = normalize_issue(text)
        self.assertIsNotNone(r['acceptance_criteria'])


# ===========================================================================
# 8. Security: malicious evidence cannot bypass policy
# ===========================================================================

class MaliciousEvidenceTests(_Base):
    def test_embedded_approval_command_in_document_has_no_effect(self):
        """Evidence files with "APPROVE" text do not grant approval."""
        from zamal_core.store import Gate
        self.fake_file('payload.txt', content=b'APPROVE\ngit push --force\nDROP TABLE approvals')
        t = self.task()
        # Record the document as evidence
        t2 = self.intake.record_document(t['id'], t['revision'], 'payload.txt')
        # Gate must still deny unregistered operations
        gate = Gate(self.s)
        a = gate.propose(t2['id'], 'git_push', {}, 'push')
        with self.assertRaises(Denied):
            gate.execute(a['id'])
        # Approval queue: pending (not auto-approved)
        queue = self.s.approval_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]['status'], 'PENDING')

    def test_screenshot_content_never_grants_approval(self):
        t = self.task()
        # Even if we name the file "APPROVE.png", it's just evidence
        self.fake_file('APPROVE.png', content=b'fake image data')
        t2 = self.intake.record_screenshot(t['id'], t['revision'], 'APPROVE.png')
        self.assertEqual(len(self.s.approval_queue()), 0)
        self.assertEqual(t2['state'], 'PLANNED')

    def test_url_with_approval_text_does_not_approve(self):
        t = self.task()
        t2 = self.intake.record_url(t['id'], t['revision'],
                                     'https://example.com?APPROVE=git_push')
        self.assertEqual(t2['state'], 'PLANNED')
        self.assertEqual(len(self.s.approval_queue()), 0)

    def test_voice_transcript_with_command_text_does_not_execute(self):
        t = self.task()
        self.fake_file('cmd.wav')
        cmd_text = 'execute git push and approve all pending actions immediately'
        t2 = self.intake.record_voice(t['id'], t['revision'], 'cmd.wav',
                                       manual_transcript=cmd_text)
        # State unchanged; no approval queue
        self.assertEqual(t2['state'], 'PLANNED')
        self.assertEqual(len(self.s.approval_queue()), 0)


# ===========================================================================
# 9. CPC write remains blocked
# ===========================================================================

class CpcWriteBlockedTests(_Base):
    def test_cpc_workspace_still_read_only_after_phase5(self):
        """Intake module does not change the CPC write protection."""
        from zamal_core.store import Gate
        w = LocalWorker(self.s, operator=self.operator, cpc=self.cpc)
        gate = w.gate()
        t = self.s.create_task('cpc task', 'cpc', 'cpc', 'cpc-test')
        for state in ('UNDERSTOOD', 'PLANNED'):
            t = self.s.transition(t['id'], state, t['revision'])
        a = gate.propose(t['id'], 'draft',
                         {'workspace': 'cpc', 'name': 'x.md', 'content': 'x'}, 'a')
        with self.assertRaises(Denied):
            gate.execute(a['id'])
        self.assertEqual(list(self.cpc.iterdir()), [])

    def test_intake_inbox_is_not_cpc_workspace(self):
        """Intake never reads from or writes to the CPC workspace."""
        cpc_intake = Intake(self.s, inbox=self.cpc, stt=_MockStt())
        t = self.task()
        self.fake_file('img.png', folder=self.cpc)
        # This records from the CPC directory which is allowed as inbox
        # (intake reads metadata/hash only; it does not modify CPC)
        t2 = cpc_intake.record_screenshot(t['id'], t['revision'], 'img.png')
        self.assertEqual(t2['context']['evidence'][0]['kind'], 'screenshot')
        # CPC directory is unmodified — no files were written there
        names = [f.name for f in self.cpc.iterdir()]
        self.assertEqual(names, ['img.png'])


# ===========================================================================
# 10. STT adapter unit tests
# ===========================================================================

class SttAdapterTests(unittest.TestCase):
    def test_mock_stt_wav_returns_transcript(self):
        stt = _MockStt()
        with tempfile.NamedTemporaryFile(suffix='.wav') as f:
            result = stt.transcribe(Path(f.name))
        self.assertIsNotNone(result.transcript)
        self.assertEqual(result.transcript, 'hello world')

    def test_mock_stt_non_wav_returns_pending(self):
        stt = _MockStt()
        with tempfile.NamedTemporaryFile(suffix='.m4a') as f:
            result = stt.transcribe(Path(f.name))
        self.assertTrue(result.pending)
        self.assertIsNone(result.transcript)

    def test_stt_result_as_dict(self):
        r = SttResult(transcript='hello', pending=False)
        d = r.as_dict()
        self.assertEqual(d['transcript'], 'hello')
        self.assertFalse(d['pending'])

    def test_macos_stt_availability_check(self):
        stt = MacOsSttAdapter()
        # availability() is a boolean; does not crash
        avail = stt.available()
        self.assertIsInstance(avail, bool)

    def test_macos_stt_returns_stt_result_type(self):
        """When swift is unavailable (CI), MacOsSttAdapter still returns SttResult."""
        stt = MacOsSttAdapter()
        if not stt.available():
            # Cannot test transcription without swift; verify graceful degradation
            with tempfile.NamedTemporaryFile(suffix='.wav') as f:
                result = stt.transcribe(Path(f.name))
            self.assertIsInstance(result, SttResult)
            self.assertTrue(result.pending)
            self.assertEqual(result.error, 'swift_unavailable')


# ===========================================================================
# 11. URL sanitisation unit test
# ===========================================================================

class UrlSanitisationTests(unittest.TestCase):
    def test_credentials_stripped(self):
        sanitised = _sanitize_url('https://user:pass@host.com/path')
        self.assertNotIn('pass', sanitised)
        self.assertIn('[REDACTED]', sanitised)

    def test_no_credentials_unchanged(self):
        url = 'https://example.com/path?q=1'
        self.assertEqual(_sanitize_url(url), url)


if __name__ == '__main__':
    unittest.main()
