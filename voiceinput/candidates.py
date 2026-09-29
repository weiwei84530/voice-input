"""Same-sounding word candidates for a selected Chinese word (城市 -> 程式, 誠實, 乘勢 …).

Candidates come from jieba's word list (349k words with frequencies) indexed by fuzzy toneless pinyin, so
Taiwanese-accent confusions (zh/z, ch/c, sh/s, ing/in, eng/en, ang/an, l/n) still match. The index takes ~13s to
build, so it is cached in DATA_DIR/cache and built in the background at startup. Words are ordered by exact-pinyin
match, then frequency; the app puts the second model's version and learned hotwords in front.
"""
import importlib.util
import logging
import pickle
import re
import threading
from pathlib import Path

from pypinyin import Style, lazy_pinyin

from .paths import DATA_DIR
from .textfmt import to_traditional

log = logging.getLogger("voiceinput")

_CACHE = DATA_DIR / "cache" / "pinyin_index.pkl"
_CACHE_VERSION = 3
_CJK = re.compile(r"^[㐀-䶿一-鿿]+$")
_FUZZY = [("zh", "z"), ("ch", "c"), ("sh", "s"), ("ing", "in"), ("eng", "en"), ("ang", "an")]
_MIN_FREQ = 3            # jieba's floor; rarer entries are mostly noise

_index: dict[str, list[tuple[str, int]]] | None = None
_words: dict[str, tuple[str, int]] = {}   # 2-4 character words (Simplified) -> (jieba POS tag, frequency)
_lock = threading.Lock()


def fuzzy(p: str) -> str:
    for a, b in _FUZZY:
        p = p.replace(a, b)
    return "n" + p[1:] if p.startswith("l") else p


def syllables(s: str) -> list[str]:
    """Fuzzy toneless pinyin per character."""
    return [fuzzy(p) for p in lazy_pinyin(s, style=Style.NORMAL)]


def _dict_path() -> Path:
    spec = importlib.util.find_spec("jieba")
    return Path(spec.submodule_search_locations[0]) / "dict.txt"


def _build() -> tuple[dict, dict]:
    index: dict[str, list[tuple[str, int]]] = {}
    words: dict[str, tuple[str, int]] = {}
    with open(_dict_path(), encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            word, freq = parts[0], int(parts[1])
            if len(word) > 4 or freq < _MIN_FREQ or not _CJK.match(word):
                continue
            index.setdefault(" ".join(syllables(word)), []).append((word, freq))
            if len(word) >= 2:
                words[word] = (parts[2] if len(parts) > 2 else "x", freq)
    for same in index.values():
        same.sort(key=lambda wf: -wf[1])
    return index, words


def load() -> dict:
    """The pinyin index, built (and cached) on first use. Safe to call from any thread."""
    global _index, _words
    with _lock:
        if _index is None:
            try:
                version, index, words = pickle.loads(_CACHE.read_bytes())
                if version == _CACHE_VERSION:
                    _index, _words = index, words
            except (OSError, ValueError, pickle.PickleError, EOFError):
                pass
            if _index is None:
                log.info("building pinyin index…")
                index, words = _build()
                _CACHE.parent.mkdir(parents=True, exist_ok=True)
                _CACHE.write_bytes(pickle.dumps((_CACHE_VERSION, index, words)))
                _words, _index = words, index
                log.info("pinyin index: %d keys", len(_index))
        return _index


def pos_tag(word: str) -> str | None:
    """jieba POS tag of a Traditional or Simplified word, "" if not a dictionary word, None while the
    dictionary is still loading (never blocks)."""
    if _index is None:
        return None
    from .textfmt import to_simplified
    return _words.get(to_simplified(word), ("", 0))[0]


def frequency(word: str) -> int:
    """jieba frequency of a 2-4 character word (0 if unknown or still loading)."""
    from .textfmt import to_simplified
    return _words.get(to_simplified(word), ("", 0))[1]


def preload() -> None:
    threading.Thread(target=load, daemon=True).start()


def homophones(word: str, limit: int = 8) -> list[str]:
    """Words that sound like word (Traditional Chinese), best first; word itself excluded."""
    word = word.strip()
    if not _CJK.match(word) or len(word) > 4:
        return []
    exact = lazy_pinyin(word, style=Style.NORMAL)
    found = load().get(" ".join(fuzzy(p) for p in exact), [])
    # Exact pinyin first (the accent-fuzzy matches are a fallback), then by frequency
    ranked = sorted(found, key=lambda wf: (lazy_pinyin(wf[0], style=Style.NORMAL) != exact, -wf[1]))
    out: list[str] = []
    for w, _ in ranked:
        t = to_traditional(w)
        if t != word and t not in out:
            out.append(t)
        if len(out) == limit:
            break
    return out
