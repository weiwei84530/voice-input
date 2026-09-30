"""VoiceInput: single-process tray app. Hold the hotkey to talk, release to paste.

What an utterance does:
1. Text is selected (UI Automation): the utterance replaces it. When it sounds like what it replaced (城市 → 程式),
   the candidate menu opens for it (edit.similar).
2. Otherwise it is dictated: formatted, hotwords applied, pasted. A context hotword that could not decide, or a
   word the second model heard differently, is offered in a menu.
A bare punctuation name (逗號, 句號 …) is typed as the mark (edit.symbol).

The hotkey on selected text (2026-09-30, replacing the spoken 刪除 / 選字): one tap opens the candidate menu, a
double tap deletes the selection, a double tap held deletes it and dictates in its place. Without a selection a
double tap undoes the last change and holding the second press records it again (a re-dictation, which opens the
candidate menu for a changed word that sounds alike). CapsLock taps on a selection do not toggle caps.
Menus are answered with the mouse only (decided 2026-09-29); picked words come first in later menus (picks.py).
A pick that looks like a correction is offered as a hotword (✓ box), as is a word the user fixed by hand.

Models are fixed (models.py): X-ASR recognises, SenseVoice gives a second opinion.
"""
import logging
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PySide6.QtCore import QLockFile, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import autostart, candidates, edit, models, paths, suspects
from .asr import Recognizer
from .audio import Recorder
from .config import Config
from .hotkey import DOUBLE_TAP_GAP, TAP_THRESHOLD, PushToTalk
from .hotwords import HotwordStore, context_words, worth_learning
from .output import backspace, click_at, paste_text, press_delete
from .overlay import Overlay
from .picker import Picker
from .picks import PickHistory
from .selection import Context, ContextProbe, caret_rect, term_watcher, visible_text
from .session import Session, foreground, redictation_pair, typed_fix
from .settings import SettingsDialog
from .textfmt import format_text, to_traditional

log = logging.getLogger("voiceinput")

_SCREEN_TERM_SCORE = 1.5
_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿]")
_FIX_IDLE_MS = 3000        # after the user stops typing, look for a hand-made correction of the dictation
_FIX_WINDOW = 120.0        # seconds after a dictation during which hand-made corrections are looked for
_MENU_ROWS = 8
_INDICATOR_DELAY = 0.1     # seconds held before the recording indicator shows (a tap is < TAP_THRESHOLD)


@dataclass
class _Press:
    """One press of the hotkey."""
    probe: object                     # ContextProbe started at key-down (a double tap reuses the first tap's)
    first: "_Press | None" = None     # the second press of a double tap: its first tap
    up_at: float = 0.0                # release time of a tap
    followed: bool = False            # a tap that became the first half of a double tap
    deleted: object = None            # Selection this double tap deleted (a held second press dictates there)
    resolved: threading.Event = field(default_factory=threading.Event)   # the double tap's action is done


@dataclass
class _Fix:
    """Text that replaced something sounding alike, now at buffer[a:b]: the candidate menu is offered for it."""
    a: int
    b: int
    old: str                          # what it replaced
    new: str                          # what was pasted
    before: str                       # text on the line before / after it (one-character fixes learn neighbours)
    after: str
    context: list
    rect: object = None


def make_icon(color: QColor) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(22, 6, 20, 34), 10, 10)          # mic capsule
    pen = QPen(color, 5)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(14, 16, 36, 34), 200 * 16, 140 * 16)     # holder
    p.drawLine(QPointF(32, 50), QPointF(32, 58))
    p.end()
    return QIcon(pm)


