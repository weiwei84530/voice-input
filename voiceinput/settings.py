"""Simple settings window. Every change is applied and saved immediately."""
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)

from . import audio, llm
from .hotkey import HOTKEYS


class SettingsDialog(QDialog):
    applied = Signal()

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self._loading = False
        self.setWindowTitle("VoiceInput 設定")
        self.setMinimumWidth(460)

        self.mic = QComboBox()
        self.mic.addItem("系統預設", "")
        for name in audio.list_input_devices():
            self.mic.addItem(name, name)

        self.hotkey = QComboBox()
        for key, (label, _) in HOTKEYS.items():
            self.hotkey.addItem(label, key)

        self.strip_punct = QCheckBox("移除句尾標點（。，,.）")
        self.autostart = QCheckBox("開機時自動啟動")

        self.llm = QComboBox()
        self.llm.addItem("關閉", "")
        for key, m in llm.LLM_MODELS.items():
            self.llm.addItem(m["label"], key)

        self.prompt = QPlainTextEdit()
        self.prompt.setMinimumHeight(140)
        reset = QPushButton("還原預設提示詞")
        reset.clicked.connect(lambda: self.prompt.setPlainText(llm.DEFAULT_PROMPT))
        # Save the prompt shortly after typing stops
        self._prompt_timer = QTimer(self, singleShot=True, interval=600, timeout=self._save_prompt)
        self.prompt.textChanged.connect(lambda: None if self._loading else self._prompt_timer.start())

        self.status = QLabel()
        self.status.setStyleSheet("color: gray")
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("麥克風", self.mic)
        form.addRow("錄音快捷鍵（按住）", self.hotkey)
        form.addRow("", self.strip_punct)
        form.addRow("", self.autostart)
        form.addRow("LLM 校正", self.llm)
        form.addRow("LLM 提示詞", self.prompt)
        reset_row = QHBoxLayout()
        reset_row.addStretch()
        reset_row.addWidget(reset)
        form.addRow("", reset_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        for combo in (self.mic, self.hotkey, self.llm):
            combo.currentIndexChanged.connect(self._apply)
        for box in (self.strip_punct, self.autostart):
            box.toggled.connect(self._apply)

    def load_values(self):
        self._loading = True
        for i in range(1, self.llm.count()):
            key = self.llm.itemData(i)
            self.llm.setItemText(i, llm.LLM_MODELS[key]["label"] + ("" if llm.is_installed(key) else "（未下載）"))
        self._select(self.mic, self.cfg.mic)
        self._select(self.hotkey, self.cfg.hotkey)
        self._select(self.llm, self.cfg.llm_model)
        self.strip_punct.setChecked(self.cfg.strip_trailing_punct)
        self.autostart.setChecked(self.cfg.autostart)
        self.prompt.setPlainText(self.cfg.llm_prompt)
        self._loading = False

    def set_status(self, text: str):
        self.status.setText(text)

    @staticmethod
    def _select(combo, value):
        i = combo.findData(value)
        combo.setCurrentIndex(max(0, i))

    def _apply(self):
        if self._loading:
            return
        self.cfg.mic = self.mic.currentData()
        self.cfg.hotkey = self.hotkey.currentData()
        self.cfg.llm_model = self.llm.currentData()
        self.cfg.strip_trailing_punct = self.strip_punct.isChecked()
        self.cfg.autostart = self.autostart.isChecked()
        self.cfg.save()
        self.applied.emit()

    def _save_prompt(self):
        self.cfg.llm_prompt = self.prompt.toPlainText()
        self.cfg.save()

    def hideEvent(self, e):
        if self._prompt_timer.isActive():
            self._prompt_timer.stop()
            self._save_prompt()
        super().hideEvent(e)
