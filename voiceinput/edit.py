"""Voice edits on selected text: decide what the spoken words mean for the selection.

Commands are matched by fixed words, no LLM: 刪除 deletes, 選字 opens the candidate menu, letters spelled one
by one replace the selection (keeping its capitalisation), anything else replaces the selection as spoken.
"""
import re
from dataclasses import dataclass

DELETE, CANDIDATES, REPLACE, UNCHANGED = "delete", "candidates", "replace", "unchanged"

_DELETE_WORDS = {"刪除", "刪掉", "刪除掉", "刪了", "刪"}
_CANDIDATE_WORDS = {"選字"}
_PUNCT = re.compile(r"[\s，。、！？；：,.!?;:]+")
_SPELLED_RAW = re.compile(r"^\s*[A-Za-z](?:[\s,，.。-]+[A-Za-z])+[\s,，.。]*$")
_TRAILING = re.compile(r"[。，,.！？!?；;]+$")


@dataclass
class Edit:
    action: str
    text: str = ""        # replacement text for REPLACE
    spelled: bool = False


def match_case(word: str, like: str) -> str:
    """Give a spelled word the capitalisation of the word it replaces (Cloud + CLAUDE -> Claude)."""
    letters = [c for c in like if c.isascii() and c.isalpha()]
    if not letters:
        return word
    if all(c.isupper() for c in letters) and len(letters) > 1:
        return word.upper()
    if letters[0].isupper():
        return word[:1].upper() + word[1:].lower()
    return word.lower()


def plan(selected: str, raw: str, formatted: str) -> Edit:
    """raw: ASR output (for detecting spelled letters); formatted: the text after format_text / hotwords."""
    spoken = _PUNCT.sub("", formatted)
    if not spoken:
        return Edit(UNCHANGED)
    if spoken in _DELETE_WORDS:
        return Edit(DELETE)
    if spoken in _CANDIDATE_WORDS:
        return Edit(CANDIDATES)
    if _SPELLED_RAW.match(raw):
        word = re.sub(r"[^A-Za-z]", "", raw)
        return Edit(REPLACE, match_case(word, selected), spelled=True)
    text = formatted.strip()
    if not _TRAILING.search(selected):
        text = _TRAILING.sub("", text)
    if text == selected.strip():
        return Edit(UNCHANGED, text)
    return Edit(REPLACE, text)
