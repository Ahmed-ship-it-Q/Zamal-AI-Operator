"""Phase 5: Universal input intake — screenshots, images, documents, URLs, voice.

All evidence is recorded as task context data, never executed or interpreted
as commands. File content, URLs, voice transcripts and embedded text never
grant approval, bypass the gate, or affect CPC project state.
"""
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from .store import Store

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 8000
MAX_URL_LENGTH = 2048
MAX_DESCRIPTION_CHARS = 1000

SUPPORTED_IMAGES = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.tif',
})
SUPPORTED_DOCUMENTS = frozenset({
    '.pdf', '.docx', '.xlsx', '.xls', '.doc',
    '.txt', '.log', '.csv', '.md', '.json', '.yaml', '.yml',
})
SUPPORTED_AUDIO = frozenset({
    '.wav', '.m4a', '.mp3', '.aiff', '.aac', '.ogg', '.flac',
})

_URL_RE = re.compile(r'^https?://.', re.I)
_CREDENTIAL_URL = re.compile(r'(https?://)[^\s/@:]+:[^\s/@]+@', re.I)


class IntakeError(ValueError):
    """Raised when evidence intake validation fails."""


# ---------------------------------------------------------------------------
# File validation helpers
# ---------------------------------------------------------------------------

def _validate_file(path: Path, max_bytes: int, allowed: frozenset) -> dict:
    """Check file is safe to record as evidence.  Never executes content."""
    suffix = path.suffix.lower()
    if suffix not in allowed:
        raise IntakeError(f'Unsupported file type: {suffix!r}')
    if path.is_symlink():
        raise IntakeError('Evidence path must not be a symlink')
    if not path.exists() or not path.is_file():
        raise IntakeError('Evidence file not found')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise IntakeError('Evidence must be a regular file')
    if info.st_nlink != 1:
        raise IntakeError('Evidence file must not have multiple hard links')
    if info.st_size > max_bytes:
        raise IntakeError(f'Evidence file exceeds limit ({max_bytes} bytes)')
    return {'size': info.st_size}


def _sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _sanitize_url(url: str) -> str:
    """Remove credentials from a URL.  The URL is never fetched."""
    return _CREDENTIAL_URL.sub(r'\1[REDACTED]@', url)


# ---------------------------------------------------------------------------
# Issue normalizer
# ---------------------------------------------------------------------------

_SECTION_HEADINGS = {
    'actual': re.compile(
        r'(?im)^[ \t]*(?:actual|currently?|bug|issue|problem|what(?:\'s|[ \t]+is)[ \t]+happening)[ \t]*[:\-][ \t]*'),
    'expected': re.compile(
        r'(?im)^[ \t]*(?:expected?|desired|should|correct[ \t]+behavior|goal|want(?:ed)?)[ \t]*[:\-][ \t]*'),
    'scope': re.compile(
        r'(?im)^[ \t]*(?:scope|affects?|limited[ \t]+to|files?)[ \t]*[:\-][ \t]*'),
    'do_not_change': re.compile(
        r'(?im)^[ \t]*(?:do[ \t]*not[ \t]*(?:change|touch|break|modify)|don\'?t[ \t]*(?:change|touch|break|modify)|preserve|keep[ \t]+unchanged)[ \t]*[:\-][ \t]*'),
    'acceptance_criteria': re.compile(
        r'(?im)^[ \t]*(?:acceptance[ \t]+criteria|done[ \t]+when|passes?[ \t]+when|verified?[ \t]+when|a\.?c\.?)[ \t]*[:\-][ \t]*'),
}

_HINT_ACTUAL = re.compile(
    r'(?i)(?:currently|right now|(?:is|are|the\s+\w+)\s+(?:not\s+|incorrectly\s+)?(?:showing|displaying|returning|broken|failing|wrong)|(?:bug|issue|problem|error)\s*(?:is|:)|(?:doesn\'?t|does\s+not|isn\'?t|is\s+not|can\'?t|cannot)\s)',
)
_HINT_EXPECTED = re.compile(
    r'(?i)(?:should|must|needs?\s+(?:\w+\s+)?to|ought\s+to|want(?:s?)?\s+(?:\w+\s+)?to|expected?\s+(?:to|behavior)|instead(?:\s+of)?|correct(?:ly)?)',
)
_HINT_DNC = re.compile(
    r'(?i)(?:don\'?t\s+(?:change|touch|break|modify|alter)|do\s+not\s+(?:change|touch|break|modify|alter)|preserve|keep\s+(?:it|the|them)?\s*(?:the\s+same|unchanged|intact|as[\s-]is)|leave\s+(?:alone|intact|unchanged))',
)
_HINT_AC = re.compile(
    r'(?i)(?:tests?\s+(?:pass|should\s+pass|must\s+pass)|verified?\s+when|done\s+when|passes?\s+when|acceptance\s+criteria)',
)


