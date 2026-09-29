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
_END_PUNCT = re.compile(r"[\s，。、！？；：,.!?;:…]+$")
_ANY_PUNCT = re.compile(r"[，。、！？；：,.!?;:…]")
_ORDINALS = "一二三四五六七八九"
_PICK = re.compile(rf"^(?:選)?第?([{_ORDINALS}兩1-9])(?:個|號)?$")
_YES = {"對", "對的", "是", "是的", "好", "好的", "沒錯", "確定", "要", "換", "改"}
_NO = {"不用", "不是", "不要", "不對", "取消", "關掉", "算了", "不用了", "不換"}
# Spelling out a character the way people do on the phone: 教育的育, 偉大的偉字, 弓長張 (2026-09-29: the user said
# 教育的育 on a selected 玉 and it was pasted literally)
_DESCRIBE = re.compile(r"^(?:是)?([㐀-䶿一-鿿]{1,4}?)的([㐀-䶿一-鿿])字?$")
_SURNAMES = {"弓長張": "張", "立早章": "章", "木子李": "李", "口天吳": "吳", "耳東陳": "陳", "草頭黃": "黃",
             "雙木林": "林", "古月胡": "胡", "言午許": "許", "雙口呂": "呂", "人可何": "何", "三橫王": "王",
             "文武斌": "斌", "雙人徐": "徐", "禾子季": "季", "木易楊": "楊", "女喬嬌": "嬌", "日月明": "明"}


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
    """Menu row (0-based) for 第二個 / 二 / 2 / 選三, 0 for 對 / 好, -2 for 不用 / 取消, else -1."""
    spoken = _PUNCT.sub("", formatted)
    if spoken in _YES:
        return 0
    if spoken in _NO:
        return -2
    m = _PICK.match(spoken)
    if not m:
        return -1
    ch = m.group(1)
    return (2 if ch == "兩" else int(ch) if ch.isdigit() else _ORDINALS.index(ch) + 1) - 1


def described_char(spoken: str) -> str | None:
    """教育的育 -> 育, 弓長張 -> 張. ASR often hears the last character as a homophone (教育的欲), so the
    character is taken from the describing word by sound."""
    spoken = _PUNCT.sub("", spoken)
    if spoken in _SURNAMES:
        return _SURNAMES[spoken]
    m = _DESCRIBE.match(spoken)
    if not m:
        return None
    word, ch = m.groups()
    if ch in word:
        return ch
    sound = syllables(ch)[0]
    same = [c for c in word if syllables(c)[0] == sound]
    return same[-1] if same else None


def replace_char(selected: str, ch: str) -> str:
    """Put the described character into the selection: a single character is replaced, in a longer selection
    the character that sounds like it (雨薇 + 教育的育 -> 育薇). Unchanged if none sounds alike."""
    text = selected.strip()
    if len(text) <= 1:
        return ch
    sound = syllables(ch)[0]
    for i, c in enumerate(text):
        if c != ch and syllables(c)[0] == sound:
            return text[:i] + ch + text[i + 1:]
    return selected


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
    m = _CHANGE_TO.search(spoken)
    ch = described_char(m.group(1) if m else spoken)
    if ch:
        new = replace_char(selected, ch)
        if new != selected:
            return Edit(REPLACE, new, spelled=True)   # spelled: an explicit correction, worth learning
    m = _SYMBOL_CHANGE.match(spoken)
    src, dst = (m.group(1), _SYMBOLS[m.group(2)]) if m else (None, _SYMBOLS.get(spoken))
    if src and _SYMBOLS[src] in selected:                          # 句號改成問號: 好。 -> 好？
        return Edit(REPLACE, selected.replace(_SYMBOLS[src], dst))
    if dst and not src:                                            # 逗號 / 改成逗號 (the LLM got these wrong)
        if _ONLY_PUNCT.match(selected) or selected.strip() in _SYMBOLS:   # 。/ 點點點 -> ，
            return Edit(REPLACE, dst)
        if _END_PUNCT.search(selected):                                    # 好。 -> 好，
            return Edit(REPLACE, _END_PUNCT.sub("", selected) + dst)
        if not _ANY_PUNCT.search(selected):                                # 好 -> 好，
            return Edit(REPLACE, selected.rstrip() + dst)
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
