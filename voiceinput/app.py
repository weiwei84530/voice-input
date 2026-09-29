"""VoiceInput: single-process tray app. Hold the hotkey to talk, release to paste.

What an utterance does, in order:
1. A suggestion menu is open (選字 or 可能聽錯): 第二個 / 對 picks, 不用 closes, anything else closes it and goes on.
2. Text is selected (UI Automation): the utterance edits the selection (edit.py).
3. A voice command (commands.py): 復原, 送出, 換行, 刪掉上一句, 城市改成程式 … on what was just dictated
   (session.py), without selecting anything.
4. Otherwise it is dictated: formatted, optional LLM rules, pasted; a trailing 送出 after a pause presses Enter.
   In the background a second model re-checks it and a likely misheard word is offered in a small menu.
"""
import logging
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QLockFile, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import autostart, candidates, commands, edit, llm, models, paths, suspects
from .asr import Recognizer, StreamingPreview
from .audio import Recorder
from .config import Config
from .hotkey import TAP_THRESHOLD, PushToTalk
from .hotwords import HotwordStore, worth_learning
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
    candidates_ready = Signal(object, list)  # Selection, ranked candidates for the 選字 menu
    suspects_ready = Signal(object, list)    # Utterance, [suspects.Suspect]
    menu_voice = Signal(int)                 # row picked by voice (-1 = close and go on, -2 = just close)
    rewriting = Signal()
    record = Signal(dict)         # one transcript log entry for the settings window
    status = Signal(str)
    llm_status = Signal(str, str)  # state (off / loading / ready / error), text
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
        self.bg = ThreadPoolExecutor(max_workers=1)       # second opinion, suspects, aux model loads
        self._status_text = ""
        self._llm_status_text = ""
        self._llm_state = ("off", "")
        self._records = deque(maxlen=100)       # transcript log, kept for this session only
        self.llm: llm.LlmServer | None = None
        self._llm_wanted = None
        self._llm_gen = 0                       # bumps on every enable/disable; stale loads discard themselves
        self._llm_lock = threading.Lock()       # serializes LLM downloads / server starts
        self.hotwords = HotwordStore()
        self.session = Session()
        self._recent = deque(maxlen=10)         # recent dictations (for 選字 second opinions)
        self._probe: ContextProbe | None = None
        self._menu_open = False
        self._menu = None                       # ("candidates", Selection) or ("suspect", Utterance, Suspect)
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
        self.rewriting.connect(self.overlay.show_rewriting)
        self.status.connect(self.on_status)
        self.llm_status.connect(self.on_llm_status)
        self.record.connect(self.on_record)
        self.models_moved.connect(self.load_llm)

        self.ptt = PushToTalk(self.cfg.hotkey, self.pressed.emit, self.released.emit, self.session.on_key)
        self.ptt.start()
        self._mouse = self._watch_mouse()
        self.load_model(self.cfg.model if self.cfg.model in models.MODELS else models.DEFAULT_MODEL)
        self.load_llm()
        self.load_aux()
        candidates.preload()   # 選字, suspects and the disfluency pass use the dictionary
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
    def load_model(self, key: str):
        self._model_key = key
        self.recognizer = None
        self.worker.submit(self._load_model_job, key)

    def _load_model_job(self, key: str):
        try:
            if not models.is_installed(key):
                def progress(done, total):
                    pct = f"{done * 100 // total}%" if total else f"{done >> 20} MB"
                    self.status.emit(f"下載模型中… {pct}")
                models.download(key, progress)
            self.status.emit("載入模型中…")
            rec = Recognizer(key)
            if key == self.cfg.model:   # ignore a load the user already switched away from
                self.recognizer = rec
                self.status.emit(f"就緒：{models.MODELS[key]['short']}")
        except Exception as e:
            log.exception("model load failed")
            self.status.emit(f"模型載入失敗：{e}")

    def _second_key(self) -> str:
        # SenseVoice, not Qwen3-ASR: as a disagreement signal (filtered by the LLM) it is enough, and it takes
        # 0.1s and ~200MB instead of ~1s and ~1.2GB (tested 2026-09-29; the app reached 1.8GB with Qwen3 on an
        # 8GB machine that also runs the 2B LLM)
        return "sensevoice" if self._model_key != "sensevoice" else "xasr"

    def load_aux(self):
        """Load or drop the live-caption and second-opinion models to match the settings."""
        self.bg.submit(self._load_aux_job)

    def _load_aux_job(self):
        try:
            if self.cfg.live_caption and self.preview is None:
                if not models.is_installed(models.STREAM_MODEL):
                    models.download(models.STREAM_MODEL)
                t0 = time.monotonic()
                self.preview = StreamingPreview(models.model_dir(models.STREAM_MODEL))
                log.info("live caption model loaded in %.1fs", time.monotonic() - t0)
            elif not self.cfg.live_caption:
                self.preview = None
        except Exception:
            log.exception("live caption model failed")
        try:
            key = self._second_key()
            if self.cfg.second_opinion and (self.second is None or self.second.key != key):
                if not models.is_installed(key):
                    log.info("second opinion: %s not installed, skipped", key)
                    self.second = None
                    return
                t0 = time.monotonic()
                self.second = Recognizer(key)
                log.info("second opinion model %s loaded in %.1fs", key, time.monotonic() - t0)
            elif not self.cfg.second_opinion:
                self.second = None
        except Exception:
            log.exception("second opinion model failed")

    def change_models_dir(self, path: str):
        """Move the downloaded models to path (empty = default folder), then reload the ASR model and LLM."""
        new = Path(path) if path else paths.DEFAULT_MODELS_DIR
        if new.resolve() == models.models_dir().resolve():
            return
        self.recognizer = None
        self.second = self.preview = None
        # Stop the LLM (its GGUF is held open) and keep it stopped until the move is done
        self._llm_wanted = None
        self._llm_gen += 1
        old_llm, self.llm = self.llm, None
        self.llm_status.emit("off", "")
        self.worker.submit(self._move_models_job, new, old_llm)

    def _move_models_job(self, new: Path, old_llm):
        with self._llm_lock:
            if old_llm:
                old_llm.close()
            self.status.emit("搬移模型中…")
            try:
                models.move_models_dir(new)
                self.cfg.models_dir = "" if new == paths.DEFAULT_MODELS_DIR else str(new)
                self.cfg.save()
                log.info("models dir: %s", new)
            except Exception as e:
                log.exception("moving models failed")
                self.status.emit(f"搬移模型失敗：{e}")
        self._load_model_job(self._model_key)
        self.models_moved.emit()
        self.load_aux()

    def load_llm(self):
        """Start or stop the LLM server: it runs while custom rules, voice edits or the second opinion are on
        (candidate ranking, context hotwords, suspects and spoken edit instructions need it)."""
        wanted = self.cfg.llm_enabled or self.cfg.edit_enabled or self.cfg.second_opinion
        if wanted == self._llm_wanted:
            return
        self._llm_wanted = wanted
        self._llm_gen += 1
        old, self.llm = self.llm, None
        threading.Thread(target=self._load_llm_job, args=(self._llm_gen, wanted, old), daemon=True).start()

    def _load_llm_job(self, gen, wanted, old):
        with self._llm_lock:
            if old:
                old.close()
            if gen != self._llm_gen:
                return
            if not wanted:
                self.llm_status.emit("off", "")
                return
            try:
                if not llm.is_installed():
                    def progress(done, total):
                        pct = f"{done * 100 // total}%" if total else f"{done >> 20} MB"
                        self.llm_status.emit("loading", f"模型下載中… {pct}")
                    llm.download(progress)
                self.llm_status.emit("loading", "模型載入中…")
                t0 = time.monotonic()
                server = llm.LlmServer(self.cfg.llm_user_rules if self.cfg.llm_enabled else "")
                log.info("llm loaded in %.1fs", time.monotonic() - t0)
                if gen != self._llm_gen:
                    server.close()
                    return
                self.llm = server
                self.llm_status.emit("ready", "已就緒")
            except Exception as e:
                log.exception("llm load failed")
                self.llm_status.emit("error", f"載入失敗：{e}")

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
        want_terms = self.cfg.screen_terms and self.recognizer.supports_hotwords
        self._probe = ContextProbe(self.cfg.edit_enabled, want_terms)
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
            tr = rec.recognize(audio, self._bias(ctx) if rec.supports_hotwords else None)
            t_asr = time.monotonic() - t0
            text = tr.text
            log.info("asr %.2fs: %s%s", t_asr, text, f" (terms: {len(ctx.terms)})" if ctx.terms else "")
            entry = {"time": stamp, "asr": to_traditional(text), "asr_s": t_asr, "llm": "", "llm_s": None}
            # Format before the LLM so it sees Traditional Chinese, joined letters and digits, matching how the
            # user writes rules. No formatting after it: that would override rules such as "add a trailing period".
            raw = text
            text = self.hotwords.apply(format_text(text, self.cfg.strip_trailing_punct), self._judge)
            entry["fmt"] = text
            if menu_open:
                row = edit.parse_pick(text)
                self.menu_voice.emit(row)
                if row >= 0 or row == -2:
                    entry.update(llm=None, edit=f"選第 {row + 1} 個" if row >= 0 else "關閉選單")
                    self.record.emit(entry)
                    self.main.emit(self.overlay.hide_overlay)
                    return
            sel = ctx.selection
            if sel is not None:
                entry["llm"] = None
                self._edit_job(sel, raw, text, entry, hwnd)
                return
            cmd = commands.parse(raw, text, tr) if self.cfg.voice_commands else commands.Command(commands.DICTATE, text)
            if cmd.kind != commands.DICTATE:
                if self._command_job(cmd, entry, hwnd):
                    return
                cmd = commands.dictation(raw, text, tr)
            text = cmd.text
            server, rules = self.llm, self.cfg.llm_user_rules.strip()
            if not self.cfg.llm_enabled:
                entry["llm"] = None  # hidden in the log
            elif not rules:
                entry["llm"] = "（規則空白，略過）"
            elif server is None:
                entry["llm"] = "（模型尚未就緒，略過）"
            elif not text:
                entry["llm"] = "（沒有文字）"
            else:
                self.rewriting.emit()
                t1 = time.monotonic()
                try:
                    text = server.rewrite(text, rules)
                    t_llm = time.monotonic() - t1
                    log.info("llm %.2fs: %s", t_llm, text)
                    entry.update(llm=text, llm_s=t_llm)
                except Exception as e:
                    log.exception("llm rewrite failed; using ASR text")
                    entry["llm"] = f"（失敗：{e}）"
            if cmd.send:
                entry["edit"] = "貼上後送出（Enter）"
            self.record.emit(entry)
            self.main.emit(lambda: self._dictate(text, cmd.send, tr, audio, hwnd))
        except Exception:
            log.exception("transcribe failed")
            self.main.emit(self.overlay.hide_overlay)

    def _judge(self, text: str, start: int, end: int, value: str) -> bool:
        """Context hotwords: replace only if the LLM prefers value over the recognised word in this sentence."""
        server = self.llm
        if server is None:
            return False
        self.rewriting.emit()   # any LLM work shows the ring, not the ASR dots
        ranked = server.rank(text[:start], text[end:], [text[start:end], value])
        log.info("hotword judge %s|%s: %s", text[start:end], value, [(c, round(p, 2)) for p, c in ranked])
        return ranked[0][1] == value

    # --- dictation ---
    def _dictate(self, text: str, send: bool, tr, audio, hwnd: int):
        """Main thread: paste a dictation (and press Enter), learn from a re-dictation, start the re-check."""
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
                    self._learn(*pair, False, source="重講")
            if self.cfg.second_opinion and not send and audio is not None:
                self.bg.submit(self._suspect_job, u)
        if send:
            # after the paste has been read (Windows Terminal reads the clipboard asynchronously)
            QTimer.singleShot(150 if text else 0, press_enter)
            self.session.sent()

    def _suspect_job(self, u):
        try:
            other = None
            if self.second is not None and u.audio is not None:
                t0 = time.monotonic()
                other = format_text(self.second.transcribe(u.audio), self.cfg.strip_trailing_punct)
                u.second = other
                log.info("second opinion %.2fs: %s", time.monotonic() - t0, other)
            server = self.llm
            found = suspects.find(u.text, other, server.rank if server else None)
            if found:
                self.suspects_ready.emit(u, found)
        except Exception:
            log.exception("suspect check failed")

    def on_suspects(self, u, found: list):
        s = self.session
        if self.recording or self.picker.isVisible() or u not in s.utterances or not s.clean():
            return
        sus = found[0]
        log.info("offer: %s -> %s", sus.word, sus.options)
        self._menu = ("suspect", u, sus)
        self.picker.show_list(f"可能聽錯「{sus.word}」：說「對」換成 1，或說第幾個", sus.options, caret_rect(),
                              timeout_ms=15_000)
        s.ignore_rects = [self.picker.physical_rect()]

    # --- voice commands without a selection ---
    def _command_job(self, cmd, entry, hwnd) -> bool:
        """Worker thread. True if the command was handled; False = dictate the utterance instead."""
        s, k = self.session, cmd.kind
        entry["llm"] = None
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
                    new = self._best_alternative(s.buffer[:a], old, s.buffer[b:])
                    if new is None:
                        note = f"「{old}」沒有其他候選"
                        self.main.emit(lambda n=note: self._notify(n))
                        entry["edit"] = note
                        self.record.emit(entry)
                        return True
                self._rewrite_later(a, b, new, f"{old} → {new}", learn=(old, new, spelled))
                note = f"{old} → {new}"
        entry["edit"] = f"指令：{note}"
        log.info("command %s: %s", k, note)
        self.record.emit(entry)
        return True

    def _replacement(self, old: str, cmd) -> tuple[str | None, bool]:
        """What 改成Y means for old: a described character (教育的育), spelled letters (match old's case), or Y.
        None when Y is old itself (the ASR wrote the new word the same way): pick the best other candidate."""
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

    def _best_alternative(self, before: str, word: str, after: str) -> str | None:
        words = candidates.homophones(word)
        if not words:
            return None
        server = self.llm
        if server is None:
            return words[0]
        self.rewriting.emit()
        ranked = server.rank(before, after, [word] + words)
        return next((c for _, c in ranked if c != word), None)

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
            h = self._learn(old, new_word, learn[2])
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
    def _alternatives(self, sel) -> list[str]:
        """What else the selection could be: the second model's version of it (when it came from a recent
        dictation), then same-sounding words."""
        alts = []
        for u in reversed(self._recent):
            i = u.text.rfind(sel.text)
            if i < 0:
                continue
            if u.second is None and self.second is not None and u.audio is not None:
                u.second = format_text(self.second.transcribe(u.audio), self.cfg.strip_trailing_punct)
            for s, e, alt in suspects.disagreements(u.text, u.second or ""):
                if i <= s and e <= i + len(sel.text):
                    word = sel.text[:s - i] + alt + sel.text[e - i:]
                    if word not in alts:
                        alts.append(word)
            break
        return alts + [w for w in candidates.homophones(sel.text) if w not in alts]

    def _ranked(self, sel) -> list[str]:
        """Candidates for the selection, best fit for its line first (frequency order without the LLM)."""
        words = self._alternatives(sel)
        server = self.llm
        if server is None or not words:
            return words
        self.rewriting.emit()
        t0 = time.monotonic()
        ranked = server.rank(sel.before, sel.after, [sel.text] + words)
        log.info("rank %.2fs %s: %s", time.monotonic() - t0, sel.text, [(c, round(p, 2)) for p, c in ranked])
        return [c for _, c in ranked if c != sel.text]

    def _edit_job(self, sel, raw: str, text: str, entry: dict, hwnd: int):
        """Worker thread: turn the utterance into an edit of the selection, using the LLM where needed."""
        cmd = commands.parse(raw, text) if self.cfg.voice_commands else None
        if cmd is not None and cmd.kind in (commands.UNDO, commands.FORGET, commands.SEND, commands.NEWLINE):
            if self._command_job(cmd, entry, hwnd):
                return
        ed = edit.plan(sel.text, raw, text)
        note = _describe(sel.text, ed)
        try:
            if ed.action == edit.REPICK:
                words = self._ranked(sel)
                if words and self.llm is not None:
                    ed = edit.Edit(edit.REPLACE, words[0])
                    note = f"{sel.text} → {words[0]}（自動挑選）"
                elif words:
                    ed = edit.Edit(edit.CANDIDATES)
                    note = f"選字「{sel.text}」（LLM 未就緒，改開選單）"
                else:
                    note = f"「{sel.text}」沒有同音候選"
            elif ed.action == edit.INSTRUCT:
                if self.llm is None:
                    note = f"「{ed.text}」需要 LLM，但尚未就緒"
                    ed = edit.Edit(edit.REPICK)
                else:
                    self.rewriting.emit()
                    t0 = time.monotonic()
                    out = self.llm.instruct(sel.before, sel.text, sel.after, ed.text)
                    entry.update(llm=out, llm_s=time.monotonic() - t0)
                    note = f"{sel.text} → {out}（指令：{ed.text}）"
                    ed = edit.Edit(edit.REPLACE, out) if out != sel.text else edit.Edit(edit.REPICK)
        except Exception as e:
            log.exception("edit failed")
            note = f"失敗：{e}"
            ed = edit.Edit(edit.REPICK)
        entry["edit"] = note
        log.info("edit on %s: %s", sel.app, note)
        self.record.emit(entry)
        self.edited.emit(sel, ed, note)
        if ed.action == edit.CANDIDATES:
            try:
                words = self._ranked(sel)
            except Exception:
                log.exception("ranking candidates failed")
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
            h = self._learn(*_with_neighbours(sel.before, sel.text, ed.text, sel.after), ed.spelled)
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
            h = self._learn(sel.text, word, False)
            self.session.replaced_selection(sel.before, sel.text, word, foreground(), f"{sel.text} → {word}", h)
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
            self._rewrite(a, b, word, f"{sus.word} → {word}", learn=(sus.word, word, False))

    def on_menu_voice(self, row: int):
        # a number / 對 picks a row, 不用 closes; anything else closes the menu and is handled normally
        self.picker.hide()
        if row >= 0:
            self.on_menu_pick(row)
        else:
            self._menu = None
            self.session.ignore_rects = []

    def _learn(self, old: str, new: str, spelled: bool, source: str = ""):
        """Record old -> new as a hotword if the edit looks like a correction of a misrecognition."""
        if not worth_learning(old, new, spelled):
            return None
        known = self.hotwords.find(old.strip())
        if known is not None and known.value == new.strip():
            return None   # already known: nothing new to announce or to undo
        h = self.hotwords.add(old.strip(), new.strip())
        log.info("learned hotword%s: %s -> %s", f" ({source})" if source else "", h.key, h.value)
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
        self._show_status()

    def on_llm_status(self, state: str, text: str):
        self._llm_state = (state, text)
        self._llm_status_text = f"LLM：{text}" if text else ""
        log.info("llm status: %s %s", state, text)
        self.settings.set_llm_state(state, text)
        self._show_status()

    def on_record(self, entry: dict):
        self._records.append(entry)
        self.settings.set_log(self._records)

    def _status_line(self) -> str:
        return "　".join(t for t in (self._status_text, self._llm_status_text) if t)

    def _show_status(self):
        self.tray.setToolTip(f"VoiceInput — {self._status_line()}")
        self.settings.set_status(self._status_line())

    def open_settings(self):
        self.settings.load_values()
        self.settings.set_llm_state(*self._llm_state)
        self.settings.set_log(self._records)
        self.settings.set_status(self._status_line())
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def apply_settings(self):
        self.ptt.set_key(self.cfg.hotkey)
        self._set_autostart()
        if self.cfg.model != self._model_key:
            self.load_model(self.cfg.model)
        self.load_llm()
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
        if self.llm:
            self.llm.close()
        self.qapp.quit()


def _with_neighbours(before: str, old: str, new: str, after: str) -> tuple[str, str]:
    """A one-character correction (張雨薇: 雨 -> 育) is learned with its neighbouring characters (張雨薇 -> 張育薇):
    a single-character hotword would change every 雨."""
    if len(old.strip()) != 1 or not _CJK_CHAR.match(old.strip()):
        return old, new
    left = before[-1:] if before[-1:] and _CJK_CHAR.match(before[-1:]) else ""
    right = after[:1] if after[:1] and _CJK_CHAR.match(after[:1]) else ""
    return left + old.strip() + right, left + new.strip() + right


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
