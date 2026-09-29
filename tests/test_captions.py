from app.services.video.captions import TimedWord, build_cues, to_vtt, words_from_alignment, words_from_text


def _alignment(text: str, offset: float) -> list[TimedWord]:
    """`words_from_alignment` of a text whose characters last 0.1 s each."""
    positions = range(len(text))
    return words_from_alignment(list(text), [i * 0.1 for i in positions], [(i + 1) * 0.1 for i in positions], offset)


def _rounded(words: list[TimedWord]) -> list[tuple[str, float, float]]:
    return [(word.text, round(word.start, 2), round(word.end, 2)) for word in words]


def test_alignment_is_grouped_into_words_and_shifted_to_the_scene():
    assert _rounded(_alignment("Hola mundo", 5.0)) == [("Hola", 5.0, 5.4), ("mundo", 5.5, 6.0)]


def test_alignment_ignores_leading_repeated_and_trailing_spaces():
    assert _rounded(_alignment("  Hola   mundo ", 0)) == [("Hola", 0.2, 0.6), ("mundo", 0.9, 1.4)]
    assert words_from_alignment([], [], [], 0) == []


def test_cues_have_at_most_two_short_lines_and_never_overlap():
    sentence = "Esta es una frase bastante larga que necesita varias líneas para caber completa en pantalla. Otra frase."
    cues = build_cues(words_from_text(sentence, 0, 12))
    for start, end, text in cues:
        lines = text.split("\n")
        assert len(lines) <= 2 and all(len(line) <= 42 for line in lines) and end > start
    for (_, end, _), (next_start, _, _) in zip(cues, cues[1:]):
        assert end <= next_start


def test_sentences_end_their_cue():
    words = [TimedWord("Hola.", 0, 0.5), TimedWord("Adiós.", 1, 1.5)]
    assert [text for _, _, text in build_cues(words)] == ["Hola.", "Adiós."]


def test_vtt_format_and_minimum_cue_length():
    vtt = to_vtt([TimedWord("Hola", 61.25, 61.5)])
    # Short words still stay on screen long enough to read (MIN_CUE_SECONDS).
    assert vtt.startswith("WEBVTT\n\n1\n00:01:01.250 --> 00:01:02.050\nHola")
