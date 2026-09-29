"""What VoiceInput knows about the text right before the caret, so it can be edited by voice without selecting.

Every dictation is pasted at the caret. As long as the user has not pressed a key or clicked since, and the same
window is in front, the text before the caret ends with exactly what we pasted (the "buffer"). Edits to it
("城市改成程式", "刪掉上一句", "復原", a picked suggestion) are done by pressing Backspace up to the edit point
and pasting the new tail. This works in any app, terminals included (Claude Code), because it needs neither
UI Automation nor a selection. Any key press or mouse click from the user ends the buffer: the caret may have
moved.

Undo keeps the buffer before each change; "復原" rewrites the tail back to it.
"""
import difflib
import re
import sys
import threading
import time
from dataclasses import dataclass, field

_BACK, _DELETE = 0x08, 0x2E
# Keys that may be part of deleting text (Shift/Ctrl + arrows/Home/End to select, then Delete) without typing
_EDIT_KEYS = {_BACK, _DELETE, 0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x23, 0x24, 0x25, 0x26,
              0x27, 0x28}
REDICTATE_WINDOW = 45.0   # seconds between a deleted dictation and its re-dictation to count as a correction
_WORD = re.compile(r"[A-Za-z0-9]+(?:['.][A-Za-z0-9]+)*|[^\sA-Za-z0-9，。、！？；：,.!?;:「」…]")


def foreground() -> int:
    if sys.platform != "win32":
        return 0
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow()


@dataclass
class Utterance:
    start: int              # span of the pasted text in Session.buffer
    end: int
    text: str               # what was pasted
    transcript: object      # asr.Transcript of the primary model
    audio: object           # numpy audio, for the second-opinion model
    at: float = field(default_factory=time.monotonic)
    second: str | None = None   # second model's formatted text, filled in the background
    deleted: bool = False       # removed by Backspace/Delete or by voice, without typing anything else
    typed: bool = False         # the user typed other keys after it


@dataclass
class Step:
    before: str
    after: str
    spans: list             # utterance spans before the change, to restore on undo
    note: str
    hotword: object = None  # hotword learned by this change, forgotten on undo


