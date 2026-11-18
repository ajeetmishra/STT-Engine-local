"""Incremental ("live") transcription on top of faster-whisper.

Whisper is not a streaming model, so live dictation is approximated the same way
whisper_streaming / WhisperLive do it:

1. Audio arrives continuously and is appended to a rolling buffer.
2. Roughly every ``step`` seconds the whole buffer is re-transcribed with word
   timestamps.
3. LocalAgreement-2: a word is *committed* (final) once two consecutive passes
   agree on it. Words after the agreed prefix are reported as a tentative
   *partial* hypothesis that may still change.
4. Audio before the last committed word is trimmed off so the buffer (and thus
   the per-pass cost) stays bounded.
"""

import difflib
import logging
import re
import threading
from dataclasses import dataclass

import numpy as np
from faster_whisper import WhisperModel

from textfmt import STYLE_PROMPT, ends_sentence, fix_word

SAMPLE_RATE = 16000

# Max words of committed text fed back as `initial_prompt` for context.
PROMPT_WORDS = 40
# Seconds of trailing audio kept when the buffer is trimmed with nothing pending.
SILENCE_TAIL_SECONDS = 1.0
# Window at the end of the buffer in which zero-length words are treated as hallucinations.
TAIL_HALLUCINATION_SECONDS = 2.0

logger = logging.getLogger("whisper-backend.live")


@dataclass
class Word:
    start: float  # absolute seconds since session start
    end: float
    text: str  # raw Whisper word incl. leading space (" off", "-site"), so "".join() spaces correctly


def _norm(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())


def _join(words: list[Word]) -> str:
    return "".join(w.text for w in words)


