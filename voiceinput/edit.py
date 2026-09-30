"""What an utterance means for selected text, and the two special utterances.

Speaking with text selected replaces it (as typing would). There are no spoken commands on a selection any more
(2026-09-30): the hotkey does it: one tap opens the candidate menu, a double tap deletes, a double tap held deletes
and dictates in its place. When the new text sounds like what it replaced (城市 → 程式, or 城市 heard again), the
app pastes it and opens the candidate menu for it (similar()).

A bare punctuation name (逗號, 句號, 驚嘆號 …) is typed as the mark itself; only when the whole utterance is exactly
the name, so 逗號很重要 is dictated as spoken.
"""
import re

from .candidates import syllables
from .hotwords import worth_learning

SYMBOLS = {"句號": "。", "句點": "。", "逗號": "，", "問號": "？", "驚嘆號": "！", "感嘆號": "！", "驚歎號": "！",
           "感歎號": "！", "頓號": "、", "分號": "；", "冒號": "：", "點點點": "……", "刪節號": "……", "省略號": "……"}

_PUNCT = re.compile(r"[\s，。、！？；：,.!?;:…]+")
_TRAILING = re.compile(r"[。，,.！？!?；;]+$")
_CJK_CHAR = re.compile(r"^[㐀-䶿一-鿿]$")


def symbol(formatted: str) -> str | None:
    """The punctuation mark when the whole utterance is its name (逗號 / 逗號。), else None."""
    return SYMBOLS.get(_PUNCT.sub("", formatted))


def replacement(selected: str, formatted: str) -> str:
    """The text that replaces a selection: sentence-final punctuation only if the selection had some."""
    text = formatted.strip()
    if not _TRAILING.search(selected):
        text = _TRAILING.sub("", text)
    return text


def similar(old: str, new: str) -> bool:
    """new sounds (or is spelled) like old: the user was correcting a misrecognition, not changing the text.
    The same word again counts (the recognizer heard 城市 once more), as does one character (雨 / 育)."""
    old, new = _PUNCT.sub("", old), _PUNCT.sub("", new)
    if not old or not new:
        return False
    if old.casefold() == new.casefold():
        return True
    if _CJK_CHAR.match(old) and _CJK_CHAR.match(new):
        return syllables(old) == syllables(new)
    return worth_learning(old, new, False)