class App(QObject):
    pressed = Signal(bool)                   # double
    released = Signal(float, bool)           # held seconds, double
    key_typed = Signal()                     # the user pressed a key (not the hotkey)
    main = Signal(object)                    # run a callable on the Qt main thread (keystrokes, clipboard, UI)
    edited = Signal(object, str, bool)       # Selection, replacement, is a bare symbol: spoken on a selection
    candidates_ready = Signal(object, list)  # Selection, candidates for the 選字 menu
    suspects_ready = Signal(object, list)    # Utterance, [suspects.Suspect]
    record = Signal(dict)         # one transcript log entry for the settings window
    status = Signal(str)

    def __init__(self, qapp: QApplication):
        super().__init__()
        self.qapp = qapp
        self.cfg = Config.load()
        self.recorder = Recorder()
        self.recognizer: Recognizer | None = None
        self.second: Recognizer | None = None         # second-opinion model (SenseVoice)
        self.recording = False
        self.worker = ThreadPoolExecutor(max_workers=1)   # serializes model loads and transcription
        self.bg = ThreadPoolExecutor(max_workers=1)       # second opinion, second model load
        self._status_text = ""
        self._records = deque(maxlen=100)       # transcript log, kept for this session only
        self.hotwords = HotwordStore()
        self.session = Session()
        self._recent = deque(maxlen=10)         # recent dictations (for 選字 second opinions)
        self.picks = PickHistory()
        self._press: _Press | None = None
        self._tap: _Press | None = None          # the last single tap, until the next press
        # ("candidates", Selection) | ("suspect", Utterance, Suspect) | ("fix", _Fix)
        # | ("learn", key, value, context words): the ✓ box offering a new hotword
        self._menu = None

        self.icon_idle = make_icon(QColor(235, 235, 235) if self._dark_taskbar() else QColor(40, 40, 40))
        self.icon_rec = make_icon(QColor(255, 69, 58))

        self.overlay = Overlay(lambda: self.recorder.level)
        self.picker = Picker()
        self.picker.timeout_ms = self.cfg.menu_seconds * 1000
        self.picker.picked.connect(self.on_menu_pick)
        self.picker.confirmed.connect(self.on_learn_confirmed)
        self.picker.declined.connect(self.on_menu_declined)
        self.settings = SettingsDialog(self.cfg, self.hotwords)
        self.settings.applied.connect(self.apply_settings)
        self._fix_timer = QTimer(self, singleShot=True, interval=_FIX_IDLE_MS, timeout=self._check_typed_fix)

        self.tray = QSystemTrayIcon(self.icon_idle)
        menu = QMenu()
        menu.addAction(QAction("設定…", menu, triggered=self.open_settings))
        menu.addSeparator()
        menu.addAction(QAction("結束", menu, triggered=self.quit))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.open_settings() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        self.pressed.connect(self.on_press)
        self.released.connect(self.on_release)
        self.key_typed.connect(self.on_key_typed)
        self.main.connect(lambda fn: fn())
        self.edited.connect(self.on_edited)
        self.candidates_ready.connect(self.on_candidates)
        self.suspects_ready.connect(self.on_suspects)
        self.status.connect(self.on_status)
        self.record.connect(self.on_record)

        self.ptt = PushToTalk(self.cfg.hotkey, self.pressed.emit, self.released.emit, self._on_other_key)
        self.ptt.start()
        self._mouse = self._watch_mouse()
        self.worker.submit(self._load_model_job)
        self.bg.submit(self._load_second_job)
        candidates.preload()   # 選字, suspects, hotword contexts and the disfluency pass use the dictionary
        if self.cfg.autostart:
            self._set_autostart()   # re-register so the entry follows the app if its folder was moved

    @staticmethod
    def _dark_taskbar() -> bool:
        if sys.platform != "win32":
            return False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 0
        except OSError:
            return True

    def _on_other_key(self, vk: int):
        """Keyboard hook thread: a key the user pressed (injected keys never get here)."""
        self.session.on_key(vk)
        term_watcher.forget()
        self.key_typed.emit()

    def _watch_mouse(self):
        """Any click may move the caret, which ends what session.py knows about the text before it. Left button
        drags may select text in a terminal (selection.TermWatcher). Our own clicks (injected) are skipped."""
        if sys.platform != "win32":
            return None
        from pynput import mouse
        downs = {0x201, 0x204, 0x207, 0x20B}   # left / right / middle / x button down

        def on_event(msg, data):
            if data.flags & 1:   # LLMHF_INJECTED
                return False
            if msg in downs:
                self.session.on_click(data.pt.x, data.pt.y)
                if msg == 0x201:
                    term_watcher.mouse_down(data.pt.x, data.pt.y)
                else:
                    term_watcher.forget()
            elif msg == 0x202:
                term_watcher.mouse_up(data.pt.x, data.pt.y)
            return False   # never passed on to pynput's (unused) callbacks: keeps the hook cheap
        listener = mouse.Listener(win32_event_filter=on_event)
        listener.daemon = True
        listener.start()
        return listener

    # --- models ---
    def _load_model_job(self):
        key = models.MAIN_MODEL
        try:
            if not models.is_installed(key):
                def progress(done, total):
                    pct = f"{done * 100 // total}%" if total else f"{done >> 20} MB"
                    self.status.emit(f"下載模型中… {pct}")
                models.download(key, progress)
            self.status.emit("載入模型中…")
            self.recognizer = Recognizer(key)
            self.status.emit("就緒")
        except Exception as e:
            log.exception("model load failed")
            self.status.emit(f"模型載入失敗：{e}")

    def _load_second_job(self):
        key = models.SECOND_MODEL
        try:
            if not models.is_installed(key):
                models.download(key)
            t0 = time.monotonic()
            self.second = Recognizer(key)
            log.info("%s loaded in %.1fs", key, time.monotonic() - t0)
        except Exception:
            log.exception("%s failed to load", key)

    # --- push-to-talk ---
    def on_press(self, double: bool):
        first = self._tap if double else None
        self._tap = None
        self._fix_timer.stop()
        if first is not None:
            first.followed = True      # its candidate menu must not open
        press = _Press(first.probe if first else ContextProbe(), first)
        self._press = press
        if first is not None:
            threading.Thread(target=self._double_job, args=(press,), daemon=True).start()
        if self.recognizer is None:
            if not double:
                self.tray.showMessage("VoiceInput", self._status_text or "模型尚未就緒", self.icon_idle, 2000)
            return
        try:
            self.recorder.start(self.cfg.mic)
        except Exception as e:
            log.exception("mic start failed")
            self.tray.showMessage("VoiceInput", f"無法開啟麥克風：{e}", self.icon_idle, 3000)
            return
        self.recording = True
        self._hwnd = foreground()
        # recording starts now (no lost first syllable), but the indicator waits a little so taps and double taps
        # do not flash it (0.1s, not TAP_THRESHOLD: the user found 0.3s and 0.2s too slow; a slow tap may flash it)
        QTimer.singleShot(int(_INDICATOR_DELAY * 1000), lambda: self._show_recording(press))

    def _show_recording(self, press: _Press):
        if self.recording and self._press is press:
            self.tray.setIcon(self.icon_rec)
            self.overlay.show_recording()

    def on_release(self, held: float, double: bool):
        press = self._press
        audio = None
        if self.recording:
            audio = self.recorder.stop()
            self.recording = False
            self.tray.setIcon(self.icon_idle)
        tap = held < TAP_THRESHOLD
        if audio is not None and not tap:
            self.overlay.show_thinking()
            self.worker.submit(self._transcribe_job, self.recognizer, audio, press, self._hwnd)
        else:
            self.overlay.hide_overlay()
        if press is None:
            return
        caps = self.cfg.hotkey == "caps_lock"
        if double:
            # without a selection the second press is replayed as before: two taps cancel out, and a held
            # second press replays one more tap to undo the first tap's CapsLock toggle
            self._when_probed(press.first.probe,
                              lambda ctx: None if ctx.selection or not (tap or caps) else self.ptt.tap())
        elif tap:
            press.up_at = time.monotonic()
            self._tap = press
            if caps:
                self.ptt.tap()   # at once: letters typed right after a CapsLock tap must not wait for UIA
            self._when_probed(press.probe, lambda ctx: self._single_tap(press, ctx.selection))

    def _when_probed(self, probe, fn):
        """Run fn(Context) on the main thread once probe has read the focused app."""
        def wait():
            ctx = probe.result()
            self.main.emit(lambda: fn(ctx))
        threading.Thread(target=wait, daemon=True).start()

    def _single_tap(self, press: _Press, sel):
        """Main thread: a single tap, once we know whether text was selected."""
        caps = self.cfg.hotkey == "caps_lock"
        if sel is None:
            if not caps:
                self.ptt.tap()   # a plain tap of F12 / right Alt goes to the app
            return
        if caps:
            self.ptt.tap()       # a tap on a selection is ours: undo the CapsLock toggle
        wait = DOUBLE_TAP_GAP - (time.monotonic() - press.up_at)
        QTimer.singleShot(max(0, int(wait * 1000)), lambda: self._open_candidates(press, sel))

    def _open_candidates(self, press: _Press, sel):
        if press.followed or self.recording or self._press is not press:
            return
        log.info("candidates for %r on %s", sel.text, sel.app)
        self._menu = ("candidates", sel)
        self.picker.show_loading(sel.text, sel.rect)
        self.session.ignore_rects = [self.picker.physical_rect()]
        self.bg.submit(self._candidates_job, sel)

    def _candidates_job(self, sel):
        try:
            words = self._menu_words(sel.text)
        except Exception:
            log.exception("candidates failed")
            words = candidates.homophones(sel.text)
        self.candidates_ready.emit(sel, words)

    def _double_job(self, press: _Press):
        """Second press of a double tap: delete the selected text, or undo the last change if nothing was
        selected. A transcription of a held second press waits for this (press.resolved)."""
        try:
            sel = press.first.probe.result().selection
            press.deleted = sel
            self.main.emit(lambda: self._double_action(press, sel))
        except Exception:
            log.exception("double tap failed")
            press.resolved.set()

    def _double_action(self, press: _Press, sel):
        try:
            if sel is None:
                self.undo()
                return
            self._close_menu()
            log.info("double tap: delete %r on %s", sel.text, sel.app)
            _delete_selection(sel)
            self.session.replaced_selection(sel.before, sel.text, "", foreground(), f"刪除「{sel.text}」")
            self.record.emit({"time": time.strftime("%H:%M:%S"), "asr": "", "asr_s": None, "fmt": "",
                              "edit": f"刪除「{sel.text}」（快按兩下）"})
        finally:
            press.resolved.set()

    def _close_menu(self):
        if self.picker.isVisible():
            self.picker.hide()
        self._menu = None
        self.session.ignore_rects = []

    def _bias(self, ctx: Context) -> list[tuple[str, float]]:
        return self.hotwords.bias_words() + [(t, _SCREEN_TERM_SCORE) for t in ctx.terms]

    def _transcribe_job(self, rec, audio, press: _Press, hwnd):
        stamp = time.strftime("%H:%M:%S")
        try:
            ctx = press.probe.result()
            if press.first is not None:
                press.resolved.wait(3.0)
            t0 = time.monotonic()
            raw = rec.recognize(audio, self._bias(ctx))
            t_asr = time.monotonic() - t0
            log.info("asr %.2fs: %s%s", t_asr, raw, f" (terms: {len(ctx.terms)})" if ctx.terms else "")
            formatted = format_text(raw, self.cfg.strip_trailing_punct)
            sym = edit.symbol(formatted)
            text, asks = (sym, []) if sym else self.hotwords.apply(formatted)
            entry = {"time": stamp, "asr": to_traditional(raw), "asr_s": t_asr, "fmt": text}
            if press.first is None and ctx.selection is not None:
                self._edit_job(ctx.selection, text, bool(sym), entry)
                return
            replaced = press.deleted if press.first is not None else None
            if replaced is not None:
                if not sym:
                    text = edit.replacement(replaced.text, text)
                entry["edit"] = f"{replaced.text} → {text}（快按兩下重講）"
            self.record.emit(entry)
            self.main.emit(lambda: self._dictate(text, audio, hwnd, asks, replaced, bool(sym)))
        except Exception:
            log.exception("transcribe failed")
            self.main.emit(self.overlay.hide_overlay)

    # --- dictation ---
    def _dictate(self, text: str, audio, hwnd: int, asks=(), replaced=None, sym=False):
        """Main thread: paste a dictation (replaced: the Selection a double tap deleted for it), open the
        candidate menu when it re-says something that sounds alike, offer undecided context hotwords, start the
        second opinion."""
        self.overlay.hide_overlay()
        log.info("result: %s", text)
        if not text:
            return
        s = self.session
        prev = s.redictation_of() if replaced is None and not sym else None
        paste_text(text)
        u = s.dictated(text, audio, foreground() or hwnd)
        if sym:
            return
        self._recent.append(u)
        fix = None
        if replaced is not None:
            if edit.similar(replaced.text, text):
                fix = _Fix(u.start, u.end, replaced.text, text, replaced.before, replaced.after,
                           _selection_context(replaced), replaced.rect)
        elif prev is not None:
            pair = redictation_pair(prev.text, text)
            log.info("re-dictation of %r: %s", prev.text, pair)
            i = text.find(pair[1]) if pair else -1
            if i >= 0 and edit.similar(*pair):
                j = i + len(pair[1])
                i2, j2 = _word_around(text, i, j)      # 黨案 → 檔案 differs in one character: offer the word
                old, new = text[i2:i] + pair[0] + text[j:j2], text[i2:j2]
                a, b = u.start + i2, u.start + j2
                fix = _Fix(a, b, old, new, s.buffer[:a], s.buffer[b:], context_words(text, i2, j2))
        if fix is not None:
            self.bg.submit(self._fix_job, fix)
        elif asks:
            a = asks[0]
            self.on_suspects(u, [suspects.Suspect(a.start, a.end, text[a.start:a.end], [a.hotword.value],
                                                  a.hotword, a.words)])
        elif audio is not None:
            self.bg.submit(self._suspect_job, u)

    def _second_text(self, u) -> str | None:
        """The second model's formatted transcript of a dictation (cached on it)."""
        if u.second is None and self.second is not None and u.audio is not None:
            t0 = time.monotonic()
            u.second = format_text(self.second.recognize(u.audio), self.cfg.strip_trailing_punct)
            log.info("second opinion %.2fs: %s", time.monotonic() - t0, u.second)
        return u.second

    def _suspect_job(self, u):
        try:
            found = suspects.find(u.text, self._second_text(u))
            # a word a hotword produced or confirmed is the user's own choice: never offer it back
            values = {h.value for h in self.hotwords.items}
            found = [f for f in found if f.word not in values]
            if found:
                self.suspects_ready.emit(u, found)
        except Exception:
            log.exception("suspect check failed")

    def on_suspects(self, u, found: list):
        s = self.session
        if self.recording or self.picker.isVisible() or u not in s.utterances or not s.clean():
            return
        sus = found[0]
        log.info("offer: %s -> %s%s", sus.word, sus.options, " (context hotword)" if sus.hotword else "")
        self._menu = ("suspect", u, sus)
        if sus.hotword is not None:
            self.picker.show_list(f"「{sus.word}」換成？", sus.options, caret_rect(),
                                  keep=f"保留「{sus.word}」")
        else:
            self.picker.show_list(f"可能聽錯「{sus.word}」", sus.options, caret_rect())
        s.ignore_rects = [self.picker.physical_rect()]

    # --- undo (double tap) ---
    def undo(self):
        """Main thread: rewrite the text before the caret back to before the last change (a dictation, a
        replaced selection, a picked suggestion)."""
        if self._menu and self._menu[0] in ("learn", "fix"):
            self._close_menu()        # the change it was offered for is being undone
        s = self.session
        step = s.pop_undo()
        if step is None:
            self._notify("沒有可以撤銷的（游標移動過，或還沒有輸入）")
            return
        n, tail = s.plan_to(step.before)
        log.info("undo %s: %d backspaces + %r", step.note, n, tail)
        backspace(n)
        paste_text(tail)
        s.undone(step)

    def _rewrite(self, a: int, b: int, new: str, note: str, learn=None) -> bool:
        """Main thread: replace buffer[a:b] with new by Backspace + paste."""
        s = self.session
        if not s.clean():
            self._notify("游標已經移動過，沒有修改")
            return False
        n, tail = s.plan_replace(a, b, new)
        log.info("rewrite: %d backspaces + %r (%s)", n, tail, note)
        backspace(n)
        paste_text(tail)
        if learn:
            old, new_word = _with_neighbours(s.buffer[:a], learn[0], learn[1], s.buffer[b:])
            self._learn(old, new_word, learn[2], context=context_words(s.buffer, a, b))
        s.apply_replace(a, b, new, note)
        return True

    def _notify(self, text: str):
        self.tray.showMessage("VoiceInput", text, self.icon_idle, 2500)

    # --- corrections made by hand ---
    def on_key_typed(self):
        u = self.session.last
        if u is not None and not u.fix_offered and time.monotonic() - u.at < _FIX_WINDOW:
            self._fix_timer.start()

    def _check_typed_fix(self):
        """The user stopped typing after a dictation: if the text box now holds it with one word changed by
        hand (城市 → 程式), offer that as a hotword."""
        u, hwnd = self.session.last, self.session.hwnd
        if u is None or u.fix_offered or self.recording or self._offering() or foreground() != hwnd:
            return

        def read():
            fix = typed_fix(u.text, visible_text())
            if fix:
                self.main.emit(lambda: self._offer_typed_fix(u, hwnd, *fix))
        threading.Thread(target=read, daemon=True).start()

    def _offering(self) -> bool:
        """The ✓ box is up. Other menus give way: once the user types, their positions in the text are stale."""
        return self.picker.isVisible() and self._menu is not None and self._menu[0] == "learn"

    def _offer_typed_fix(self, u, hwnd: int, old: str, new: str):
        if u.fix_offered or self.recording or self._offering() or foreground() != hwnd:
            return
        u.fix_offered = True
        i = u.text.find(old)
        if i >= 0:
            old, new = _with_neighbours(u.text[:i], old, new, u.text[i + len(old):])
            i = u.text.find(old)
        log.info("typed fix of %r: %s -> %s", u.text, old, new)
        self._learn(old, new, False, source="手動修改",
                    context=context_words(u.text, i, i + len(old)) if i >= 0 else None)

    # --- the candidate menu, utterances on a selection ---
    def _alternatives(self, word: str) -> list[str]:
        """What else word could be, best first: the second model's version of it (when it came from a recent
        dictation), the value of a learned hotword for it, then same-sounding words by frequency."""
        alts = []
        for u in reversed(self._recent):
            i = u.text.rfind(word)
            if i < 0:
                continue
            for s, e, alt in suspects.disagreements(u.text, self._second_text(u) or ""):
                if i <= s and e <= i + len(word):
                    w = word[:s - i] + alt + word[e - i:]
                    if w not in alts:
                        alts.append(w)
            break
        h = self.hotwords.find(word)
        if h is not None and h.value not in alts:
            alts.append(h.value)
        return alts + [w for w in candidates.homophones(word) if w not in alts and w != word]

    def _menu_words(self, word: str, exclude=()) -> list[str]:
        """Candidate menu rows for word: words the user picked before that sound like it, then _alternatives."""
        out = []
        for w in self.picks.like(word) + self._alternatives(word):
            if w != word and w not in exclude and w not in out:
                out.append(w)
        return out[:_MENU_ROWS]

    def _edit_job(self, sel, text: str, sym: bool, entry: dict):
        """Worker thread: an utterance spoken on a selection replaces it."""
        rep = text if sym else edit.replacement(sel.text, text)
        entry["edit"] = f"{sel.text} → {rep}"
        log.info("edit on %s: %s", sel.app, entry["edit"])
        self.record.emit(entry)
        self.edited.emit(sel, rep, sym)

    def on_edited(self, sel, rep: str, sym: bool):
        self.overlay.hide_overlay()
        if not rep:
            return
        _paste_over(sel, rep)
        self.session.replaced_selection(sel.before, sel.text, rep, foreground(), f"{sel.text} → {rep}")
        if not sym and edit.similar(sel.text, rep):
            n = len(sel.before)
            self.bg.submit(self._fix_job, _Fix(n, n + len(rep), sel.text, rep, sel.before, sel.after,
                                               _selection_context(sel), sel.rect))

    def _fix_job(self, fix: _Fix):
        """Background: menu rows for text that replaced something sounding alike. What was pasted comes first,
        then words picked before, then alternatives of it and of what it replaced (never the replaced text)."""
        try:
            rows = [fix.new] if fix.new != fix.old else []
            if not self.cfg.fix_menu:     # setting off: straight to the ✓ box (_offer_fix)
                self.main.emit(lambda: self._offer_fix(fix, rows))
                return
            rows += self._menu_words(fix.new, exclude=rows + [fix.old])
            if fix.new != fix.old:
                rows += self._menu_words(fix.old, exclude=rows)
            rows = rows[:_MENU_ROWS]
            self.main.emit(lambda: self._offer_fix(fix, rows))
        except Exception:
            log.exception("fix menu failed")

    def _offer_fix(self, fix: _Fix, rows: list):
        s = self.session
        if self.recording or self._offering() or not s.clean() or s.buffer[fix.a:fix.b] != fix.new:
            return
        undone = self._undone_hotword(fix.old, fix.new)
        if undone is not None:
            self._learn(fix.old, fix.new, False, context=fix.context)   # records where the hotword was wrong
        if rows == [fix.new]:
            # nothing else to choose from (an English word): offer the hotword directly
            if undone is None:
                self._learn(*_with_neighbours(fix.before, fix.old, fix.new, fix.after), False, source="取代",
                            context=fix.context)
            return
        if not rows:
            return
        log.info("fix menu: %s (was %s): %s", fix.new, fix.old, rows)
        self._menu = ("fix", fix)
        self.picker.show_list(f"選字：{fix.new}（原本「{fix.old}」）", rows, fix.rect or caret_rect())
        s.ignore_rects = [self.picker.physical_rect()]

    def on_candidates(self, sel, words: list):
        if self._menu and self._menu[0] == "candidates" and self._menu[1] is sel and self.picker.isVisible():
            self.picker.show_candidates(sel.text, words)
            self.session.ignore_rects = [self.picker.physical_rect()]

    def on_menu_pick(self, row: int):
        menu, words = self._menu, self.picker.candidates
        self._menu = None
        self.session.ignore_rects = []
        if menu is None or not 0 <= row < len(words):
            return
        word = words[row]
        self.picks.add(word)
        if menu[0] == "candidates":
            sel = menu[1]
            log.info("menu pick: %s -> %s", sel.text, word)
            _paste_over(sel, word)
            self._learn(*_with_neighbours(sel.before, sel.text, word, sel.after), False,
                        context=_selection_context(sel))
            self.session.replaced_selection(sel.before, sel.text, word, foreground(), f"{sel.text} → {word}")
            return
        s = self.session
        if menu[0] == "fix":
            fix = menu[1]
            if s.buffer[fix.a:fix.b] != fix.new:
                self._notify("文字已經改變，沒有修改")
                return
            log.info("fix pick: %s -> %s (was %s)", fix.new, word, fix.old)
            if word != fix.new and not self._rewrite(fix.a, fix.b, word, f"{fix.new} → {word}"):
                return
            self._learn(*_with_neighbours(fix.before, fix.old, word, fix.after), False, source="選字",
                        context=fix.context)
            return
        _, u, sus = menu
        a, b = u.start + sus.start, u.start + sus.end
        if s.buffer[a:b] != sus.word:
            a = s.buffer.rfind(sus.word)
            if a < 0:
                self._notify("文字已經改變，沒有修改")
                return
            b = a + len(sus.word)
        log.info("suspect pick: %s -> %s", sus.word, word)
        if sus.hotword is not None:
            sus.hotword.remember(sus.words, True)
            sus.hotword.hits += 1
            self.hotwords.save()
            self._rewrite(a, b, word, f"{sus.word} → {word}")
        else:
            self._rewrite(a, b, word, f"{sus.word} → {word}", learn=(sus.word, word, False))

    def on_menu_declined(self):
        """保留「X」clicked on a context hotword question: remember these words as a place not to replace."""
        menu, self._menu = self._menu, None
        self.session.ignore_rects = []
        if menu and menu[0] == "suspect" and menu[2].hotword is not None:
            sus = menu[2]
            log.info("hotword %s declined near %s", sus.hotword.key, sus.words)
            sus.hotword.remember(sus.words, False)
            self.hotwords.save()

    def _learn(self, old: str, new: str, spelled: bool, source: str = "", context=None):
        """If the edit looks like a correction of a misrecognition, offer old -> new as a hotword in the ✓ box.
        Nothing is added until the user clicks ✓ (decided 2026-09-29)."""
        old, new = old.strip(), new.strip()
        undone = self._undone_hotword(old, new)
        if undone is not None:
            # a hotword's replacement changed back (程式 -> 城市): not a new correction but a place where
            # 城市 -> 程式 was wrong, as if 保留 had been clicked; a reverse hotword would fight the original
            log.info("hotword %s -> %s undone near %s", undone.key, undone.value, context)
            if context and undone.mode != "always":
                undone.remember(context, False)
                self.hotwords.save()
            return
        if not worth_learning(old, new, spelled):
            return
        known = self.hotwords.find(old)
        if known is not None and known.value == new:
            if context:
                known.remember(context, True)   # one more sentence where it was right
                self.hotwords.save()
            return
        if known is not None:
            # 城市 -> 乘勢 picked once must not overwrite 城市 -> 程式; this sentence just was not a 程式 one
            log.info("not offering %s -> %s: hotword %s -> %s exists", old, new, known.key, known.value)
            if context and known.mode != "always":
                known.remember(context, False)
                self.hotwords.save()
            return
        log.info("offer hotword%s: %s -> %s near %s", f" ({source})" if source else "", old, new, context)
        self._menu = ("learn", old, new, context)
        self.picker.show_confirm("加入熱詞？點 ✓ 加入", f"{old} → {new}", caret_rect())
        self.session.ignore_rects = [self.picker.physical_rect()]

    def _undone_hotword(self, old: str, new: str):
        """The hotword whose value old holds and whose key new puts back (程式 -> 城市 for 城市 -> 程式; also
        with neighbouring characters: 張育薇 -> 張雨薇), or None."""
        for h in self.hotwords.items:
            if not h.key or not h.value:
                continue
            i = old.casefold().find(h.value.casefold())
            if i >= 0 and (old[:i] + h.key + old[i + len(h.value):]).casefold() == new.casefold():
                return h
        return None

    def on_learn_confirmed(self):
        menu, self._menu = self._menu, None
        self.session.ignore_rects = []
        if not menu or menu[0] != "learn":
            return
        _, old, new, context = menu
        h = self.hotwords.add(old, new, context=context)
        log.info("hotword added: %s -> %s near %s", h.key, h.value, context)
        self.settings.refresh_hotwords()

    # --- settings / status ---
    def on_status(self, text: str):
        self._status_text = text
        log.info("status: %s", text)
        self.tray.setToolTip(f"VoiceInput — {text}")
        self.settings.set_status(text)

    def on_record(self, entry: dict):
        self._records.append(entry)
        self.settings.set_log(self._records)

    def open_settings(self):
        self.settings.load_values()
        self.settings.set_log(self._records)
        self.settings.set_status(self._status_text)
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def apply_settings(self):
        self.ptt.set_key(self.cfg.hotkey)
        self.picker.timeout_ms = self.cfg.menu_seconds * 1000
        self._set_autostart()

    def _set_autostart(self):
        try:
            autostart.set_enabled(self.cfg.autostart)
        except Exception as e:
            log.exception("autostart failed")
            self.settings.set_status(f"開機啟動設定失敗：{e}")

    def quit(self):
        self.ptt.stop()
        if self._mouse:
            self._mouse.stop()
        self.tray.hide()
        self.qapp.quit()