def _drop_tail_repeat(words: list[Word]) -> list[Word]:
    """Remove an immediately repeated phrase (2+ words) at the end of a pass, a common
    Whisper hallucination where the audio stops: "... thank you. Thank you."""
    for n in range(len(words) // 2, 1, -1):
        if [_norm(w.text) for w in words[-n:]] == [_norm(w.text) for w in words[-2 * n:-n]]:
            return words[:-n]
    return words


def pcm16_to_float32(data: bytes) -> np.ndarray:
    """Little-endian signed 16-bit mono PCM -> float32 in [-1, 1]."""
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


class LiveTranscriber:
    """Per-connection streaming state. ``add_audio`` is called from the event loop while
    ``process``/``finish`` run in a worker thread, so buffer mutations take a lock."""

    def __init__(
        self,
        model: WhisperModel,
        *,
        language: str | None,
        beam_size: int,
        max_buffer_seconds: float,
    ) -> None:
        self.model = model
        self.language = language
        self.beam_size = beam_size
        self.max_buffer_seconds = max_buffer_seconds

        self._lock = threading.Lock()
        self._audio = np.zeros(0, dtype=np.float32)
        self._offset = 0.0  # absolute time of self._audio[0]
        self._unprocessed = 0  # samples received since the last pass started
        self._total_samples = 0

        self.committed: list[Word] = []
        self._hypothesis: list[Word] = []  # tentative words from the previous pass

    @property
    def total_seconds(self) -> float:
        return self._total_samples / SAMPLE_RATE

    @property
    def pending_seconds(self) -> float:
        return self._unprocessed / SAMPLE_RATE

    @property
    def text(self) -> str:
        return _join(self.committed).strip()

    @property
    def _committed_end(self) -> float:
        return self.committed[-1].end if self.committed else 0.0

    def add_audio(self, samples: np.ndarray) -> None:
        with self._lock:
            self._audio = np.concatenate([self._audio, samples])
            self._unprocessed += len(samples)
            self._total_samples += len(samples)

    def _transcribe(self, audio: np.ndarray, offset: float) -> list[Word]:
        # Prompt with committed text that precedes the buffer (not the part re-transcribed
        # inside it), so Whisper keeps context without being nudged to skip words.
        # The style prompt goes first so Whisper imitates its punctuation and casing.
        context = [w for w in self.committed if w.end <= offset][-PROMPT_WORDS:]
        prompt = " ".join(p for p in (STYLE_PROMPT, _join(context).strip()) if p)
        segments, _ = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            temperature=0.0,  # no fallback re-decoding: keeps passes fast and predictable
            word_timestamps=True,
            vad_filter=True,  # skip silence; avoids hallucinated text on quiet buffers
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )
        buffer_end = offset + len(audio) / SAMPLE_RATE
        words: list[Word] = []
        for segment in segments:
            for w in segment.words or []:
                start, end = offset + float(w.start), offset + float(w.end)
                # Zero-length words squeezed against the end of the buffer are Whisper
                # hallucinating past where the audio stops ("... Thank you. Thank you.").
                if end - start < 0.01 and start > buffer_end - TAIL_HALLUCINATION_SECONDS:
                    return _drop_tail_repeat(words)
                if w.word.strip():
                    words.append(Word(start, end, w.word))
        return _drop_tail_repeat(words)

    def _new_words(self, words: list[Word], offset: float) -> list[Word]:
        """Drop the words of a pass that re-transcribe already committed audio.

        The buffer starts inside committed audio, so a pass begins by repeating the
        committed words that lie in the buffer. They are located by text alignment
        (timestamps jitter between passes by a few hundred ms, which drops or
        duplicates boundary words), falling back to timestamps when alignment fails.
        """
        overlap = [w for w in self.committed if w.end > offset]
        logger.debug(
            "pass @%.2f: committed-in-buffer=%r words=%r",
            offset, _join(overlap), [(round(float(w.start), 2), w.text) for w in words],
        )
        if not overlap or not words:
            return words

        a = [_norm(w.text) for w in overlap]
        b = [_norm(w.text) for w in words]
        blocks = [
            m for m in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks()
            if m.size
        ]
        edge = self._committed_end
        if blocks:
            last = blocks[-1]
            missing = len(a) - (last.a + last.size)  # committed words after the match
            # Accept only a match that reaches (nearly) to the committed tail and sits near
            # the head of the pass, not a stray "the" further on.
            if missing <= 2 and last.b <= last.a + 3:
                cut = last.b + last.size
                # Unmatched committed tail words were either re-worded or dropped by this
                # pass; skip a following word only if it lies inside committed audio.
                while missing and cut < len(words) and (words[cut].start + words[cut].end) / 2 <= edge:
                    cut += 1
                    missing -= 1
                return words[cut:]

        return [w for w in words if (w.start + w.end) / 2 > edge]

    def _cased(self, words: list[Word]) -> list[Word]:
        """Copies of ``words`` with sentence casing, continuing from the committed text."""
        prev = self.committed[-1].text if self.committed else None
        out: list[Word] = []
        for w in words:
            text = fix_word(w.text, prev is None or ends_sentence(prev))
            out.append(Word(w.start, w.end, text))
            prev = text
        return out

    def _commit(self, words: list[Word]) -> list[Word]:
        cased = self._cased(words)
        self.committed.extend(cased)
        return cased

    def _trim(self) -> list[Word]:
        """Bound the buffer. Returns words force-committed to make room (rare)."""
        with self._lock:
            duration = len(self._audio) / SAMPLE_RATE
            if duration <= self.max_buffer_seconds:
                return []
            end = self._offset + duration
            forced: list[Word] = []
            if self._hypothesis:
                cut = self._committed_end
                if cut <= self._offset and duration > 2 * self.max_buffer_seconds:
                    # Passes keep disagreeing; accept the latest hypothesis rather than grow forever.
                    forced = self._commit(self._hypothesis)
                    self._hypothesis = []
                    cut = forced[-1].end
            else:
                cut = max(self._committed_end, end - SILENCE_TAIL_SECONDS)
            if cut > self._offset:
                drop = int((cut - self._offset) * SAMPLE_RATE)
                self._audio = self._audio[drop:]
                self._offset += drop / SAMPLE_RATE
            return forced

    def process(self) -> tuple[str, str]:
        """Run one pass. Returns (newly committed text, current tentative text)."""
        with self._lock:
            audio, offset = self._audio, self._offset
            self._unprocessed = 0
        hypothesis = self._new_words(self._transcribe(audio, offset), offset)

        agreed: list[Word] = []
        for previous, current in zip(self._hypothesis, hypothesis):
            if _norm(previous.text) != _norm(current.text):
                break
            agreed.append(current)
        self._hypothesis = hypothesis[len(agreed):]
        committed = self._commit(agreed)

        committed.extend(self._trim())
        return _join(committed), _join(self._cased(self._hypothesis))

    def finish(self) -> str:
        """Final pass over whatever is left; commits everything. Returns the new text."""
        with self._lock:
            audio, offset = self._audio, self._offset
            self._audio = np.zeros(0, dtype=np.float32)
            self._unprocessed = 0
        if len(audio) < SAMPLE_RATE // 10:
            words = self._hypothesis
        else:
            words = self._new_words(self._transcribe(audio, offset), offset)
        delta = _join(self._commit(words))
        self._hypothesis = []
        # End the session on a full stop if Whisper left the last sentence open.
        if self.committed and self.committed[-1].text.rstrip()[-1:].isalnum():
            last = self.committed[-1]
            self.committed[-1] = Word(last.start, last.end, last.text.rstrip() + ".")
            delta += "."
        return delta
