"""Find words in a dictation that were probably misheard, so the app can offer a fix without the user having
to spot and select them.

A word is suspect when a second ASR model (SenseVoice) heard something different there and its version is the
likelier word: a dictionary word where the typed one is not (主機版 / 主機板), or at least _FREQ_RATIO times more
common. Both models often share an error, so this fires rarely; it never guesses from the dictionary alone.
Context hotwords that could not decide (hotwords.Ask) are offered through the same menu.
"""
import difflib
import re
from dataclasses import dataclass, field

from . import candidates

_CJK = re.compile(r"[㐀-䶿一-鿿]")
_KEEP = re.compile(r"[㐀-䶿一-鿿A-Za-z0-9]")
_FREQ_RATIO = 20


@dataclass
class Suspect:
    start: int             # span in the dictated text
    end: int
    word: str
    options: list[str]     # best first
    hotword: object = None                       # set when offered for a context hotword (hotwords.Ask)
    words: list = field(default_factory=list)    # that sentence's context words


def _normalized(text: str) -> tuple[str, list[int]]:
    """text without punctuation and spaces, casefolded, plus each kept character's index in text."""
    keep = [(i, c.casefold()) for i, c in enumerate(text) if _KEEP.match(c)]
    return "".join(c for _, c in keep), [i for i, _ in keep]


def disagreements(text: str, other: str) -> list[tuple[int, int, str]]:
    """Spans of text (start, end, other's version) where another transcript of the same audio differs in
    Chinese characters of the same count. Punctuation, spacing, case and English are ignored."""
    a, amap = _normalized(text)
    b, _ = _normalized(other)
    out = []
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or i2 - i1 > 4 or i2 - i1 != j2 - j1:
            continue
        old, new = a[i1:i2], b[j1:j2]
        if not (all(_CJK.match(c) for c in old) and all(_CJK.match(c) for c in new)):
            continue
        out.append((amap[i1], amap[i2 - 1] + 1, new))
    return out


def _word_around(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a span to the dictionary word (2-4 characters) that covers it: 城 -> 城市."""
    for size in (2, 3, 4):
        for s in range(max(0, end - size), min(start, len(text) - size) + 1):
            w = text[s:s + size]
            if re.fullmatch(r"[㐀-䶿一-鿿]+", w) and candidates.is_word(w):
                return s, s + size
    return start, end


def likelier(alt: str, word: str) -> bool:
    """alt is a dictionary word and word is not, or alt is much more common."""
    fa, fw = candidates.frequency(alt), candidates.frequency(word)
    return fa > 0 and (fw == 0 or fa >= fw * _FREQ_RATIO)


def find(text: str, other: str | None) -> list[Suspect]:
    out = []
    for s, e, alt in disagreements(text, other or ""):
        s2, e2 = _word_around(text, s, e)
        word, alt_word = text[s2:e2], text[s2:s] + alt + text[e:e2]
        if not likelier(alt_word, word):
            continue
        options = [alt_word] + [h for h in candidates.homophones(word) if h != alt_word][:2]
        out.append(Suspect(s2, e2, word, options))
    return out
