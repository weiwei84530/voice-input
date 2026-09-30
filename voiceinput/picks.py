"""Words the user picked in a menu, kept across runs (DATA_DIR/picks.json) and put first in later menus.

Matched by sound, not by the word that was selected: once 程式 has been picked, any selection that sounds like it
(城市, 成是) lists 程式 first. Latin words match by spelling (edit distance, as hotwords do). Most recent first.
"""
import json
import logging
import threading
import time

from .candidates import syllables
from .hotwords import spelled_alike
from .paths import PICKS_PATH

log = logging.getLogger("voiceinput")

_MAX = 500


class PickHistory:
    def __init__(self, path=PICKS_PATH):
        self.path = path
        self._lock = threading.Lock()
        try:
            self._at: dict[str, float] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._at = {}

    def add(self, word: str) -> None:
        word = word.strip()
        if not word:
            return
        with self._lock:
            self._at.pop(word, None)
            self._at[word] = time.time()
            for old in sorted(self._at, key=self._at.get)[:-_MAX]:
                del self._at[old]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._at, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError:
                log.exception("saving picks failed")

    def like(self, word: str) -> list[str]:
        """Picked words that sound like word (itself included if it was picked), most recent first."""
        word = word.strip()
        if not word:
            return []
        sounds = syllables(word) if not word.isascii() else None
        with self._lock:
            ranked = sorted(self._at, key=self._at.get, reverse=True)
        out = []
        for w in ranked:
            if w == word or (sounds is not None and len(w) == len(word) and not w.isascii()
                             and syllables(w) == sounds) or (sounds is None and spelled_alike(w, word)):
                out.append(w)
        return out