def _with_neighbours(before: str, old: str, new: str, after: str) -> tuple[str, str]:
    """A one-character correction (張雨薇: 雨 -> 育) is learned with its neighbouring characters (張雨薇 -> 張育薇):
    a single-character hotword would change every 雨."""
    if len(old.strip()) != 1 or not _CJK_CHAR.match(old.strip()):
        return old, new
    left = before[-1:] if before[-1:] and _CJK_CHAR.match(before[-1:]) else ""
    right = after[:1] if after[:1] and _CJK_CHAR.match(after[:1]) else ""
    return left + old.strip() + right, left + new.strip() + right


def _word_around(text: str, i: int, j: int) -> tuple[int, int]:
    """The span of the longest dictionary word (2-4 characters) in text that covers text[i:j], else (i, j)."""
    if j - i >= 4 or not all(_CJK_CHAR.match(c) for c in text[i:j]):
        return i, j
    for n in (4, 3, 2):
        for s in range(max(0, j - n), min(i, len(text) - n) + 1):
            if candidates.pos_tag(text[s:s + n]):
                return s, s + n
    return i, j


def _delete_selection(sel):
    """Delete the selected text. In a terminal a click right after it moves the cursor there first
    (selection.TermWatcher), then Backspace removes it."""
    if sel.click is None:
        press_delete()
        return
    term_watcher.forget()
    click_at(*sel.click)
    backspace(len(sel.text))


def _paste_over(sel, text: str):
    """Replace the selected text: a paste replaces a selection, except in a terminal."""
    if sel.click is not None:
        _delete_selection(sel)
    paste_text(text)


def _selection_context(sel) -> list[str]:
    line = sel.before + sel.text + sel.after
    return context_words(line, len(sel.before), len(sel.before) + len(sel.text))


def main():
    paths.migrate_legacy()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(paths.LOG_PATH, encoding="utf-8"),
                  logging.StreamHandler()],
    )
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)

    lock = QLockFile(str(paths.LOCK_PATH))
    if not lock.tryLock(100):
        log.info("already running")
        return 0

    app = App(qapp)  # noqa: F841 (keep reference alive)
    return qapp.exec()
