"""Settings window with a live transcript log. Every change is applied and saved immediately."""
import html

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QPlainTextEdit, QTextBrowser, QVBoxLayout)

from . import audio, llm
from .models import MODELS, is_installed
from .hotkey import HOTKEYS

RULES_HINT = "例如：\n- 「cloud code」一律寫成「Claude Code」\n- 「那個」不要刪"
RULES_NOTE = "LLM 會逐字照規則執行，例如寫「句尾加句號」，連問句也會被加上句號。規則空白時不會執行 LLM。"


class SettingsDialog(QDialog):
    applied = Signal()

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self._loading = False
        self._llm_state = "off"
        self.setWindowTitle("VoiceInput 設定")
        self.resize(940, 480)

        self.model = QComboBox()
        for key, m in MODELS.items():
            self.model.addItem(m["label"], key)

        self.mic = QComboBox()
        self.mic.addItem("系統預設", "")
        for name in audio.list_input_devices():
            self.mic.addItem(name, name)

        self.hotkey = QComboBox()
        for key, (label, _) in HOTKEYS.items():
            self.hotkey.addItem(label, key)

        self.strip_punct = QCheckBox("移除句尾標點（。，,.）")
        self.autostart = QCheckBox("開機時自動啟動")

        self.llm_enabled = QCheckBox(f"啟用自訂規則（本機 LLM：{llm.LLM_LABEL}）")
        self.llm_hint = QLabel()
        self.llm_hint.setStyleSheet("color: gray")
        self.rules = QPlainTextEdit()
        self.rules.setPlaceholderText(RULES_HINT)
        self.rules.setMinimumHeight(150)
        # Save the rules shortly after typing stops
        self._rules_timer = QTimer(self, singleShot=True, interval=600, timeout=self._save_rules)
        self.rules.textChanged.connect(lambda: None if self._loading else self._rules_timer.start())

        self.rules_note = QLabel(RULES_NOTE)
        self.rules_note.setStyleSheet("color: gray")
        self.rules_note.setWordWrap(True)

        self.status = QLabel()
        self.status.setStyleSheet("color: gray")
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("語音模型", self.model)
        form.addRow("麥克風", self.mic)
        form.addRow("錄音快捷鍵（按住）", self.hotkey)
        form.addRow("", self.strip_punct)
        form.addRow("", self.autostart)
        llm_row = QHBoxLayout()
        llm_row.addWidget(self.llm_enabled)
        llm_row.addStretch()
        llm_row.addWidget(self.llm_hint)

        left = QVBoxLayout()
        left.addLayout(form)
        left.addSpacing(8)
        left.addLayout(llm_row)
        left.addWidget(self.rules, 1)
        left.addWidget(self.rules_note)
        left.addWidget(self.status)

        self.log = QTextBrowser()
        self.log.setPlaceholderText("每次說話的 ASR、LLM 與最終輸出會顯示在這裡（只保留這次執行期間）")
        right = QVBoxLayout()
        right.addWidget(QLabel("辨識紀錄"))
        right.addWidget(self.log, 1)

        cols = QHBoxLayout()
        cols.addLayout(left, 4)
        cols.addSpacing(12)
        cols.addLayout(right, 5)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addLayout(cols, 1)
        layout.addWidget(buttons)

        for combo in (self.model, self.mic, self.hotkey):
            combo.currentIndexChanged.connect(self._apply)
        for box in (self.strip_punct, self.autostart, self.llm_enabled):
            box.toggled.connect(self._apply)

    def load_values(self):
        self._loading = True
        for i in range(self.model.count()):
            key = self.model.itemData(i)
            self.model.setItemText(i, MODELS[key]["label"] + ("" if is_installed(key) else "（未下載）"))
        self._select(self.model, self.cfg.model)
        self._select(self.mic, self.cfg.mic)
        self._select(self.hotkey, self.cfg.hotkey)
        self.strip_punct.setChecked(self.cfg.strip_trailing_punct)
        self.autostart.setChecked(self.cfg.autostart)
        self.llm_enabled.setChecked(self.cfg.llm_enabled)
        self.rules.setPlainText(self.cfg.llm_user_rules)
        self._loading = False
        self._update_rules_enabled()

    def set_status(self, text: str):
        self.status.setText(text)

    def set_llm_state(self, state: str, text: str):
        """state: off / loading / ready / error. The rules box is editable only when the model is ready."""
        self._llm_state = state
        self.llm_hint.setText(text)
        self._update_rules_enabled()

    def set_log(self, entries):
        self.log.clear()
        for e in entries:
            self.append_log(e)

    def append_log(self, entry: dict):
        """entry: time, asr, asr_s, llm, llm_s, final; llm_s is None when the LLM did not run."""
        def row(label, secs, text, color=""):
            t = f"{secs:.2f}s" if secs is not None else ""
            style = f" style='color:{color}'" if color else ""
            return (f"<tr><td width=40><b>{label}</b></td><td width=46 style='color:gray'>{t}</td>"
                    f"<td{style}>{html.escape(text)}</td></tr>")
        self.log.append(
            f"<div style='color:gray'>{entry['time']}</div><table cellspacing=0 cellpadding=2>"
            + row("ASR", entry["asr_s"], entry["asr"])
            + row("LLM", entry["llm_s"], entry["llm"], "" if entry["llm_s"] is not None else "gray")
            + row("最終", None, entry["final"])
            + "</table>")
        self._scroll_log()

    def _scroll_log(self):
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _update_rules_enabled(self):
        self.rules.setEnabled(self.llm_enabled.isChecked() and self._llm_state == "ready")

    @staticmethod
    def _select(combo, value):
        i = combo.findData(value)
        combo.setCurrentIndex(max(0, i))

    def _apply(self):
        if self._loading:
            return
        self.cfg.model = self.model.currentData()
        self.cfg.mic = self.mic.currentData()
        self.cfg.hotkey = self.hotkey.currentData()
        self.cfg.strip_trailing_punct = self.strip_punct.isChecked()
        self.cfg.autostart = self.autostart.isChecked()
        self.cfg.llm_enabled = self.llm_enabled.isChecked()
        self.cfg.save()
        self._update_rules_enabled()
        self.applied.emit()

    def _save_rules(self):
        self.cfg.llm_user_rules = self.rules.toPlainText()
        self.cfg.save()

    def hideEvent(self, e):
        if self._rules_timer.isActive():
            self._rules_timer.stop()
            self._save_rules()
        super().hideEvent(e)
