"""WebVTT captions from word timings (TTS alignment or speech-to-text)."""

from dataclasses import dataclass

MAX_LINE_CHARS = 42
MAX_CUE_LINES = 2
MAX_CUE_SECONDS = 5.0
MIN_CUE_SECONDS = 0.8
MIN_SPAN_SECONDS = 0.1  # `words_from_text` never squeezes a scene's words into less time than this
SENTENCE_ENDINGS = (".", "?", "!")

Cue = tuple[float, float, str]  # start, end, text


@dataclass
class TimedWord:
    text: str
    start: float
    end: float


def words_from_alignment(characters: list[str], starts: list[float], ends: list[float], offset: float) -> list[TimedWord]:
    """Group a character-level alignment into words, shifted to the scene start in the video."""
    words: list[TimedWord] = []
    current, word_start, word_end = "", 0.0, 0.0
    # The trailing space closes the last word.
    for char, start, end in [*zip(characters, starts, ends), (" ", 0.0, 0.0)]:
        if not char.isspace():
            if not current:
                word_start = start
            current += char
            word_end = end
        elif current:
            words.append(TimedWord(current, offset + word_start, offset + word_end))
            current = ""
    return words


def words_from_text(text: str, start: float, end: float) -> list[TimedWord]:
    """Fallback when no alignment exists: spread words over the span proportionally to their length."""
    tokens = text.split()
    total_chars = sum(len(token) for token in tokens) or 1
    span = max(end - start, MIN_SPAN_SECONDS)
    cursor = start
    words = []
    for token in tokens:
        duration = span * len(token) / total_chars
        words.append(TimedWord(token, cursor, cursor + duration))
        cursor += duration
    return words


def _timestamp(seconds: float) -> str:
    milliseconds = round(max(seconds, 0) * 1000)
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def build_cues(words: list[TimedWord]) -> list[Cue]:
    """Cues of up to MAX_CUE_LINES lines of MAX_LINE_CHARS, broken at sentence ends when possible."""
    cues: list[Cue] = []
    lines: list[str] = [""]
    cue_start = None
    last_end = 0.0

    def flush():
        nonlocal lines, cue_start
        text = "\n".join(line for line in lines if line)
        if text and cue_start is not None:
            cues.append((cue_start, max(last_end, cue_start + MIN_CUE_SECONDS), text))
        lines, cue_start = [""], None

    for word in words:
        if cue_start is None:
            cue_start = word.start
        candidate = f"{lines[-1]} {word.text}".strip()
        if len(candidate) <= MAX_LINE_CHARS:
            lines[-1] = candidate
        elif len(lines) < MAX_CUE_LINES:
            lines.append(word.text)
        else:
            flush()
            cue_start = word.start
            lines = [word.text]
        last_end = word.end
        if word.text.endswith(SENTENCE_ENDINGS) or last_end - cue_start >= MAX_CUE_SECONDS:
            flush()
    flush()

    # Never overlap the next cue.
    for index in range(len(cues) - 1):
        start, end, text = cues[index]
        cues[index] = (start, min(end, cues[index + 1][0]), text)
    return cues


def to_vtt(words: list[TimedWord]) -> str:
    """The WebVTT file of the words: one numbered cue per `build_cues` entry."""
    blocks = ["WEBVTT", ""]
    for index, (start, end, text) in enumerate(build_cues(words), start=1):
        blocks += [str(index), f"{_timestamp(start)} --> {_timestamp(end)}", text, ""]
    return "\n".join(blocks)
