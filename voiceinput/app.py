"""VoiceInput: single-process tray app. Hold the hotkey to talk, release to paste.

What an utterance does, in order:
1. A suggestion menu is open (選字 or 可能聽錯): 第二個 / 對 picks, 不用 closes, anything else closes it and goes on.
2. Text is selected (UI Automation): the utterance edits the selection (edit.py).
3. A voice command (commands.py): 復原, 送出, 換行, 刪掉上一句, 城市改成程式 … on what was just dictated
   (session.py), without selecting anything.
4. Otherwise it is dictated: formatted, hotwords applied, pasted; a trailing 送出 after a pause presses Enter.
   A context hotword that could not decide, or a word the second model heard differently, is offered in a menu.

Models are fixed (models.py): X-ASR recognises, SenseVoice gives a second opinion, streaming X-ASR shows captions.
There is no LLM (removed 2026-09-29 to keep the app light).
"""
import logging
import re
import sys
import time
from collections import deque
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QLockFile, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import autostart, candidates, commands, edit, models, paths, suspects
from .asr import Recognizer, StreamingPreview
from .audio import Recorder
from .config import Config
from .hotkey import TAP_THRESHOLD, PushToTalk
from .hotwords import HotwordStore, context_words, worth_learning
from .output import backspace, paste_text, press_delete, press_enter, press_newline
from .overlay import Overlay
from .picker import Picker
from .selection import Context, ContextProbe, caret_rect
from .session import Session, foreground, redictation_pair
from .settings import SettingsDialog
from .textfmt import format_text, to_traditional

log = logging.getLogger("voiceinput")

