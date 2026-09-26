"""User hotwords: corrections learned from voice edits (城市 -> 程式, Cloud -> Claude), applied after formatting.

Each hotword maps a wrong key to the right value. Mode "always" replaces blindly; mode "context" (the default)
only replaces when a judge says the value fits the sentence better, so 台北這個城市 is not turned into 程式.
Without a judge (LLM not running) context hotwords are left alone.
"""
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field

from pypinyin import Style, lazy_pinyin

from .paths import HOTWORDS_PATH

log = logging.getLogger("voiceinput")

ALWAYS, CONTEXT = "always", "context"
_LATIN = re.compile(r"^[A-Za-z][A-Za-z0-9 .'+#-]*$")
_CJK = re.compile(r"^[㐀-䶿一-鿿]+$")


@dataclass
class Hotword:
    key: str
    value: str
    mode: str = CONTEXT
    hits: int = 0
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M"))


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

    def add(self, key: str, value: str, mode: str = CONTEXT) -> Hotword:
        """Add or update the hotword for key; returns it."""
        h = self.find(key)
        if h:
            h.value = value
        else:
            h = Hotword(key, value, mode)
            self.items.append(h)
        self.save()
        return h

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

    def apply(self, text: str, judge=None) -> str:
        """Replace hotword keys in one pass (longest key first, no chained replacements).
        judge(text, start, end, value) -> bool decides context-mode hotwords; None = leave them alone."""
        pattern = self._compiled()
        if not pattern or not text:
            return text
        out, pos, hit = [], 0, False
        for m in pattern.finditer(text):
            h = self.find(m.group(0))
            if h is None or h.value == m.group(0):
                continue
            if h.mode != ALWAYS:
                try:
                    ok = judge is not None and judge(text, m.start(), m.end(), h.value)
                except Exception:
                    log.exception("hotword judge failed")
                    ok = False
                if not ok:
                    continue
            out.append(text[pos:m.start()])
            out.append(h.value)
            pos = m.end()
            h.hits += 1
            hit = True
        if not hit:
            return text
        out.append(text[pos:])
        self.save()
        return "".join(out)


# --- which edits are worth learning ---
_FUZZY = [("zh", "z"), ("ch", "c"), ("sh", "s"), ("ing", "in"), ("eng", "en"), ("ang", "an")]


def _syllables(s: str) -> list[str]:
    out = []
    for p in lazy_pinyin(s, style=Style.NORMAL):
        for a, b in _FUZZY:
            p = p.replace(a, b)
        if p.startswith("l"):
            p = "n" + p[1:]
        out.append(p)
    return out


def sounds_alike(a: str, b: str) -> bool:
    """Chinese words of 2+ characters whose syllables (fuzzy initials/finals) almost all match: 城市/程式 yes,
    明天/後天 no. Single characters are never learned (四 -> 是 would rewrite every 四)."""
    if not (_CJK.match(a) and _CJK.match(b)) or len(a) != len(b) or len(a) < 2 or a == b:
        return False
    sa, sb = _syllables(a), _syllables(b)
    return sum(x == y for x, y in zip(sa, sb)) >= len(sa) - len(sa) // 3


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
