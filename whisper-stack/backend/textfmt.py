"""Light, rule-based cleanup of Whisper output: sentence casing, "I", final period.

Whisper usually punctuates on its own, but small models on short clips often don't.
Two things help: a punctuated ``initial_prompt`` (Whisper imitates the prompt's
style) and the deterministic fixes below. Mid-sentence proper nouns and missing
commas are left to Whisper.
"""

import os
import re

# Whisper mimics the style of its prompt. Empty env value disables it.
STYLE_PROMPT = os.getenv(
    "WHISPER_STYLE_PROMPT",
    "Hello, welcome. This is a dictation, written with proper punctuation and capitalization.",
).strip()

SENTENCE_END = (".", "?", "!")

# Standalone "i" / "i'm" / "i've" ... but not "i.e." or a word containing i.
_I_RE = re.compile(r"(?<![\w.'’])i(?=['’]\w|[\s,;:!?)\"]|\.(?!\w)|$)")
# Sentence end (optionally followed by closing quotes/brackets), whitespace, lowercase letter.
_SENTENCE_START_RE = re.compile(r"([.?!][\"')\]]*\s+)([a-z])")


def _capitalize_first(text: str) -> str:
    for i, ch in enumerate(text):
        if ch.isalpha():
            return text[:i] + ch.upper() + text[i + 1:]
        if ch.isalnum():  # starts with a digit: nothing to capitalize
            return text
    return text


def ends_sentence(text: str) -> bool:
    return text.rstrip().rstrip("\"')]").endswith(SENTENCE_END)


def fix_word(text: str, capitalize: bool) -> str:
    """Fix one Whisper word (keeps its leading space). ``capitalize`` = starts a sentence."""
    text = _I_RE.sub("I", text)
    return _capitalize_first(text) if capitalize else text


def format_text(text: str) -> str:
    """Clean a complete transcript."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return text
    text = _I_RE.sub("I", text)
    text = _capitalize_first(text)
    text = _SENTENCE_START_RE.sub(lambda m: m.group(1) + m.group(2).upper(), text)
    if text[-1].isalnum():
        text += "."
    return text