class Session:
    def __init__(self):
        self._lock = threading.RLock()
        self.buffer = ""
        self.hwnd = 0
        self.dirty = True
        self.utterances: list[Utterance] = []
        self.steps: list[Step] = []
        self.last: Utterance | None = None     # most recent dictation, kept after the buffer ends (re-dictation)
        self.ignore_rects: list = []           # our own windows (menu): clicks there do not move the caret

    # --- user input (called from the hook threads) ---
    def on_key(self, vk: int):
        with self._lock:
            if self.last is not None:
                if vk in (_BACK, _DELETE) and not self.last.typed:
                    self.last.deleted = True
                elif vk not in _EDIT_KEYS:
                    self.last.typed = True
            self.dirty = True

    def on_click(self, x: int, y: int):
        for (l, t, r, b) in list(self.ignore_rects):
            if l <= x < r and t <= y < b:
                return
        self.dirty = True

    # --- state ---
    def clean(self) -> bool:
        """True if the text before the caret is known to end with self.buffer."""
        return not self.dirty and bool(self.buffer) and self.hwnd == foreground()

    def last_utterance(self) -> Utterance | None:
        with self._lock:
            return self.utterances[-1] if self.clean() and self.utterances else None

    def redictation_of(self) -> Utterance | None:
        """The previous dictation if it was just deleted (keys or voice) and not replaced by typing."""
        u = self.last
        if u and u.deleted and not u.typed and time.monotonic() - u.at < REDICTATE_WINDOW:
            return u
        return None

    # --- changes (the caller performs the keystrokes this returns) ---
    def dictated(self, text: str, transcript, audio, hwnd: int) -> Utterance:
        """Record a paste of text at the caret."""
        with self._lock:
            if self.dirty or hwnd != self.hwnd:
                self.buffer, self.utterances, self.steps = "", [], []
            before = self.buffer
            spans = self._spans()
            start = len(self.buffer)
            self.buffer += text
            u = Utterance(start, len(self.buffer), text, transcript, audio)
            self.utterances.append(u)
            self.steps.append(Step(before, self.buffer, spans, f"輸入「{text}」"))
            self.last = u
            self.hwnd, self.dirty = hwnd, False
            return u

    def replaced_selection(self, before: str, old: str, new: str, hwnd: int, note: str, hotword=None):
        """Record an edit of a selection: the caret now follows new, with before (rest of the line) ahead."""
        with self._lock:
            self.buffer, self.utterances = before + new, []
            self.steps = [Step(before + old, self.buffer, [], note, hotword)]
            self.hwnd, self.dirty = hwnd, False

    def sent(self):
        """Enter was pressed: the text left the input box."""
        with self._lock:
            self.buffer, self.utterances, self.steps = "", [], []
            self.dirty = True

    def plan_replace(self, a: int, b: int, new: str) -> tuple[int, str]:
        """Keystrokes to turn buffer[a:b] into new: (backspaces, text to paste)."""
        after = self.buffer[:a] + new + self.buffer[b:]
        return self.plan_to(after)

    def plan_to(self, after: str) -> tuple[int, str]:
        p = 0
        n = min(len(self.buffer), len(after))
        while p < n and self.buffer[p] == after[p]:
            p += 1
        return len(self.buffer) - p, after[p:]

    def apply_replace(self, a: int, b: int, new: str, note: str, hotword=None):
        with self._lock:
            before, spans = self.buffer, self._spans()
            self.buffer = before[:a] + new + before[b:]
            delta = len(new) - (b - a)
            keep = []
            for u in self.utterances:
                if u.end <= a:
                    keep.append(u)
                elif u.start >= b:
                    u.start += delta
                    u.end += delta
                    keep.append(u)
                elif a <= u.start and u.end <= b and not new:
                    u.deleted = True     # removed entirely (刪掉上一句)
                else:
                    u.end = max(u.start, u.end + delta)
                    u.text = self.buffer[u.start:u.end]
                    keep.append(u)
            self.utterances = keep
            self.steps.append(Step(before, self.buffer, spans, note, hotword))
            self.dirty = False

    def pop_undo(self) -> Step | None:
        with self._lock:
            if not self.clean() or not self.steps or self.steps[-1].after != self.buffer:
                return None
            return self.steps[-1]

    def undone(self, step: Step):
        with self._lock:
            self.steps.pop()
            self.buffer = step.before
            for u, (s, e, text) in step.spans:
                u.start, u.end, u.text, u.deleted = s, e, text, False
            self.utterances = [u for u, _ in step.spans]
            if self.last is not None and self.last not in self.utterances:
                self.last.deleted = True      # undoing a dictation counts as deleting it (re-dictation)
            self.dirty = False

    def _spans(self):
        return [(u, (u.start, u.end, u.text)) for u in self.utterances]


# --- learning from re-dictation ---
def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def _join(words: list[str]) -> str:
    out = ""
    for w in words:
        if out and out[-1].isascii() and out[-1].isalnum() and w[0].isascii() and w[0].isalnum():
            out += " "
        out += w
    return out


def redictation_pair(old: str, new: str) -> tuple[str, str] | None:
    """(wrong, right) if new repeats old with one word or phrase changed: 我想請你用 cloud code → … Claude Code
    gives (cloud, Claude). Case differences alone do not count as changes."""
    a, b = _words(old), _words(new)
    if not a or not b:
        return None
    sm = difflib.SequenceMatcher(a=[w.casefold() for w in a], b=[w.casefold() for w in b], autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    if len(ops) != 1 or ops[0][0] != "replace":
        return None
    if sm.ratio() < 0.5 and not (len(a) == 1 and len(b) == 1):
        return None
    _, i1, i2, j1, j2 = ops[0]
    return _join(a[i1:i2]), _join(b[j1:j2])
