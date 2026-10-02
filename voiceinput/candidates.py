"""Same-sounding word candidates for a selected Chinese word (城市 -> 程式, 誠實, 乘勢 …), and the word list
the rest of the app asks "is this a word / how common is it".

The words come from McBopomofo (小麥注音, MIT): ~146k Taiwanese phrases of 2-6 characters and the Big5 single
characters, each with its Bopomofo reading and a frequency counted in a Taiwanese corpus (dict/words.tsv.gz, built
by dev/build_dict.py). Every word is indexed by its toneless reading, made fuzzy for Taiwanese-accent confusions
(ㄓ/ㄗ, ㄔ/ㄘ, ㄕ/ㄙ, ㄥ/ㄣ, ㄤ/ㄢ, ㄌ/ㄋ), under both McBopomofo's reading and pypinyin's (垃圾 is listed as
ㄌㄜˋ ㄙㄜˋ but said ㄌㄚ ㄐㄧ). Candidates are ordered: same reading with tones, same reading without tones,
fuzzy reading; then frequency. The index is cached in DATA_DIR/cache and built in the background at startup.

Until 2026-10-02 this used jieba's dict.txt (349k mainland words, POS tags); see AGENTS.md.
"""
import gzip
import logging
import pickle
import re
import threading
from pathlib import Path

from pypinyin import Style, lazy_pinyin

from .paths import DATA_DIR

log = logging.getLogger("voiceinput")

_DICT = Path(__file__).resolve().parent / "dict" / "words.tsv.gz"
_CACHE = DATA_DIR / "cache" / "word_index.pkl"
_OLD_CACHE = DATA_DIR / "cache" / "pinyin_index.pkl"   # the jieba index, removed on first build
_CACHE_VERSION = 1
_CJK = re.compile(r"^[㐀-䶿一-鿿]+$")
_TONES = re.compile("[ˊˇˋ˙]")
_FUZZY = [("ㄓ", "ㄗ"), ("ㄔ", "ㄘ"), ("ㄕ", "ㄙ"), ("ㄤ", "ㄢ"), ("ㄌ", "ㄋ")]
_ENG = re.compile("(?<![ㄨㄩ])ㄥ")   # eng/en and ing/in, but not ong/un or iong/ün

_index: dict[str, list[tuple[str, str, int]]] | None = None   # fuzzy key -> [(word, toned reading, frequency)]
_words: dict[str, tuple[int, list[str]]] = {}                  # word -> (frequency, toned readings)
_lock = threading.Lock()


def fuzzy(p: str) -> str:
    """Pinyin fuzzy form, for comparing two texts by sound (edit.similar, hotwords, picks)."""
    for a, b in (("zh", "z"), ("ch", "c"), ("sh", "s"), ("ing", "in"), ("eng", "en"), ("ang", "an")):
        p = p.replace(a, b)
    return "n" + p[1:] if p.startswith("l") else p


def syllables(s: str) -> list[str]:
    """Fuzzy toneless pinyin per character."""
    return [fuzzy(p) for p in lazy_pinyin(s, style=Style.NORMAL)]


def _toned(syllable: str) -> str:
    """pypinyin puts the neutral-tone dot first (˙ㄉㄜ), McBopomofo last (ㄉㄜ˙)."""
    return syllable[1:] + "˙" if syllable.startswith("˙") else syllable


def _key(reading: str) -> str:
    key = _ENG.sub("ㄣ", _TONES.sub("", reading))
    for a, b in _FUZZY:
        key = key.replace(a, b)
    return key


def _pypinyin_reading(word: str) -> str:
    return " ".join(_toned(s) for s in lazy_pinyin(word, style=Style.BOPOMOFO))


def _build() -> tuple[dict, dict]:
    index: dict[str, list[tuple[str, str, int]]] = {}
    words: dict[str, tuple[int, list[str]]] = {}
    with gzip.open(_DICT, "rt", encoding="utf-8") as f:
        for line in f:
            word, reading, freq = line.rstrip("\n").split("\t")
            freq = int(freq)
            readings = words.setdefault(word, (freq, []))[1]
            readings.append(reading)
    for word, (freq, readings) in words.items():
        keys = {_key(r): r for r in readings}
        if len(word) > 1:
            spoken = _pypinyin_reading(word)
            keys.setdefault(_key(spoken), spoken)
        for key, reading in keys.items():
            index.setdefault(key, []).append((word, reading, freq))
    return index, words


def load() -> dict:
    """The reading index, built (and cached) on first use. Safe to call from any thread."""
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
                log.info("building word index…")
                index, words = _build()
                _CACHE.parent.mkdir(parents=True, exist_ok=True)
                _CACHE.write_bytes(pickle.dumps((_CACHE_VERSION, index, words)))
                _OLD_CACHE.unlink(missing_ok=True)
                _words, _index = words, index
                log.info("word index: %d words, %d readings", len(_words), len(_index))
        return _index


def is_word(word: str) -> bool | None:
    """Whether word (Traditional Chinese) is a dictionary word; None while the dictionary is still loading
    (never blocks)."""
    if _index is None:
        return None
    return word in _words


def frequency(word: str) -> int:
    """How common word is (0 if unknown or still loading)."""
    return _words.get(word, (0, []))[0]


def preload() -> None:
    threading.Thread(target=load, daemon=True).start()


def _readings(word: str) -> list[str]:
    """Toned readings of word: the dictionary's and the spoken one (pypinyin), else character by character
    (first-listed reading)."""
    if word in _words:
        readings = list(_words[word][1])
        if len(word) > 1 and (spoken := _pypinyin_reading(word)) not in readings:
            readings.append(spoken)
        return readings
    return [" ".join(_words[c][1][0] if c in _words else _pypinyin_reading(c) for c in word)]


loose = True   # also other tones and fuzzy readings (setting loose_homophones); False: the same reading only


def homophones(word: str, limit: int = 8) -> list[str]:
    """Words that sound like word (Traditional Chinese), best first; word itself excluded."""
    word = word.strip()
    if not _CJK.match(word) or len(word) > 6:
        return []
    index = load()
    readings = _readings(word)
    found: dict[str, tuple[int, int]] = {}
    for reading in readings:
        toneless = _TONES.sub("", reading)
        for w, r, freq in index.get(_key(reading), []):
            rank = 0 if r == reading else 1 if _TONES.sub("", r) == toneless else 2
            if w != word and (loose or rank == 0) and (w not in found or rank < found[w][0]):
                found[w] = (rank, -freq)
    return sorted(found, key=found.get)[:limit]
