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
_CHANGE_TO = re.compile(r"(?:改成|換成|改為|換為|變成|寫成)(.+)$")
# Words that make an utterance an editing instruction rather than new text (decided 2026-09-29: cue words, not
# an LLM classifier). Re-dictating a sentence that happens to contain one of them is sent to the LLM too.
_INSTRUCT = re.compile(r"改|換|變成|翻譯|翻成|加上|加個|加一|補上|去掉|拿掉|移除|刪|寫成|寫得|一點|這句|這段|這行|"
                       r"句號|逗號|問號|驚嘆號|頓號|引號|括號|標點|符號|大寫|小寫|語氣|禮貌|正式|口語|簡短|縮短|精簡|潤飾|通順")
# Punctuation by spoken name. 2B gets these wrong (。 + 改成逗號 -> "，。", 點點點 left as is), so they are done in code.
_SYMBOLS = {"句號": "。", "句點": "。", "逗號": "，", "問號": "？", "驚嘆號": "！", "感嘆號": "！", "驚歎號": "！",
            "頓號": "、", "分號": "；", "冒號": "：", "點點點": "……", "刪節號": "……", "省略號": "……"}
_SYM = "|".join(sorted(_SYMBOLS, key=len, reverse=True))
_SYMBOL_CHANGE = re.compile(rf"^(?:把)?(?:({_SYM}))?(?:都)?(?:改成|換成|變成|改為|換為)(?:標點符號的|標點的|符號的)?({_SYM})$")
_ONLY_PUNCT = re.compile(r"^[\s，。、！？；：,.!?;:…]+$")
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
    m = _SYMBOL_CHANGE.match(spoken)
    if m:
        src, dst = m.group(1), _SYMBOLS[m.group(2)]
        if src and _SYMBOLS[src] in selected:                      # 句號改成問號: 好。 -> 好？
            return Edit(REPLACE, selected.replace(_SYMBOLS[src], dst))
        if _ONLY_PUNCT.match(selected) or selected.strip() in _SYMBOLS:   # 。/ 點點點 + 改成逗號
            return Edit(REPLACE, dst)
    m = _CHANGE_TO.search(spoken)
    if m and worth_learning(selected, m.group(1), False):
        return Edit(REPLACE, m.group(1))       # 改成程式: the 2B model echoes the instruction instead
    if re.search(r"[A-Za-z]", selected) and ("大寫" in spoken) != ("小寫" in spoken):
        # 2B fails at this (returned the line unchanged); do it directly
        return Edit(REPLACE, selected.upper() if "大寫" in spoken else selected.lower())
    if _INSTRUCT.search(spoken):
        return Edit(INSTRUCT, formatted.strip())
    text = formatted.strip()
    if not _TRAILING.search(selected):
        text = _TRAILING.sub("", text)
    return Edit(REPLACE, text)