def normalize_issue(text: str) -> dict:
    """Convert informal user text into the canonical issue structure.

    Returns a dict with keys: original, actual, expected, scope,
    do_not_change, acceptance_criteria, confidence.
    Fields absent from the input are None.
    Input text is never executed.
    """
    if not isinstance(text, str):
        raise IntakeError('Issue text must be a string')
    text = text.strip()
    if not text:
        raise IntakeError('Issue text must not be empty')

    result = {
        'original': text,
        'actual': None,
        'expected': None,
        'scope': None,
        'do_not_change': None,
        'acceptance_criteria': None,
        'confidence': 'minimal',
    }

    # Pass 1: explicit section headings
    padded = '\n' + text
    boundaries = []
    for field, pat in _SECTION_HEADINGS.items():
        for m in pat.finditer(padded):
            boundaries.append((m.start(), m.end(), field))
    boundaries.sort(key=lambda x: x[0])

    if len(boundaries) >= 2:
        sections: dict = {}
        for i, (start, end, field) in enumerate(boundaries):
            next_start = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(padded)
            content = padded[end:next_start].strip()
            if content and field not in sections:
                sections[field] = content
        if len(sections) >= 2:
            result.update(sections)
            result['confidence'] = 'structured'
            return result

    # Pass 2: sentence-level heuristics
    sentences = re.split(r'(?<=[.!?])\s+|\n+', text)
    actual_s: list = []
    expected_s: list = []
    dnc_s: list = []
    ac_s: list = []
    other_s: list = []

    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if _HINT_DNC.search(s):
            dnc_s.append(s)
        elif _HINT_AC.search(s):
            ac_s.append(s)
        elif _HINT_ACTUAL.search(s):
            actual_s.append(s)
        elif _HINT_EXPECTED.search(s):
            expected_s.append(s)
        else:
            other_s.append(s)

    # Distribute uncategorised sentences by position
    if other_s and not actual_s and not expected_s:
        mid = max(1, len(other_s) // 2)
        actual_s = other_s[:mid]
        expected_s = other_s[mid:]
    elif other_s and not actual_s:
        actual_s = other_s

    if any([actual_s, expected_s, dnc_s, ac_s]):
        result['confidence'] = 'heuristic'

    if actual_s:
        result['actual'] = ' '.join(actual_s)
    if expected_s:
        result['expected'] = ' '.join(expected_s)
    if dnc_s:
        result['do_not_change'] = ' '.join(dnc_s)
    if ac_s:
        result['acceptance_criteria'] = ' '.join(ac_s)

    return result


# ---------------------------------------------------------------------------
# STT adapter
# ---------------------------------------------------------------------------

class SttResult:
    def __init__(self, *, transcript=None, pending=False, error=None):
        self.transcript = transcript
        self.pending = pending
        self.error = error

    def as_dict(self) -> dict:
        return {'transcript': self.transcript, 'pending': self.pending, 'error': self.error}


class MacOsSttAdapter:
    """Best-effort speech-to-text via macOS SFSpeechRecognizer (swift subprocess).

    Returns SttResult(pending=True) when swift or speech permission is unavailable.
    The transcript is plain text; it is never executed or used as approval.
    """
    SWIFT = '/usr/bin/swift'
    TIMEOUT = 45  # compile + recognise

    _SCRIPT = r'''
import Foundation
import Speech

let args = CommandLine.arguments
guard args.count == 2 else {
    print(#"{"error":"usage"}"#); exit(1)
}
let fileURL = URL(fileURLWithPath: args[1])
guard let rec = SFSpeechRecognizer(locale: Locale(identifier: "en-US")),
      rec.isAvailable else {
    print(#"{"pending":true,"reason":"recognizer_unavailable"}"#); exit(0)
}
let req = SFSpeechURLRecognitionRequest(url: fileURL)
req.shouldReportPartialResults = false
var transcript = ""
var done = false
let mainLoop = CFRunLoopGetMain()
rec.recognitionTask(with: req) { result, _ in
    if let r = result, r.isFinal {
        transcript = r.bestTranscription.formattedString
        done = true
        CFRunLoopStop(mainLoop)
    } else if result == nil {
        done = true
        CFRunLoopStop(mainLoop)
    }
}
DispatchQueue.global().asyncAfter(deadline: .now() + 25) {
    if !done { done = true; CFRunLoopStop(mainLoop) }
}
CFRunLoopRun()
let safe = transcript
    .replacingOccurrences(of: "\\", with: "\\\\")
    .replacingOccurrences(of: "\"", with: "\\\"")
    .replacingOccurrences(of: "\n", with: "\\n")
if done && !transcript.isEmpty {
    print("{\"transcript\":\"\(safe)\"}")
} else {
    print(#"{"pending":true,"reason":"no_result"}"#)
}
'''

    def available(self) -> bool:
        p = Path(self.SWIFT)
        return p.is_file() and os.access(p, os.X_OK)

    def transcribe(self, audio_path: Path) -> SttResult:
        if not self.available():
            return SttResult(pending=True, error='swift_unavailable')
        with tempfile.NamedTemporaryFile(suffix='.swift', mode='w',
                                         delete=False, prefix='zamal_stt_') as f:
            f.write(self._SCRIPT)
            script = f.name
        try:
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()),
                   'LANG': 'en_US.UTF-8', 'TZ': 'Asia/Qatar',
                   'TMPDIR': tempfile.gettempdir()}
            r = subprocess.run([self.SWIFT, script, str(audio_path)],
                               capture_output=True, text=True,
                               timeout=self.TIMEOUT, env=env,
                               cwd='/', start_new_session=True)
            if r.returncode != 0:
                return SttResult(pending=True, error='swift_nonzero')
            data = json.loads(r.stdout.strip())
            if 'transcript' in data:
                return SttResult(transcript=str(data['transcript'])[:MAX_TRANSCRIPT_CHARS])
            return SttResult(pending=True, error=str(data.get('reason', 'unknown')))
        except subprocess.TimeoutExpired:
            return SttResult(pending=True, error='stt_timeout')
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return SttResult(pending=True, error='stt_error')
        finally:
            try:
                os.unlink(script)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

