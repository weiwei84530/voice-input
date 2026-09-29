"""User hotwords: corrections learned from voice edits (城市 -> 程式, Cloud -> Claude), applied after formatting.

Each hotword maps a wrong key to the right value. Mode "always" replaces blindly. Mode "context" (the default)
decides by the words around the key: each hotword remembers the words seen near it when the correction was made or
confirmed (修, bug) and when a replacement was declined (台北, 漂亮). A sentence sharing a declined word keeps the key,
one sharing a confirmed word gets the value, and anything else is left as spoken and offered in the suggestion menu
("可能是程式？"), whose answer adds this sentence's words to one list or the other. So 台北這個城市 is asked about once
and then left alone.
"""
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field

from .candidates import syllables
from .paths import HOTWORDS_PATH

log = logging.getLogger("voiceinput")

ALWAYS, CONTEXT = "always", "context"
_LATIN = re.compile(r"^[A-Za-z][A-Za-z0-9 .'+#-]*$")
_CJK = re.compile(r"^[㐀-䶿一-鿿]+$")
_WINDOW = 6            # characters on each side of the key that count as its context
_MAX_CONTEXT = 40      # words kept per list, newest last
_STOP = set("的了是在我你他她它們這那個一有就也都要會把給和與及或嗎呢吧啊很還再又而跟被讓")
_STOP_WORDS = set("這個 那個 一個 我們 你們 他們 就是 然後 可以 什麼 現在 還是 因為 所以 如果 但是 一下".split())
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*|[㐀-䶿一-鿿]+")


def context_words(text: str, start: int, end: int) -> list[str]:
    """Content words within _WINDOW characters of text[start:end]: dictionary words (greedy longest match),
    other single characters and English words, minus function words."""
    from .candidates import pos_tag
    out = []
    for chunk in (text[max(0, start - _WINDOW):start], text[end:end + _WINDOW]):
        for m in _TOKEN.finditer(chunk):
            run = m.group(0)
            if run[0].isascii():
                out.append(run.casefold())
                continue
            i = 0
            while i < len(run):
                for n in (4, 3, 2, 1):
                    w = run[i:i + n]
                    if len(w) == n and (n == 1 or pos_tag(w)):
                        out.append(w)
                        i += n
                        break
    return [w for w in dict.fromkeys(out) if w not in _STOP and w not in _STOP_WORDS]


@dataclass
class Hotword:
    key: str
    value: str
    mode: str = CONTEXT
    hits: int = 0
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M"))
    contexts: list = field(default_factory=list)    # words near the key where the value was right
    negatives: list = field(default_factory=list)   # words near the key where it was wrong

    def remember(self, words: list[str], right: bool):
        """Add a sentence's context words to the confirmed or the declined list."""
        mine, other = (self.contexts, self.negatives) if right else (self.negatives, self.contexts)
        for w in words:
            if w in other:
                other.remove(w)
            if w not in mine:
                mine.append(w)
        del mine[:-_MAX_CONTEXT]


@dataclass
class Ask:
    """A context hotword whose sentence gave no verdict: offer the value instead of replacing."""
    start: int            # span of the key in the returned text
    end: int
    hotword: Hotword
    words: list           # the sentence's context words, remembered once the user answers


class HotwordStore:
    def __init__(self, path=HOTWORDS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.items: list[Hotword] = []
        self._pattern = None
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            fields = Hotword.__dataclass_fields__
            self.items = [Hotword(**{k: v for k, v in d.items() if k in fields}) for d in data]
        except (OSError, ValueError, TypeError):
            self.items = []
        self._pattern = None

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps([asdict(h) for h in self.items], ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            self._pattern = None

    def find(self, key: str) -> Hotword | None:
        k = key.casefold()
        return next((h for h in self.items if h.key.casefold() == k), None)

    def add(self, key: str, value: str, mode: str = CONTEXT, context: list[str] | None = None) -> Hotword:
        """Add or update the hotword for key, remembering the words around the correction; returns it."""
        h = self.find(key)
        if h:
            h.value = value
        else:
            h = Hotword(key, value, mode)
            self.items.append(h)
        if context:
            h.remember(context, True)
        self.save()
        return h

    def bias_words(self) -> list[tuple[str, float]]:
        """(word, score) pairs for ASR hotword biasing: the corrected values. Latin terms get a higher score
        (cloud code -> Claude Code needed 3.0 in testing)."""
        return [(h.value, 3.0 if _LATIN.match(h.value) else 2.0) for h in self.items if h.value]

    def remove(self, h: Hotword):
        if h in self.items:
            self.items.remove(h)
            self.save()

    def _compiled(self):
        if self._pattern is None:
            keys = sorted({h.key for h in self.items if h.key}, key=len, reverse=True)
            parts = [rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])" if _LATIN.match(k) else re.escape(k) for k in keys]
            self._pattern = re.compile("|".join(parts), re.I) if parts else False
        return self._pattern

    def apply(self, text: str) -> tuple[str, list[Ask]]:
        """Replace hotword keys in one pass (longest key first, no chained replacements). Returns the text and the
        context hotwords left as spoken because their sentence gave no verdict (spans in the returned text)."""
        pattern = self._compiled()
        if not pattern or not text:
            return text, []
        out, pos, hit, asks, shift = [], 0, False, [], 0
        for m in pattern.finditer(text):
            h = self.find(m.group(0))
            if h is None or h.value == m.group(0):
                continue
            if h.mode != ALWAYS:
                words = context_words(text, m.start(), m.end())
                verdict = ("no" if any(w in h.negatives for w in words)
                           else "yes" if any(w in h.contexts for w in words) else "ask")
                log.info("hotword %s -> %s near %s: %s", m.group(0), h.value, words, verdict)
                if verdict != "yes":
                    if verdict == "ask":
                        asks.append(Ask(m.start() + shift, m.end() + shift, h, words))
                    continue
            out.append(text[pos:m.start()])
            out.append(h.value)
            shift += len(h.value) - (m.end() - m.start())
            pos = m.end()
            h.hits += 1
            hit = True
        if not hit:
            return text, asks
        out.append(text[pos:])
        self.save()
        return "".join(out), asks


# --- which edits are worth learning ---
def sounds_alike(a: str, b: str) -> bool:
    """Chinese words of 2+ characters whose syllables (fuzzy initials/finals) all match: 城市/程式 yes,
    明天見/後天見 no (it used to allow one different syllable in three and learned that content change).
    Words of 4+ characters may differ in one syllable. Single characters are never learned (四 -> 是 would
    rewrite every 四)."""
    if not (_CJK.match(a) and _CJK.match(b)) or len(a) != len(b) or len(a) < 2 or a == b:
        return False
    sa, sb = syllables(a), syllables(b)
    return sum(x != y for x, y in zip(sa, sb)) <= (1 if len(sa) >= 4 else 0)


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def spelled_alike(a: str, b: str) -> bool:
    """Latin words that differ by at most half their letters (Cloud -> Claude)."""
    if not (_LATIN.match(a) and _LATIN.match(b)) or a.casefold() == b.casefold():
        return False
    return _edit_distance(a.casefold(), b.casefold()) * 2 <= max(len(a), len(b))


def worth_learning(old: str, new: str, spelled: bool) -> bool:
    old, new = old.strip(), new.strip()
    if not old or not new or old == new or len(old) > 20:
        return False
    return spelled or sounds_alike(old, new) or spelled_alike(old, new)
