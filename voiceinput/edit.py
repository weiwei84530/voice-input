"""Voice edits on selected text: decide what the spoken words mean for the selection.

Commands are recognised without the LLM, by pinyin so ASR homophones still count (選字 is often heard as 選自):
刪除 deletes, 選字 opens the candidate menu, letters spelled one by one replace the selection (keeping its
capitalisation), 改成X replaces with X, sentences with an editing verb (翻譯成英文) go to the LLM, and anything else
replaces the selection as spoken. Saying the selected word again means "not this one": pick another candidate.
"""
import re
from dataclasses import dataclass

from .candidates import syllables
from .hotwords import worth_learning

DELETE, CANDIDATES, REPLACE, REPICK, INSTRUCT = "delete", "candidates", "replace", "repick", "instruct"

_DELETE = [syllables(w) for w in ("刪除", "刪掉", "刪除掉")]
_CANDIDATE = [syllables(w) for w in ("選字", "換字", "改字")]
_PUNCT = re.compile(r"[\s，。、！？；：,.!?;:]+")
_SPELLED_RAW = re.compile(r"^\s*[A-Za-z](?:[\s,，.。-]+[A-Za-z])+[\s,，.。]*$")
_TRAILING = re.compile(r"[。，,.！？!?；;]+$")
_CHANGE_TO = re.compile(r"^(?:請|幫我)?(?:把它|把這個|這個)?(?:改成|換成|改為|換為|變成|寫成)(.+)$")
_INSTRUCT = re.compile(r"翻譯|翻成|改寫|重寫|潤飾|縮短|簡化|精簡|加上|加個|補上|去掉|拿掉|移除|大寫|小寫|口語|正式|語氣")
_ORDINALS = "一二三四五六七八九"
_PICK = re.compile(rf"^(?:選)?第?([{_ORDINALS}兩1-9])(?:個|號)?$")


@dataclass
class Edit:
    action: str
    text: str = ""        # replacement (REPLACE), instruction (INSTRUCT)
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


def parse_pick(formatted: str) -> int:
    """Menu row (0-based) for 第二個 / 二 / 2 / 選三, or -1."""
    m = _PICK.match(_PUNCT.sub("", formatted))
    if not m:
        return -1
    ch = m.group(1)
    return (2 if ch == "兩" else int(ch) if ch.isdigit() else _ORDINALS.index(ch) + 1) - 1


def plan(selected: str, raw: str, formatted: str) -> Edit:
    """raw: ASR output (for detecting spelled letters); formatted: the text after format_text / hotwords."""
    spoken = _PUNCT.sub("", formatted)
    if not spoken:
        return Edit(REPICK)
    sounds = syllables(spoken)
    if sounds in _DELETE:
        return Edit(DELETE)
    if sounds in _CANDIDATE:
        return Edit(CANDIDATES)
    if _SPELLED_RAW.match(raw):
        word = re.sub(r"[^A-Za-z]", "", raw)
        return Edit(REPLACE, match_case(word, selected), spelled=True)
    if spoken == _PUNCT.sub("", selected):
        return Edit(REPICK)
    m = _CHANGE_TO.match(spoken)
    if m and worth_learning(selected, m.group(1), False):
        return Edit(REPLACE, m.group(1))       # 改成程式: the 2B model echoes the instruction instead
    if m or _INSTRUCT.search(spoken):
        return Edit(INSTRUCT, formatted.strip())
    text = formatted.strip()
    if not _TRAILING.search(selected):
        text = _TRAILING.sub("", text)
    return Edit(REPLACE, text)
