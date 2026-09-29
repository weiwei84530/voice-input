"""Find words in a dictation that were probably misheard, so the app can offer a fix without the user having
to spot and select them.

A word is suspect when a second ASR model (SenseVoice) heard something different there and the LLM, asked to
choose between the two for this sentence, gives the second model's version a fair share. Asking the LLM alone is useless:
scanning 80 of the user's real utterances for homophones it prefers flagged only correct words (就是 -> 就勢 0.79,
想要 -> 先要 0.93; tested 2026-09-29). X-ASR token confidences were tried as a second signal and dropped for the
same reason: without the second model's version the LLM has to pick among dictionary homophones.
"""
import difflib
import logging
import re
from dataclasses import dataclass

from . import candidates

log = logging.getLogger("voiceinput")

_CJK = re.compile(r"[㐀-䶿一-鿿]")
_KEEP = re.compile(r"[㐀-䶿一-鿿A-Za-z0-9]")
# Offer the second model's version when the LLM gives it at least this share. A wrong offer is cheap (a small
# menu that closes by itself or on the next utterance); 2B leans to mainland wording (函數 0.71 over 函式).
_OFFER_P = 0.25


@dataclass
class Suspect:
    start: int             # span in the dictated text
    end: int
    word: str
    options: list[str]     # best first


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
            if re.fullmatch(r"[㐀-䶿一-鿿]+", w) and candidates.pos_tag(w):
                return s, s + size
    return start, end


def find(text: str, other: str | None, rank) -> list[Suspect]:
    """rank(before, after, options) -> [(p, option)] best first, or None when the LLM is not running."""
    out = []
    for s, e, alt in disagreements(text, other or ""):
        s2, e2 = _word_around(text, s, e)
        word, alt_word = text[s2:e2], text[s2:s] + alt + text[e:e2]
        if rank is None:
            continue
        duel = rank(text[:s2], text[e2:], [word, alt_word])
        log.info("suspect %s|%s: %s", word, alt_word, [(c, round(p, 2)) for p, c in duel])
        if dict((c, p) for p, c in duel).get(alt_word, 0) < _OFFER_P:
            continue
        options = [alt_word] + [h for h in candidates.homophones(word) if h != alt_word][:2]
        out.append(Suspect(s2, e2, word, options))
    return out
