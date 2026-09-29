"""Same-sounding word candidates for a selected Chinese word (城市 -> 程式, 誠實, 乘勢 …).

Candidates come from jieba's word list (349k words with frequencies) indexed by fuzzy toneless pinyin, so
Taiwanese-accent confusions (zh/z, ch/c, sh/s, ing/in, eng/en, ang/an, l/n) still match. The index takes ~13s to
build, so it is cached in DATA_DIR/cache and built in the background at startup. Ranking by context is done by the
LLM (llm.LlmServer.rank); here words are only ordered by exact-pinyin match, then frequency.
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
_CACHE_VERSION = 1
_CJK = re.compile(r"^[㐀-䶿一-鿿]+$")
_FUZZY = [("zh", "z"), ("ch", "c"), ("sh", "s"), ("ing", "in"), ("eng", "en"), ("ang", "an")]
_MIN_FREQ = 3            # jieba's floor; rarer entries are mostly noise

_index: dict[str, list[tuple[str, int]]] | None = None
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


def _build() -> dict:
    index: dict[str, list[tuple[str, int]]] = {}
    with open(_dict_path(), encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            word, freq = parts[0], int(parts[1])
            if len(word) > 4 or freq < _MIN_FREQ or not _CJK.match(word):
                continue
            index.setdefault(" ".join(syllables(word)), []).append((word, freq))
    for words in index.values():
        words.sort(key=lambda wf: -wf[1])
    return index


def load() -> dict:
    """The pinyin index, built (and cached) on first use. Safe to call from any thread."""
    global _index
    with _lock:
        if _index is None:
            try:
                version, _index = pickle.loads(_CACHE.read_bytes())
                if version != _CACHE_VERSION:
                    _index = None
            except (OSError, ValueError, pickle.PickleError, EOFError):
                _index = None
            if _index is None:
                log.info("building pinyin index…")
                _index = _build()
                _CACHE.parent.mkdir(parents=True, exist_ok=True)
                _CACHE.write_bytes(pickle.dumps((_CACHE_VERSION, _index)))
                log.info("pinyin index: %d keys", len(_index))
        return _index


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
