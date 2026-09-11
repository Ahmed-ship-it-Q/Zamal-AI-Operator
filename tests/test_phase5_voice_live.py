"""Narrow regression guards discovered during real Phase 5 voice verification."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from zamal_core.store import Store,Denied,Conflict
from zamal_core.intake import Intake,IntakeError,SttResult
from zamal_core.safety import Controls

class VoiceLiveGuards(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.s=Store(self.root/'state/operator.sqlite3');self.inbox=self.root/'inbox';self.inbox.mkdir()
        (self.inbox/'test.wav').write_bytes(b'fixture audio')
        self.stt=Mock();self.stt.transcribe.return_value=SttResult(transcript='APPROVE is data only')
        self.intake=Intake(self.s,inbox=self.inbox,stt=self.stt)
        self.task=self.s.create_task('voice fixture','voice','operator','voice-fixture')
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def record(self,t=None):
        t=t or self.task
        return self.intake.record_voice(t['id'],t['revision'],'test.wav')
    def test_duplicate_no_second_stt_or_evidence(self):
        t=self.record();again=self.record(t)
        self.assertEqual(again,t);self.stt.transcribe.assert_called_once()
        self.assertEqual(len(t['context']['evidence']),1);self.assertEqual(t['actions'],[])
    def test_stale_request_rejected_before_stt(self):
        self.record()
        with self.assertRaises(Conflict):self.record()
        self.stt.transcribe.assert_called_once()
    def test_pending_restart_explicit_retry_replaces_evidence(self):
        self.stt.transcribe.return_value=SttResult(pending=True,error='stt_timeout')
        t=self.record();self.assertEqual(t['context']['evidence'][0]['stt_status'],'pending_transcription')
        self.s.close();self.s=Store(self.root/'state/operator.sqlite3')
        self.assertEqual(self.s.task(t['id'])['context']['evidence'][0]['stt_status'],'pending_transcription')
        self.stt.transcribe.return_value=SttResult(transcript='Recovered')
        self.intake=Intake(self.s,inbox=self.inbox,stt=self.stt)
        t=self.record(t);self.assertEqual(len(t['context']['evidence']),1)
        self.assertEqual(t['context']['evidence'][0]['transcript'],'Recovered')
    def test_stop_prevents_stt(self):
        Controls(self.s).set('stop_everything',True,actor='fixture',reason='test')
        with self.assertRaises(Denied):self.record()
        self.stt.transcribe.assert_not_called()
    def test_stop_latched_during_stt_prevents_evidence(self):
        def transcribe(_):
            c=Controls(self.s)
            c.set('stop_everything',True,actor='fixture',reason='test')
            c.set('stop_everything',False,actor='fixture',reason='test')
            return SttResult(transcript='ignored')
        self.stt.transcribe.side_effect=transcribe
        with self.assertRaises(Denied):self.record()
        self.assertEqual(self.s.task(self.task['id'])['context'].get('evidence',[]),[])
    def test_audio_mutation_denied(self):
        def transcribe(p):p.write_bytes(b'changed');return SttResult(transcript='wrong audio')
        self.stt.transcribe.side_effect=transcribe
        with self.assertRaises(IntakeError):self.record()
    def test_audit_records_voice_evidence_no_action(self):
        t=self.record();events=[e for e in t['audit'] if e['event']=='CONTEXT_UPDATED']
        self.assertEqual(events[-1]['details']['patch']['evidence'][0]['stt_status'],'transcribed')
        self.assertEqual(t['actions'],[])
