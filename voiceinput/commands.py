"""Voice commands that need no selection. They act on what VoiceInput just typed (session.Session.buffer) or
on the app (Enter, new line), so correcting a dictation needs neither the mouse nor the keyboard:

  復原 / 還原 / 撤銷          undo the last change (a dictation, an edit, a picked suggestion)
  不要記                      forget the hotword just learned
  送出                        press Enter; "…（停頓）送出" at the end of a dictation pastes it and sends
  換行                        Shift+Enter
  刪掉上一句 / 全部刪掉        remove the last dictation / everything dictated since the last click or key
  城市改成程式 / 不是城市是程式  replace a word in the dictated text (玉改成教育的育 and spelled letters work)
  刪掉那個 / 把那個刪掉        remove a word from the dictated text
  問號 / 逗號 …               type the symbol (replacing trailing punctuation of the dictated text)

Commands are recognised by fuzzy pinyin (ASR hears 復原 as 服原). Text-targeting commands only apply when the
target is found in the dictated text; otherwise the utterance is dictated as usual, so "把這個函式改成 async"
still reaches Claude Code as a request.
"""
import re
from dataclasses import dataclass

from .candidates import syllables
from .edit import _SPELLED_RAW, _SYMBOLS

UNDO, FORGET, SEND, NEWLINE, DELETE_LAST, DELETE_ALL = "undo", "forget", "send", "newline", "delete_last", "delete_all"
REPLACE, DELETE_TEXT, SYMBOL, DICTATE = "replace", "delete_text", "symbol", "dictate"

_STRIP = re.compile(r"[，。、！？；：,.!?;:「」\"']+")
_SPACES = re.compile(r"\s+")


def _phrases(*words):
    return {tuple(syllables(w)) for w in words}


_WHOLE = {
    UNDO: _phrases("復原", "還原", "撤銷", "撤回", "回復", "上一步", "復原一下", "取消上一步"),
    FORGET: _phrases("不要記", "不要記住", "別記", "不用記", "不要記這個", "不要學", "忘掉", "忘掉這個"),
    SEND: _phrases("送出", "發送", "傳送", "送出去"),
    NEWLINE: _phrases("換行", "下一行", "斷行", "新的一行"),
    DELETE_LAST: _phrases("刪掉上一句", "刪除上一句", "上一句刪掉", "上一句刪除", "刪掉剛剛那句", "剛剛那句刪掉",
                          "刪掉剛剛那句話", "刪掉剛才那句", "剛才那句刪掉", "上一句不要", "剛剛那句不要"),
    DELETE_ALL: _phrases("全部刪掉", "全部刪除", "整段刪掉", "全部清掉", "清空", "全部清空"),
}
_CHANGE = re.compile(r"^(?:把|將)?\s*(.+?)\s*(?:改成|換成|改為|換為|寫成|變成)\s*(.+?)$")
_NOT_BUT = re.compile(r"^不是\s*(.+?)\s*(?:而)?是\s*(.+?)$")
_DELETE_AFTER = re.compile(r"^(?:把|將)?\s*(.+?)\s*(?:刪掉|刪除|去掉|拿掉)$")
_DELETE_BEFORE = re.compile(r"^(?:刪掉|刪除|去掉|拿掉)\s*(.+?)$")
_SEND_TAIL = ("送出", "發送")
_SEND_RAW = re.compile(r"[，。,.!?！？、]\s*(?:送出|发送|發送)[。.！!]?$")
_SEND_PAUSE = 0.35   # seconds of silence before a trailing 送出 (TTS test: 0.64s with a pause, ~0.1s without)


@dataclass
class Command:
    kind: str
    text: str = ""        # DICTATE: text to paste; REPLACE: replacement; SYMBOL: the symbol
    target: str = ""      # REPLACE / DELETE_TEXT: the words to find in the dictated text
    send: bool = False    # DICTATE: press Enter afterwards
    spelled: bool = False # REPLACE: the replacement was spelled letter by letter


def parse(raw: str, formatted: str, transcript=None) -> Command:
    """raw: ASR output; formatted: after format_text and hotwords."""
    spoken = _SPACES.sub(" ", _STRIP.sub(" ", formatted)).strip()
    compact = spoken.replace(" ", "")
    if compact:
        sounds = tuple(syllables(compact))
        for kind, phrases in _WHOLE.items():
            if sounds in phrases:
                return Command(kind)
        if compact in _SYMBOLS:
            return Command(SYMBOL, _SYMBOLS[compact])
        if len(compact) <= 24:
            m = _CHANGE.match(spoken) or _NOT_BUT.match(spoken)
            if m and 0 < len(m.group(1)) <= 12:
                target, new = m.group(1).strip(), m.group(2).strip()
                spelled = bool(_SPELLED_RAW.match(_raw_tail(raw)))
                return Command(REPLACE, new, target, spelled=spelled)
            m = _DELETE_AFTER.match(spoken) or _DELETE_BEFORE.match(spoken)
            if m and 0 < len(m.group(1).strip()) <= 12:
                return Command(DELETE_TEXT, target=m.group(1).strip())
    return dictation(raw, formatted, transcript)


def dictation(raw: str, formatted: str, transcript=None) -> Command:
    """Plain dictation; a trailing 送出 said after a pause means "and press Enter"."""
    text = formatted.strip()
    body = _STRIP.sub("", text[-4:]) if text else ""
    for tail in _SEND_TAIL:
        if body.endswith(tail) and len(text.rstrip("。.！!")) > len(tail):
            paused = bool(_SEND_RAW.search(raw.strip())) or (
                transcript is not None and transcript.pause_before_tail(len(tail)) >= _SEND_PAUSE)
            if paused:
                head = text.rstrip("。.！! ")[:-len(tail)]
                return Command(DICTATE, head.rstrip("，,、。. "), send=True)
    return Command(DICTATE, text)


def _raw_tail(raw: str) -> str:
    """The part of the raw ASR text after 改成 (for spelled-letter detection)."""
    for key in ("改成", "换成", "改为", "换为", "写成", "变成", "是"):
        if key in raw:
            return raw.split(key, 1)[1]
    return raw


def locate(buffer: str, target: str, avoid: str = "") -> tuple[int, int] | None:
    """Span of target in buffer, last occurrence first: exact, then ignoring case, then same fuzzy pinyin
    (the ASR may write the spoken target as a homophone: 城市改成程式 heard as 程式改成程式). A pinyin match equal
    to avoid (the replacement) is skipped."""
    target = _STRIP.sub("", target).strip()
    if not target or not buffer:
        return None
    i = buffer.rfind(target)
    if i >= 0:
        return i, i + len(target)
    i = buffer.casefold().rfind(target.casefold())
    if i >= 0:
        return i, i + len(target)
    if re.search(r"[A-Za-z]", target):
        return None
    want, n = syllables(target), len(target)
    for i in range(len(buffer) - n, -1, -1):
        window = buffer[i:i + n]
        if window != avoid and re.fullmatch(r"[㐀-䶿一-鿿]+", window) and syllables(window) == want:
            return i, i + n
    return None