class Intake:
    """Record evidence for a task.

    Never executes evidence content.
    Never bypasses the approval gate or emergency controls.
    CPC workspace remains read-only; no writes are made to it here.
    """

    def __init__(self, store: Store, inbox: Path = None, stt=None):
        self.store = store
        self.inbox = Path(inbox) if inbox else Path(__file__).resolve().parent.parent / 'inbox'
        self.stt = stt if stt is not None else MacOsSttAdapter()

    def _resolve_path(self, ref: str) -> Path:
        if not isinstance(ref, str) or not ref.strip():
            raise IntakeError('File reference must be a non-empty string')
        if '\x00' in ref or '\\' in ref:
            raise IntakeError('File reference contains unsafe characters')
        if self.inbox.is_symlink():
            raise IntakeError('Intake inbox must not be a symlink')
        root = self.inbox.resolve()
        raw = Path(ref) if ref.startswith('/') else root / ref
        try:
            parts = raw.relative_to(root).parts
        except ValueError:
            raise IntakeError('File must be inside the intake inbox')
        if not parts or any(p in ('..', '.', '') or p.startswith('.') for p in parts):
            raise IntakeError('Path component not permitted')
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise IntakeError('Evidence path component must not be a symlink')
        try:
            current.resolve().relative_to(root)
        except ValueError:
            raise IntakeError('Evidence escapes inbox')
        return current

    def _append(self, task_id: str, revision: int, items: list) -> dict:
        task = self.store.task(task_id)
        current = list(task['context'].get('evidence', []))
        current.extend(items)
        return self.store.patch_context(task_id, {'evidence': current}, revision)

    # ------------------------------------------------------------------
    # Public intake methods
    # ------------------------------------------------------------------

    def record_screenshot(self, task_id: str, revision: int, file_ref: str,
                          label: str = 'current') -> dict:
        """Record a screenshot as evidence.

        label: 'current' (what is broken), 'expected' (desired state),
               'reference', or any safe [A-Za-z0-9_-]{1,50} identifier.
        File content is never executed or parsed for instructions.
        """
        if label not in {'current', 'expected', 'reference'} and \
                not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', label):
            raise IntakeError('label must be current/expected/reference or a safe identifier')
        p = self._resolve_path(file_ref)
        meta = _validate_file(p, MAX_IMAGE_BYTES, SUPPORTED_IMAGES)
        return self._append(task_id, revision, [{
            'kind': 'screenshot', 'label': label,
            'reference': file_ref, 'sha256': _sha256_path(p),
            'size': meta['size'], 'suffix': p.suffix.lower(),
            'recorded_at': time.time(),
        }])

    def record_images(self, task_id: str, revision: int, images: list) -> dict:
        """Record multiple images for one task.

        images: list of {'file': str, 'label': str}.
        Each image is separately identifiable by label and sha256.
        """
        if not isinstance(images, list) or not images:
            raise IntakeError('images must be a non-empty list')
        if len(images) > 20:
            raise IntakeError('At most 20 images per call')
        labels_seen: set = set()
        items = []
        for entry in images:
            if not isinstance(entry, dict) or 'file' not in entry:
                raise IntakeError("Each image entry must have a 'file' key")
            label = str(entry.get('label', 'image'))
            if label in labels_seen:
                raise IntakeError(f'Duplicate image label: {label!r}')
            labels_seen.add(label)
            p = self._resolve_path(entry['file'])
            meta = _validate_file(p, MAX_IMAGE_BYTES, SUPPORTED_IMAGES)
            items.append({
                'kind': 'image', 'label': label,
                'reference': entry['file'], 'sha256': _sha256_path(p),
                'size': meta['size'], 'suffix': p.suffix.lower(),
                'recorded_at': time.time(),
            })
        return self._append(task_id, revision, items)

    def record_document(self, task_id: str, revision: int, file_ref: str,
                        description: str = '') -> dict:
        """Record a PDF, DOCX, XLSX, log, or text file as evidence.

        Only the reference and hash are stored; content is never executed.
        """
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise IntakeError('Description too long')
        p = self._resolve_path(file_ref)
        meta = _validate_file(p, MAX_DOCUMENT_BYTES, SUPPORTED_DOCUMENTS)
        suffix = p.suffix.lower()
        subtype = ('text'        if suffix in {'.txt', '.log', '.md', '.csv', '.json', '.yaml', '.yml'} else
                   'pdf'         if suffix == '.pdf' else
                   'spreadsheet' if suffix in {'.xlsx', '.xls'} else
                   'word'        if suffix in {'.docx', '.doc'} else 'binary')
        return self._append(task_id, revision, [{
            'kind': 'document', 'subtype': subtype,
            'reference': file_ref, 'sha256': _sha256_path(p),
            'size': meta['size'], 'suffix': suffix,
            'description': description[:MAX_DESCRIPTION_CHARS],
            'recorded_at': time.time(),
        }])

    def record_url(self, task_id: str, revision: int, url: str,
                   description: str = '') -> dict:
        """Store a URL as task evidence.

        The URL is NEVER fetched, resolved, or used to bypass approval or safety.
        Embedded instructions or commands in the URL or description have no effect.
        """
        if not isinstance(url, str) or not url.strip():
            raise IntakeError('URL must be a non-empty string')
        url = url.strip()
        if len(url) > MAX_URL_LENGTH:
            raise IntakeError(f'URL exceeds {MAX_URL_LENGTH} character limit')
        if not _URL_RE.match(url):
            raise IntakeError('URL must begin with http:// or https://')
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise IntakeError('Description too long')
        return self._append(task_id, revision, [{
            'kind': 'url', 'url': _sanitize_url(url),
            'description': description[:MAX_DESCRIPTION_CHARS],
            'fetched': False,
            'recorded_at': time.time(),
        }])

    def record_voice(self, task_id: str, revision: int, file_ref: str,
                     manual_transcript: str = None) -> dict:
        """Accept a voice file; transcribe if possible, else mark pending.

        Transcript is plain text evidence only; it is never executed.
        Providing manual_transcript skips STT.
        """
        from .safety import Controls
        from .store import Conflict
        controls = Controls(self.store)
        baseline = controls.check('inspect')
        task = self.store.task(task_id)
        if task['revision'] != revision:
            raise Conflict('Task revision changed before voice intake')
        p = self._resolve_path(file_ref)
        meta = _validate_file(p, MAX_AUDIO_BYTES, SUPPORTED_AUDIO)
        sha = _sha256_path(p)
        current = list(task['context'].get('evidence', []))
        previous = next((i for i, e in enumerate(current)
                         if e.get('kind') == 'voice' and e.get('reference') == file_ref
                         and e.get('sha256') == sha), None)
        if previous is not None:
            existing = current[previous]
            if existing.get('stt_status') in ('manual', 'transcribed') and (
                    manual_transcript is None or manual_transcript == existing.get('transcript')):
                return task
        item: dict = {
            'kind': 'voice', 'reference': file_ref, 'sha256': sha,
            'size': meta['size'], 'suffix': p.suffix.lower(),
            'recorded_at': time.time(),
            'stt_status': None, 'transcript': None,
        }
        if manual_transcript is not None:
            if not isinstance(manual_transcript, str):
                raise IntakeError('Manual transcript must be a string')
            item['transcript'] = manual_transcript[:MAX_TRANSCRIPT_CHARS]
            item['stt_status'] = 'manual'
        else:
            stt = self.stt.transcribe(p)
            if stt.transcript is not None:
                item['transcript'] = stt.transcript
                item['stt_status'] = 'transcribed'
            else:
                item['stt_status'] = 'pending_transcription'
                if stt.error:
                    item['stt_error'] = stt.error
        controls.check('inspect', baseline)
        _validate_file(p, MAX_AUDIO_BYTES, SUPPORTED_AUDIO)
        if _sha256_path(p) != sha:
            raise IntakeError('Audio changed during transcription')
        if previous is None:
            current.append(item)
        else:
            current[previous] = item
        return self.store.patch_context(task_id, {'evidence': current}, revision)
