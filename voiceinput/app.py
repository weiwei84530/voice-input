"""VoiceInput: single-process tray app. Hold the hotkey to talk, release to paste."""
import logging
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QLockFile, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import autostart, llm, models
from .asr import Recognizer
from .audio import Recorder
from .config import Config
from .hotkey import TAP_THRESHOLD, PushToTalk
from .output import paste_text
from .overlay import Overlay
from .settings import SettingsDialog
from .textfmt import format_text

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
    rewriting = Signal()
    record = Signal(dict)         # one transcript log entry for the settings window
    status = Signal(str)
    llm_status = Signal(str, str)  # state (off / loading / ready / error), text

    def __init__(self, qapp: QApplication):
        super().__init__()
        self.qapp = qapp
        self.cfg = Config.load()
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

        self.icon_idle = make_icon(QColor(235, 235, 235) if self._dark_taskbar() else QColor(40, 40, 40))
        self.icon_rec = make_icon(QColor(255, 69, 58))

        self.overlay = Overlay(lambda: self.recorder.level)
        self.settings = SettingsDialog(self.cfg)
        self.settings.applied.connect(self.apply_settings)

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
        self.transcribed.connect(self.on_transcribed)
        self.rewriting.connect(self.overlay.show_rewriting)
        self.status.connect(self.on_status)
        self.llm_status.connect(self.on_llm_status)
        self.record.connect(self.on_record)

        self.ptt = PushToTalk(self.cfg.hotkey, self.pressed.emit, self.released.emit)
        self.ptt.start()
        self.load_model(self.cfg.model if self.cfg.model in models.MODELS else models.DEFAULT_MODEL)
        self.load_llm()

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
        self.worker.submit(self._transcribe_job, self.recognizer, audio)

    def _transcribe_job(self, rec, audio):
        stamp = time.strftime("%H:%M:%S")
        try:
            t0 = time.monotonic()
            text = rec.transcribe(audio)
            t_asr = time.monotonic() - t0
            log.info("asr %.2fs: %s", t_asr, text)
            entry = {"time": stamp, "asr": text, "asr_s": t_asr, "llm": "", "llm_s": None}
            server, rules = self.llm, self.cfg.llm_user_rules.strip()
            if not self.cfg.llm_enabled:
                entry["llm"] = "（未啟用）"
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
            entry["final"] = format_text(text, self.cfg.strip_trailing_punct)
            text = entry["final"]
            self.record.emit(entry)
        except Exception:
            log.exception("transcribe failed")
            text = ""
        self.transcribed.emit(text)

    def on_transcribed(self, text: str):
        self.overlay.hide_overlay()
        log.info("result: %s", text)
        paste_text(text)

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
        self.settings.append_log(entry)

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
        try:
            autostart.set_enabled(self.cfg.autostart)
        except Exception as e:
            log.exception("autostart failed")
            self.settings.set_status(f"開機啟動設定失敗：{e}")
        if self.cfg.model != self._model_key:
            self.load_model(self.cfg.model)
        self.load_llm()

    def quit(self):
        self.ptt.stop()
        self.tray.hide()
        if self.llm:
            self.llm.close()
        self.qapp.quit()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(models.ROOT / "voiceinput.log", encoding="utf-8"),
                  logging.StreamHandler()],
    )
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)

    lock = QLockFile(str(models.ROOT / ".voiceinput.lock"))
    if not lock.tryLock(100):
        log.info("already running")
        return 0

    app = App(qapp)  # noqa: F841 (keep reference alive)
    return qapp.exec()