_SCREEN_TERM_SCORE = 1.5
_PUNCT_END = "，。、！？；：,.!?;:…"
_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿]")


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
    pressed = Signal()
    released = Signal(float)
    main = Signal(object)                    # run a callable on the Qt main thread (keystrokes, clipboard, UI)
    edited = Signal(object, object, str)     # Selection, edit.Edit, log text: a voice edit on selected text
    candidates_ready = Signal(object, list)  # Selection, candidates for the 選字 menu
    suspects_ready = Signal(object, list)    # Utterance, [suspects.Suspect]
    menu_voice = Signal(int)                 # row picked by voice (-1 = close and go on, -2 = declined)
    record = Signal(dict)         # one transcript log entry for the settings window
    status = Signal(str)
    models_moved = Signal()

    def __init__(self, qapp: QApplication):
        super().__init__()
        self.qapp = qapp
        self.cfg = Config.load()
        models.set_models_dir(self.cfg.models_path())
        self.recorder = Recorder()
        self.recognizer: Recognizer | None = None
        self.second: Recognizer | None = None         # second-opinion model (SenseVoice)
        self.preview: StreamingPreview | None = None  # live captions
        self.recording = False
        self.worker = ThreadPoolExecutor(max_workers=1)   # serializes model loads and transcription
        self.bg = ThreadPoolExecutor(max_workers=1)       # second opinion, aux model loads
        self._status_text = ""
        self._records = deque(maxlen=100)       # transcript log, kept for this session only
        self.hotwords = HotwordStore()
        self.session = Session()
        self._recent = deque(maxlen=10)         # recent dictations (for 選字 second opinions)
        self._probe: ContextProbe | None = None
        self._menu_open = False
        # ("candidates", Selection) | ("span", start, end, word) | ("suspect", Utterance, Suspect)
        self._menu = None
        self._undo = None                       # hotword the last "learned" notification can undo (click)
        self._learned = None                    # last learned hotword ("不要記")
        self._caption_raw = ""
        self._caption = ""

        self.icon_idle = make_icon(QColor(235, 235, 235) if self._dark_taskbar() else QColor(40, 40, 40))
        self.icon_rec = make_icon(QColor(255, 69, 58))

        self.overlay = Overlay(lambda: self.recorder.level, self._caption_text)
        self.picker = Picker()
        self.picker.picked.connect(self.on_menu_pick)
        self.settings = SettingsDialog(self.cfg, self.hotwords)
        self.settings.applied.connect(self.apply_settings)
        self.settings.models_dir_chosen.connect(self.change_models_dir)

        self.tray = QSystemTrayIcon(self.icon_idle)
        menu = QMenu()
        menu.addAction(QAction("設定…", menu, triggered=self.open_settings))
        menu.addSeparator()
        menu.addAction(QAction("結束", menu, triggered=self.quit))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.open_settings() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.messageClicked.connect(self._undo_learned)
        self.tray.show()

        self.pressed.connect(self.on_press)
        self.released.connect(self.on_release)
        self.main.connect(lambda fn: fn())
        self.edited.connect(self.on_edited)
        self.candidates_ready.connect(self.on_candidates)
        self.suspects_ready.connect(self.on_suspects)
        self.menu_voice.connect(self.on_menu_voice)
        self.status.connect(self.on_status)
        self.record.connect(self.on_record)
        self.models_moved.connect(self.load_aux)

        self.ptt = PushToTalk(self.cfg.hotkey, self.pressed.emit, self.released.emit, self.session.on_key)
        self.ptt.start()
        self._mouse = self._watch_mouse()
        self.load_model()
        self.load_aux()
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

    def _watch_mouse(self):
        """Any click may move the caret, which ends what session.py knows about the text before it."""
        if sys.platform != "win32":
            return None
        from pynput import mouse
        downs = {0x201, 0x204, 0x207, 0x20B}   # left / right / middle / x button down

        def on_event(msg, data):
            if msg in downs:
                self.session.on_click(data.pt.x, data.pt.y)
            return False   # never passed on to pynput's (unused) callbacks: keeps the hook cheap
        listener = mouse.Listener(win32_event_filter=on_event)
        listener.daemon = True
        listener.start()
        return listener

    # --- models ---
    def load_model(self):
        self.recognizer = None
        self.worker.submit(self._load_model_job)

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

    def load_aux(self):
        """Load or drop the live-caption and second-opinion models to match the settings."""
        self.bg.submit(self._load_aux_job)

    def _load_aux_job(self):
        for key, wanted, attr, make in (
                (models.STREAM_MODEL, self.cfg.live_caption, "preview",
                 lambda: StreamingPreview(models.model_dir(models.STREAM_MODEL))),
                (models.SECOND_MODEL, self.cfg.second_opinion, "second", lambda: Recognizer(models.SECOND_MODEL))):
            try:
                if not wanted:
                    setattr(self, attr, None)
                    continue
                if getattr(self, attr) is not None:
                    continue
                if not models.is_installed(key):
                    models.download(key)
                t0 = time.monotonic()
                setattr(self, attr, make())
                log.info("%s loaded in %.1fs", key, time.monotonic() - t0)
            except Exception:
                log.exception("%s failed to load", key)

    def change_models_dir(self, path: str):
        """Move the downloaded models to path (empty = default folder), then reload them."""
        new = Path(path) if path else paths.DEFAULT_MODELS_DIR
        if new.resolve() == models.models_dir().resolve():
            return
        self.recognizer = None
        self.second = self.preview = None
        self.worker.submit(self._move_models_job, new)

    def _move_models_job(self, new: Path):
        self.status.emit("搬移模型中…")
        try:
            models.move_models_dir(new)
            self.cfg.models_dir = "" if new == paths.DEFAULT_MODELS_DIR else str(new)
            self.cfg.save()
            log.info("models dir: %s", new)
        except Exception as e:
            log.exception("moving models failed")
            self.status.emit(f"搬移模型失敗：{e}")
        self._load_model_job()
        self.models_moved.emit()

    # --- push-to-talk ---
    def _caption_text(self) -> str:
        if not self.recording or self.preview is None:
            return ""
        raw = self.preview.text
        if raw != self._caption_raw:
            self._caption_raw, self._caption = raw, to_traditional(raw)
        return self._caption

    def on_press(self):
        if self.recognizer is None:
            self.tray.showMessage("VoiceInput", self._status_text or "模型尚未就緒", self.icon_idle, 2000)
            return
        preview = self.preview if self.cfg.live_caption else None
        self.recorder.listener = preview.feed if preview else None
        try:
            self.recorder.start(self.cfg.mic)
        except Exception as e:
            log.exception("mic start failed")
            self.tray.showMessage("VoiceInput", f"無法開啟麥克風：{e}", self.icon_idle, 3000)
            return
        if preview:
            preview.start()
        self.recording = True
        self._caption_raw = self._caption = ""
        self._probe = ContextProbe(self.cfg.edit_enabled, self.cfg.screen_terms)
        self._menu_open = self.picker.isVisible()
        self._hwnd = foreground()
        self.tray.setIcon(self.icon_rec)
        self.overlay.show_recording()

    def on_release(self, held: float):
        if not self.recording:
            return
        audio = self.recorder.stop()
        if self.preview is not None:
            self.preview.stop()
        self.recording = False
        self.tray.setIcon(self.icon_idle)
        if held < TAP_THRESHOLD:
            self.overlay.hide_overlay()
            return
        self.overlay.show_thinking()
        self.worker.submit(self._transcribe_job, self.recognizer, audio, self._probe, self._menu_open, self._hwnd)

    def _bias(self, ctx: Context) -> list[tuple[str, float]]:
        return self.hotwords.bias_words() + [(t, _SCREEN_TERM_SCORE) for t in ctx.terms]

    def _transcribe_job(self, rec, audio, probe, menu_open, hwnd):
        stamp = time.strftime("%H:%M:%S")
        try:
            ctx = probe.result() if probe else Context()
            t0 = time.monotonic()
            tr = rec.recognize(audio, self._bias(ctx))
            t_asr = time.monotonic() - t0
            raw = tr.text
            log.info("asr %.2fs: %s%s", t_asr, raw, f" (terms: {len(ctx.terms)})" if ctx.terms else "")
            entry = {"time": stamp, "asr": to_traditional(raw), "asr_s": t_asr}
            text, asks = self.hotwords.apply(format_text(raw, self.cfg.strip_trailing_punct))
            entry["fmt"] = text
            if menu_open:
                row = edit.parse_pick(text)
                self.menu_voice.emit(row)
                if row >= 0 or row == -2:
                    entry["edit"] = f"選第 {row + 1} 個" if row >= 0 else "不用"
                    self.record.emit(entry)
                    self.main.emit(self.overlay.hide_overlay)
                    return
            sel = ctx.selection
            if sel is not None:
                self._edit_job(sel, raw, text, entry, hwnd)
                return
            cmd = commands.parse(raw, text, tr) if self.cfg.voice_commands else commands.Command(commands.DICTATE, text)
            if cmd.kind != commands.DICTATE:
                if self._command_job(cmd, entry, hwnd):
                    return
                cmd = commands.dictation(raw, text, tr)
            if cmd.send:
                entry["edit"] = "貼上後送出（Enter）"
            self.record.emit(entry)
            asks = [a for a in asks if a.end <= len(cmd.text)]   # a trailing 送出 was cut off
            self.main.emit(lambda: self._dictate(cmd.text, cmd.send, tr, audio, hwnd, asks))
        except Exception:
            log.exception("transcribe failed")
            self.main.emit(self.overlay.hide_overlay)

    # --- dictation ---
    def _dictate(self, text: str, send: bool, tr, audio, hwnd: int, asks=()):
        """Main thread: paste a dictation (and press Enter), learn from a re-dictation, offer undecided context
        hotwords, start the second opinion."""
        self.overlay.hide_overlay()
        log.info("result: %s%s", text, " [送出]" if send else "")
        if text:
            prev = self.session.redictation_of()
            paste_text(text)
            u = self.session.dictated(text, tr, audio, foreground() or hwnd)
            self._recent.append(u)
            if prev is not None:
                pair = redictation_pair(prev.text, text)
                log.info("re-dictation of %r: %s", prev.text, pair)
                if pair and worth_learning(*pair, False):
                    i = text.find(pair[1])
                    self._learn(*pair, False, source="重講",
                                context=context_words(text, i, i + len(pair[1])) if i >= 0 else None)
            if asks and not send:
                a = asks[0]
                self.on_suspects(u, [suspects.Suspect(a.start, a.end, text[a.start:a.end], [a.hotword.value],
                                                      a.hotword, a.words)])
            elif self.cfg.second_opinion and not send and audio is not None:
                self.bg.submit(self._suspect_job, u)
        if send:
            # after the paste has been read (Windows Terminal reads the clipboard asynchronously)
            QTimer.singleShot(150 if text else 0, press_enter)
            self.session.sent()

    def _second_text(self, u) -> str | None:
        """The second model's formatted transcript of a dictation (cached on it)."""
        if u.second is None and self.second is not None and u.audio is not None:
            t0 = time.monotonic()
            u.second = format_text(self.second.transcribe(u.audio), self.cfg.strip_trailing_punct)
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
            title = f"「{sus.word}」要換成「{sus.options[0]}」嗎？說「對」換掉，「不用」保留（會記住這個語境）"
        else:
            title = f"可能聽錯「{sus.word}」：說「對」換成 1，或說第幾個"
        self.picker.show_list(title, sus.options, caret_rect(), timeout_ms=15_000)
        s.ignore_rects = [self.picker.physical_rect()]

    # --- voice commands without a selection ---
    def _command_job(self, cmd, entry, hwnd) -> bool:
        """Worker thread. True if the command was handled; False = dictate the utterance instead."""
        s, k = self.session, cmd.kind
        note = None
        if k == commands.UNDO:
            self.main.emit(self._undo_step)
            note = "復原"
        elif k == commands.FORGET:
            self.main.emit(self._forget)
            note = "不要記"
        elif k == commands.SEND:
            self.main.emit(lambda: (self.overlay.hide_overlay(), press_enter(), self.session.sent()))
            note = "送出（Enter）"
        elif k == commands.NEWLINE:
            def newline():
                self.overlay.hide_overlay()
                press_newline()
                if s.clean():
                    s.apply_replace(len(s.buffer), len(s.buffer), "\n", "換行")
            self.main.emit(newline)
            note = "換行（Shift+Enter）"
        elif k == commands.SYMBOL:
            if s.clean() and s.buffer[-1] in _PUNCT_END:
                self._rewrite_later(len(s.buffer) - 1, len(s.buffer), cmd.text, f"句尾改成{cmd.text}")
            elif s.clean():
                self._rewrite_later(len(s.buffer), len(s.buffer), cmd.text, f"加上{cmd.text}")
            else:
                self.main.emit(lambda: self._dictate(cmd.text, False, None, None, hwnd))
            note = f"標點 {cmd.text}"
        elif k in (commands.DELETE_LAST, commands.DELETE_ALL):
            if not s.clean():
                note = "游標已經移動過，不知道剛剛輸入的內容在哪裡"
                self.main.emit(lambda n=note: self._notify(n))
            elif k == commands.DELETE_LAST and not s.utterances:
                note = "沒有剛剛輸入的句子"
                self.main.emit(lambda n=note: self._notify(n))
            elif k == commands.DELETE_LAST:
                u = s.utterances[-1]
                self._rewrite_later(u.start, u.end, "", f"刪掉「{u.text}」")
                note = f"刪掉上一句「{u.text}」"
            else:
                self._rewrite_later(0, len(s.buffer), "", "全部刪掉")
                note = "全部刪掉"
        elif k in (commands.REPLACE, commands.DELETE_TEXT):
            if not s.clean():
                return False
            span = commands.locate(s.buffer, cmd.target, avoid=cmd.text)
            if span is None:
                log.info("command target %r not in %r; dictating", cmd.target, s.buffer[-60:])
                return False
            a, b = span
            old = s.buffer[a:b]
            if k == commands.DELETE_TEXT:
                self._rewrite_later(a, b, "", f"刪掉「{old}」")
                note = f"刪掉「{old}」"
            else:
                new, spelled = self._replacement(old, cmd)
                if new is None:
                    # the ASR wrote the new word like the old one (城市改成城市): let the user pick
                    words = self._alternatives(old)
                    self.main.emit(lambda: self._open_span_menu(a, b, old, words))
                    note = f"選字「{old}」"
                else:
                    self._rewrite_later(a, b, new, f"{old} → {new}", learn=(old, new, spelled))
                    note = f"{old} → {new}"
        entry["edit"] = f"指令：{note}"
        log.info("command %s: %s", k, note)
        self.record.emit(entry)
        return True

    def _replacement(self, old: str, cmd) -> tuple[str | None, bool]:
        """What 改成Y means for old: a described character (教育的育), spelled letters (match old's case), or Y.
        None when Y is old itself (the ASR wrote the new word the same way)."""
        ch = edit.described_char(cmd.text)
        if ch:
            new = edit.replace_char(old, ch)
            return (new if new != old else None), True
        if cmd.spelled:
            letters = "".join(c for c in cmd.text if c.isalpha())
            return edit.match_case(letters, old), True
        if cmd.text == old:
            return None, False
        return cmd.text, False

    def _open_span_menu(self, a: int, b: int, word: str, words: list[str]):
        self.overlay.hide_overlay()
        if not words:
            self._notify(f"「{word}」沒有同音候選")
            return
        self._menu = ("span", a, b, word)
        self.picker.show_list(f"選字：{word}　點選或說「第幾個」", words, caret_rect())
        self.session.ignore_rects = [self.picker.physical_rect()]

    def _rewrite_later(self, a: int, b: int, new: str, note: str, learn=None):
        expected = self.session.buffer
        self.main.emit(lambda: self._rewrite(a, b, new, note, learn, expected))

    def _rewrite(self, a: int, b: int, new: str, note: str, learn=None, expected=None):
        """Main thread: replace buffer[a:b] with new by Backspace + paste."""
        self.overlay.hide_overlay()
        s = self.session
        if not s.clean() or (expected is not None and s.buffer != expected):
            self._notify("游標已經移動過，沒有修改")
            return
        n, tail = s.plan_replace(a, b, new)
        log.info("rewrite: %d backspaces + %r (%s)", n, tail, note)
        backspace(n)
        paste_text(tail)
        h = None
        if learn:
            old, new_word = _with_neighbours(s.buffer[:a], learn[0], learn[1], s.buffer[b:])
            h = self._learn(old, new_word, learn[2], context=context_words(s.buffer, a, b))
        s.apply_replace(a, b, new, note, h)

    def _undo_step(self):
        self.overlay.hide_overlay()
        s = self.session
        step = s.pop_undo()
        if step is None:
            self._notify("沒有可以復原的（游標移動過，或還沒有輸入）")
            return
        n, tail = s.plan_to(step.before)
        log.info("undo %s: %d backspaces + %r", step.note, n, tail)
        backspace(n)
        paste_text(tail)
        s.undone(step)
        if step.hotword is not None:
            self._forget_hotword(step.hotword)
        self._notify(f"已復原：{step.note}")

    def _forget(self):
        self.overlay.hide_overlay()
        if self._learned is None:
            self._notify("沒有剛記住的熱詞")
            return
        self._forget_hotword(self._learned)
        self._notify(f"已取消熱詞：{self._learned.key} → {self._learned.value}")
        self._learned = None

    def _forget_hotword(self, h):
        log.info("hotword undone: %s -> %s", h.key, h.value)
        self.hotwords.remove(h)
        if self._undo is h:
            self._undo = None
        self.settings.refresh_hotwords()

    def _notify(self, text: str):
        self.tray.showMessage("VoiceInput", text, self.icon_idle, 2500)

    # --- voice edits on a selection ---
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

    def _edit_job(self, sel, raw: str, text: str, entry: dict, hwnd: int):
        """Worker thread: turn the utterance into an edit of the selection."""
        cmd = commands.parse(raw, text) if self.cfg.voice_commands else None
        if cmd is not None and cmd.kind in (commands.UNDO, commands.FORGET, commands.SEND, commands.NEWLINE):
            if self._command_job(cmd, entry, hwnd):
                return
        ed = edit.plan(sel.text, raw, text)
        if ed.action == edit.REPICK:
            ed = edit.Edit(edit.CANDIDATES)   # saying the word again: show the other candidates
        note = _describe(sel.text, ed)
        entry["edit"] = note
        log.info("edit on %s: %s", sel.app, note)
        self.record.emit(entry)
        self.edited.emit(sel, ed, note)
        if ed.action == edit.CANDIDATES:
            try:
                words = self._alternatives(sel.text)
            except Exception:
                log.exception("candidates failed")
                words = candidates.homophones(sel.text)
            self.candidates_ready.emit(sel, words)

    def on_edited(self, sel, ed, note: str):
        self.overlay.hide_overlay()
        self._undo = None   # a click on any newer notification must not undo an older hotword
        if ed.action == edit.DELETE:
            press_delete()
            self.session.replaced_selection(sel.before, sel.text, "", foreground(), f"刪除「{sel.text}」")
        elif ed.action == edit.REPLACE:
            paste_text(ed.text)
            h = self._learn(*_with_neighbours(sel.before, sel.text, ed.text, sel.after), ed.spelled,
                            context=_selection_context(sel))
            self.session.replaced_selection(sel.before, sel.text, ed.text, foreground(), note, h)
        elif ed.action == edit.CANDIDATES:
            self._menu = ("candidates", sel)
            self.picker.show_loading(sel.text, sel.rect)
            self.session.ignore_rects = [self.picker.physical_rect()]
        else:
            self._notify(note)

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
        if menu[0] == "candidates":
            sel = menu[1]
            log.info("menu pick: %s -> %s", sel.text, word)
            paste_text(word)
            h = self._learn(*_with_neighbours(sel.before, sel.text, word, sel.after), False,
                            context=_selection_context(sel))
            self.session.replaced_selection(sel.before, sel.text, word, foreground(), f"{sel.text} → {word}", h)
        elif menu[0] == "span":
            _, a, b, old = menu
            if self.session.buffer[a:b] != old:
                self._notify("文字已經改變，沒有修改")
                return
            log.info("span pick: %s -> %s", old, word)
            self._rewrite(a, b, word, f"{old} → {word}", learn=(old, word, False))
        else:
            _, u, sus = menu
            s = self.session
            a, b = u.start + sus.start, u.start + sus.end
            if s.buffer[a:b] != sus.word:
                span = commands.locate(s.buffer, sus.word)
                if span is None:
                    self._notify("文字已經改變，沒有修改")
                    return
                a, b = span
            log.info("suspect pick: %s -> %s", sus.word, word)
            if sus.hotword is not None:
                sus.hotword.remember(sus.words, True)
                sus.hotword.hits += 1
                self.hotwords.save()
                self._rewrite(a, b, word, f"{sus.word} → {word}")
            else:
                self._rewrite(a, b, word, f"{sus.word} → {word}", learn=(sus.word, word, False))

    def on_menu_voice(self, row: int):
        # a number / 對 picks a row, 不用 declines; anything else closes the menu and is handled normally
        menu = self._menu
        self.picker.hide()
        if row >= 0:
            self.on_menu_pick(row)
            return
        self._menu = None
        self.session.ignore_rects = []
        if row == -2 and menu and menu[0] == "suspect" and menu[2].hotword is not None:
            sus = menu[2]
            log.info("hotword %s declined near %s", sus.hotword.key, sus.words)
            sus.hotword.remember(sus.words, False)
            self.hotwords.save()

    def _learn(self, old: str, new: str, spelled: bool, source: str = "", context=None):
        """Record old -> new as a hotword if the edit looks like a correction of a misrecognition."""
        if not worth_learning(old, new, spelled):
            return None
        known = self.hotwords.find(old.strip())
        if known is not None and known.value == new.strip():
            if context:
                known.remember(context, True)   # one more sentence where it was right
                self.hotwords.save()
            return None   # already known: nothing new to announce or to undo
        if known is not None:
            # 城市 -> 乘勢 picked once must not overwrite 城市 -> 程式; this sentence just was not a 程式 one
            log.info("not learning %s -> %s: hotword %s -> %s exists", old, new, known.key, known.value)
            if context and known.mode != "always":
                known.remember(context, False)
                self.hotwords.save()
            return None
        h = self.hotwords.add(old.strip(), new.strip(), context=context)
        log.info("learned hotword%s: %s -> %s near %s", f" ({source})" if source else "", h.key, h.value, context)
        self._undo = self._learned = h
        self.settings.refresh_hotwords()
        self.tray.showMessage("已記住熱詞", f"{h.key} → {h.value}（說「不要記」或點這裡取消）", self.icon_idle, 4000)
        return h

    def _undo_learned(self):
        if self._undo is not None:
            self._forget_hotword(self._undo)
            self._learned = None

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
        self._set_autostart()
        self.load_aux()

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


def _selection_context(sel) -> list[str]:
    line = sel.before + sel.text + sel.after
    return context_words(line, len(sel.before), len(sel.before) + len(sel.text))


def _describe(selected: str, ed) -> str:
    if ed.action == edit.DELETE:
        return f"刪除「{selected}」"
    if ed.action == edit.REPLACE:
        return f"{selected} → {ed.text}" + ("（拼字）" if ed.spelled else "")
    if ed.action == edit.CANDIDATES:
        return f"選字「{selected}」"
    return f"「{selected}」不變"


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
