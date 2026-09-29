"""Voice edits on selected text: decide what the spoken words mean for the selection.

A delete trigger (刪除) deletes the selection, a pick trigger (選字) or saying the selected word again opens the
candidate menu, anything else replaces the selection as spoken. The trigger words are user settings, matched by
toneless pinyin because ASR hears 選字 as 選自. Spelling letters, 教育的育, 改成X, 大寫 / 小寫 and punctuation
names were removed 2026-09-29: corrections are made by selecting with the mouse and picking.
"""
import re
from dataclasses import dataclass

from .candidates import syllables

DELETE, CANDIDATES, REPLACE = "delete", "candidates", "replace"
DELETE_WORDS = ["刪除", "刪掉", "刪除掉"]
PICK_WORDS = ["選字", "換字", "改字"]

_PUNCT = re.compile(r"[\s，。、！？；：,.!?;:]+")
_TRAILING = re.compile(r"[。，,.！？!?；;]+$")
_SPLIT = re.compile(r"[\s,，、;；]+")


@dataclass
class Edit:
    action: str
    text: str = ""        # replacement (REPLACE)


def split_words(text: str) -> list[str]:
    """Trigger words typed in the settings, separated by commas or spaces."""
    return [w for w in _SPLIT.split(text) if w]


def plan(selected: str, formatted: str, delete_words: list[str], pick_words: list[str]) -> Edit:
    """formatted: the spoken text after format_text / hotwords."""
    spoken = _PUNCT.sub("", formatted)
    if not spoken or spoken == _PUNCT.sub("", selected):
        return Edit(CANDIDATES)          # the same word again: "not this one"
    sounds = syllables(spoken)
    if any(sounds == syllables(w) for w in delete_words):
        return Edit(DELETE)
    if any(sounds == syllables(w) for w in pick_words):
        return Edit(CANDIDATES)
    text = formatted.strip()
    if not _TRAILING.search(selected):
        text = _TRAILING.sub("", text)
    return Edit(REPLACE, text)
