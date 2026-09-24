"""Simple settings window."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
                               QVBoxLayout)

from . import audio
from .hotkey import HOTKEYS
from .models import MODELS, is_installed


class SettingsDialog(QDialog):
    applied = Signal()

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("VoiceInput 設定")
        self.setMinimumWidth(380)

        self.model = QComboBox()
        for key, m in MODELS.items():
            label = m["label"] + ("" if is_installed(key) else "（未下載）")
            self.model.addItem(label, key)

        self.mic = QComboBox()
        self.mic.addItem("系統預設", "")
        for name in audio.list_input_devices():
            self.mic.addItem(name, name)

        self.hotkey = QComboBox()
        for key, (label, _) in HOTKEYS.items():
            self.hotkey.addItem(label, key)

        self.traditional = QCheckBox("輸出轉為繁體中文（台灣用語）")
        self.autostart = QCheckBox("開機時自動啟動")

        self.status = QLabel()
        self.status.setStyleSheet("color: gray")

        form = QFormLayout()
        form.addRow("語音模型", self.model)
        form.addRow("麥克風", self.mic)
        form.addRow("錄音快捷鍵（按住）", self.hotkey)
        form.addRow("", self.traditional)
        form.addRow("", self.autostart)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

    def load_values(self):
        self._select(self.model, self.cfg.model)
        self._select(self.mic, self.cfg.mic)
        self._select(self.hotkey, self.cfg.hotkey)
        self.traditional.setChecked(self.cfg.traditional)
        self.autostart.setChecked(self.cfg.autostart)

    def set_status(self, text: str):
        self.status.setText(text)

    @staticmethod
    def _select(combo, value):
        i = combo.findData(value)
        combo.setCurrentIndex(max(0, i))

    def _save(self):
        self.cfg.model = self.model.currentData()
        self.cfg.mic = self.mic.currentData()
        self.cfg.hotkey = self.hotkey.currentData()
        self.cfg.traditional = self.traditional.isChecked()
        self.cfg.autostart = self.autostart.isChecked()
        self.cfg.save()
        self.applied.emit()
