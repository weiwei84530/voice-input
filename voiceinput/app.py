"""VoiceInput: single-process tray app. Hold the hotkey to talk, release to paste."""
import logging
import sys
import threading
import time
from collections import deque
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QLockFile, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import autostart, edit, llm, models, paths
from .asr import Recognizer
from .audio import Recorder
from .config import Config
from .hotkey import TAP_THRESHOLD, PushToTalk
from .hotwords import HotwordStore, worth_learning
from .output import paste_text, press_delete
from .overlay import Overlay
from .selection import SelectionProbe
from .settings import SettingsDialog
from .textfmt import format_text, to_traditional

log = logging.getLogger("voiceinput")


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
    transcribed = Signal(str)
    edited = Signal(object, object)   # Selection, edit.Edit: a voice edit on selected text
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
        self.recording = False
        self.worker = ThreadPoolExecutor(max_workers=1)   # serializes model loads and transcription
        self._status_text = ""
        self._llm_status_text = ""
        self._llm_state = ("off", "")
        self._records = deque(maxlen=100)       # transcript log, kept for this session only
        self.llm: llm.LlmServer | None = None
        self._llm_wanted = None
        self._llm_gen = 0                       # bumps on every enable/disable; stale loads discard themselves
        self._llm_lock = threading.Lock()       # serializes LLM downloads / server starts
        self.hotwords = HotwordStore()
        self._recent = deque(maxlen=30)         # recent pasted results; only these are learned from
        self._probe: SelectionProbe | None = None
        self._undo = None                       # hotword the last "learned" notification can undo

        self.icon_idle = make_icon(QColor(235, 235, 235) if self._dark_taskbar() else QColor(40, 40, 40))
        self.icon_rec = make_icon(QColor(255, 69, 58))

        self.overlay = Overlay(lambda: self.recorder.level)
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
        self.transcribed.connect(self.on_transcribed)
        self.edited.connect(self.on_edited)
        self.rewriting.connect(self.overlay.show_rewriting)
        self.status.connect(self.on_status)
        self.llm_status.connect(self.on_llm_status)
        self.record.connect(self.on_record)
        self.models_moved.connect(self.load_llm)

        self.ptt = PushToTalk(self.cfg.hotkey, self.pressed.emit, self.released.emit)
        self.ptt.start()
        self.load_model(self.cfg.model if self.cfg.model in models.MODELS else models.DEFAULT_MODEL)
        self.load_llm()
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

    # --- model ---
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

    def change_models_dir(self, path: str):
        """Move the downloaded models to path (empty = default folder), then reload the ASR model and LLM."""
        new = Path(path) if path else paths.DEFAULT_MODELS_DIR
        if new.resolve() == models.models_dir().resolve():
            return
        self.recognizer = None
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

    def load_llm(self):
        """Start or stop the LLM server to match cfg.llm_enabled (the model is only loaded while enabled)."""
        wanted = self.cfg.llm_enabled
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
                server = llm.LlmServer(self.cfg.llm_user_rules)
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
    def on_press(self):
        if self.recognizer is None:
            self.tray.showMessage("VoiceInput", self._status_text or "模型尚未就緒", self.icon_idle, 2000)
            return
        try:
            self.recorder.start(self.cfg.mic)
        except Exception as e:
            log.exception("mic start failed")
            self.tray.showMessage("VoiceInput", f"無法開啟麥克風：{e}", self.icon_idle, 3000)
            return
        self.recording = True
        self._probe = SelectionProbe() if self.cfg.edit_enabled else None
        self.tray.setIcon(self.icon_rec)
        self.overlay.show_recording()

    def on_release(self, held: float):
        if not self.recording:
            return
        self.recording = False
        audio = self.recorder.stop()
        self.tray.setIcon(self.icon_idle)
        if held < TAP_THRESHOLD:
            self.overlay.hide_overlay()
            return
        self.overlay.show_thinking()
        self.worker.submit(self._transcribe_job, self.recognizer, audio, self._probe)

    def _transcribe_job(self, rec, audio, probe):
        stamp = time.strftime("%H:%M:%S")
        try:
            t0 = time.monotonic()
            text = rec.transcribe(audio)
            t_asr = time.monotonic() - t0
            log.info("asr %.2fs: %s", t_asr, text)
            entry = {"time": stamp, "asr": to_traditional(text), "asr_s": t_asr, "llm": "", "llm_s": None}
            # Format before the LLM so it sees Traditional Chinese, joined letters and digits, matching how the
            # user writes rules. No formatting after it: that would override rules such as "add a trailing period".
            raw = text
            text = self.hotwords.apply(format_text(text, self.cfg.strip_trailing_punct))
            entry["fmt"] = text
            sel = probe.result() if probe else None
            if sel is not None:
                ed = edit.plan(sel.text, raw, text)
                entry["llm"] = None
                entry["edit"] = _describe(sel.text, ed)
                log.info("edit on %s: %s", sel.app, entry["edit"])
                self.record.emit(entry)
                self.edited.emit(sel, ed)
                return
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
            self.record.emit(entry)
        except Exception:
            log.exception("transcribe failed")
            text = ""
        self.transcribed.emit(text)

    def on_transcribed(self, text: str):
        self.overlay.hide_overlay()
        log.info("result: %s", text)
        if text:
            self._recent.append(text)
        paste_text(text)

    def on_edited(self, sel, ed):
        self.overlay.hide_overlay()
        self._undo = None   # a click on any newer notification must not undo an older hotword
        if ed.action == edit.DELETE:
            press_delete()
        elif ed.action == edit.REPLACE:
            paste_text(ed.text)
            self._learn(sel.text, ed)
        elif ed.action == edit.CANDIDATES:
            self.tray.showMessage("VoiceInput", "選字選單尚未完成", self.icon_idle, 2000)
        else:
            self.tray.showMessage("VoiceInput", f"辨識結果和選取的文字相同：{ed.text or '（沒有文字）'}",
                                  self.icon_idle, 2000)

    def _learn(self, old: str, ed):
        """Record old -> new as a hotword if old came from a recent result and the edit looks like a correction."""
        i = next((i for i in range(len(self._recent) - 1, -1, -1) if old in self._recent[i]), None)
        if i is None:
            return
        self._recent[i] = self._recent[i].replace(old, ed.text, 1)
        if not worth_learning(old, ed.text, ed.spelled):
            return
        h = self.hotwords.add(old.strip(), ed.text.strip())
        log.info("learned hotword: %s -> %s", h.key, h.value)
        self._undo = h
        self.settings.refresh_hotwords()
        self.tray.showMessage("已記住熱詞", f"{h.key} → {h.value}（點這裡取消）", self.icon_idle, 4000)

    def _undo_learned(self):
        if self._undo is not None:
            log.info("hotword undone: %s -> %s", self._undo.key, self._undo.value)
            self.hotwords.remove(self._undo)
            self._undo = None
            self.settings.refresh_hotwords()

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

    def _set_autostart(self):
        try:
            autostart.set_enabled(self.cfg.autostart)
        except Exception as e:
            log.exception("autostart failed")
            self.settings.set_status(f"開機啟動設定失敗：{e}")

    def quit(self):
        self.ptt.stop()
        self.tray.hide()
        if self.llm:
            self.llm.close()
        self.qapp.quit()


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
