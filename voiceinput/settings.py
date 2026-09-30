"""Settings window with a live transcript log. Every change is applied and saved immediately."""
import html

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox, QStackedWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from . import audio, hotwords
from .hotkey import HOTKEYS

HOTWORDS_NOTE = ("修正辨識錯的字後（選取後改字或選字、刪掉重講、手動改字），會詢問是否加入熱詞。"
                 "自動判斷：看前後文決定要不要換，沒把握時會問；一律取代：直接換。")
MODES = [(hotwords.CONTEXT, "自動判斷"), (hotwords.ALWAYS, "一律取代")]
_CARD_COLUMNS = 2
_CARD_STYLE = "QFrame#card { border: 1px solid palette(mid); border-radius: 6px; }"


class SettingsDialog(QDialog):
    applied = Signal()

    def __init__(self, cfg, hotword_store):
        super().__init__()
        self.cfg = cfg
        self.hotwords = hotword_store
        self._loading = False
        self.setWindowTitle("VoiceInput 設定")
        self.resize(940, 480)

        self.mic = QComboBox()
        self.mic.addItem("系統預設", "")
        for name in audio.list_input_devices():
            self.mic.addItem(name, name)

        self.hotkey = QComboBox()
        for key, (label, _) in HOTKEYS.items():
            self.hotkey.addItem(label, key)

        self.strip_punct = QCheckBox("移除句尾標點（。，,.）")
        self.autostart = QCheckBox("開機時自動啟動")
        self.fix_menu = QCheckBox("修改的內容發音相近時，顯示選字選單")
        self.fix_menu.setToolTip("選取後重講、或撤銷後重講成發音相近的字（城市 → 程式）時，跳出同音字選單；"
                                 "關閉時改為直接詢問是否加入熱詞")
        self.menu_seconds = QSpinBox(minimum=3, maximum=120, suffix=" 秒")
        self.menu_seconds.setToolTip("選字選單、建議選單和加入熱詞的 ✓ 框，沒點的話幾秒後自動關閉")
        self.menu_seconds.valueChanged.connect(self._apply)
        hotwords_btn = QPushButton("管理熱詞…", clicked=lambda: self._show_page(1))
        hotwords_row = QHBoxLayout()
        hotwords_row.addWidget(hotwords_btn)
        hotwords_row.addStretch()

        self.status = QLabel()
        self.status.setStyleSheet("color: gray")
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("麥克風", self.mic)
        hotkey_note = QLabel("按住說話；快按兩下撤銷剛輸入的文字，第二下按住可直接重講。\n"
                             "選取文字時：按一下開選字選單，快按兩下刪除，第二下按住可刪除後重講")
        hotkey_note.setStyleSheet("color: gray")
        hotkey_note.setWordWrap(True)
        form.addRow("錄音快捷鍵", self.hotkey)
        form.addRow("", hotkey_note)
        form.addRow("", self.strip_punct)
        form.addRow("", self.autostart)
        form.addRow("", self.fix_menu)
        form.addRow("選單自動關閉", self.menu_seconds)
        form.addRow("熱詞", hotwords_row)

        left = QVBoxLayout()
        left.addLayout(form)
        left.addStretch(1)
        left.addWidget(self.status)

        self.log = QTextBrowser()
        self.log.setPlaceholderText("每次說話的辨識結果、格式化結果與指令會顯示在這裡（只保留這次執行期間）")
        right = QVBoxLayout()
        right.addWidget(QLabel("辨識紀錄"))
        right.addWidget(self.log, 1)

        cols = QHBoxLayout()
        cols.addLayout(left, 4)
        cols.addSpacing(12)
        cols.addLayout(right, 5)

        main_page = QWidget()
        main_page.setLayout(cols)
        cols.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        self.pages.addWidget(main_page)
        self.pages.addWidget(self._build_hotwords_page())

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.pages, 1)
        layout.addWidget(buttons)

        for combo in (self.mic, self.hotkey):
            combo.currentIndexChanged.connect(self._apply)
        for box in (self.strip_punct, self.autostart, self.fix_menu):
            box.toggled.connect(self._apply)

    def load_values(self):
        self._loading = True
        self._select(self.mic, self.cfg.mic)
        self._select(self.hotkey, self.cfg.hotkey)
        self.strip_punct.setChecked(self.cfg.strip_trailing_punct)
        self.autostart.setChecked(self.cfg.autostart)
        self.fix_menu.setChecked(self.cfg.fix_menu)
        self.menu_seconds.setValue(self.cfg.menu_seconds)
        self._loading = False

    def set_status(self, text: str):
        self.status.setText(text)

    def set_log(self, entries):
        """Render entries newest first, in pipeline order. Each entry: time, asr, asr_s, fmt, and edit (a
        command or edit description, optional)."""
        def row(label, secs, text, color=""):
            t = f"{secs:.2f}s" if secs is not None else ""
            style = f" style='color:{color}'" if color else ""
            return (f"<tr><td width=40><b>{label}</b></td><td width=46 style='color:gray'>{t}</td>"
                    f"<td{style}>{html.escape(text)}</td></tr>")

        def block(e):
            return (f"<div style='color:gray'>{e['time']}</div><table cellspacing=0 cellpadding=2>"
                    + row("ASR", e["asr_s"], e["asr"])
                    + row("格式", None, e["fmt"])
                    + (row("編輯", None, e["edit"]) if e.get("edit") else "")
                    + "</table>")

        self.log.setHtml("<br>".join(block(e) for e in reversed(entries)))
        self.log.verticalScrollBar().setValue(0)

    @staticmethod
    def _select(combo, value):
        i = combo.findData(value)
        combo.setCurrentIndex(max(0, i))

    def _apply(self):
        if self._loading:
            return
        self.cfg.mic = self.mic.currentData()
        self.cfg.hotkey = self.hotkey.currentData()
        self.cfg.strip_trailing_punct = self.strip_punct.isChecked()
        self.cfg.autostart = self.autostart.isChecked()
        self.cfg.fix_menu = self.fix_menu.isChecked()
        self.cfg.menu_seconds = self.menu_seconds.value()
        self.cfg.save()
        self.applied.emit()

    # --- hotwords page: one card per hotword, in a grid ---
    def _build_hotwords_page(self) -> QWidget:
        back = QPushButton("← 返回設定", clicked=lambda: self._show_page(0))
        add = QPushButton("新增", clicked=self._add_hotword)
        top = QHBoxLayout()
        top.addWidget(back)
        top.addStretch()
        top.addWidget(add)

        self.hw_grid = QGridLayout()
        self.hw_grid.setSpacing(6)
        for c in range(_CARD_COLUMNS):
            self.hw_grid.setColumnStretch(c, 1)
        cards = QWidget()
        inner = QVBoxLayout(cards)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addLayout(self.hw_grid)
        inner.addStretch(1)
        self.hw_scroll = QScrollArea(widgetResizable=True, frameShape=QFrame.NoFrame)
        self.hw_scroll.setWidget(cards)

        note = QLabel(HOTWORDS_NOTE)
        note.setStyleSheet("color: gray")
        note.setWordWrap(True)

        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.addLayout(top)
        v.addWidget(self.hw_scroll, 1)
        v.addWidget(note)
        return page

    def _show_page(self, i: int):
        if i == 1:
            self.refresh_hotwords()
        self.pages.setCurrentIndex(i)

    def refresh_hotwords(self):
        while self.hw_grid.count():
            w = self.hw_grid.takeAt(0).widget()
            w.setParent(None)
            w.deleteLater()
        for i, h in enumerate(self.hotwords.items):
            self.hw_grid.addWidget(self._hotword_card(h), i // _CARD_COLUMNS, i % _CARD_COLUMNS)

    def _hotword_card(self, h) -> QFrame:
        card = QFrame(objectName="card")
        card.setStyleSheet(_CARD_STYLE)
        key = QLineEdit(h.key, placeholderText="辨識成")
        value = QLineEdit(h.value, placeholderText="改成")
        for line, attr in ((key, "key"), (value, "value")):
            line.editingFinished.connect(lambda e=line, a=attr: self._hotword_edited(h, a, e))
        mode = QComboBox()
        for k, label in MODES:
            mode.addItem(label, k)
        self._select(mode, h.mode)
        mode.currentIndexChanged.connect(lambda _: self._set_mode(h, mode.currentData()))
        hits = QLabel(f"{h.hits} 次")
        hits.setStyleSheet("color: gray")
        hits.setToolTip("命中次數")
        remove = QPushButton("✕", clicked=lambda: self._remove_hotword(h))
        remove.setFixedWidth(28)
        remove.setToolTip("刪除")
        row = QHBoxLayout(card)
        row.setContentsMargins(8, 4, 4, 4)
        row.addWidget(key, 1)
        row.addWidget(QLabel("→"))
        row.addWidget(value, 1)
        row.addWidget(mode)
        row.addWidget(hits)
        row.addWidget(remove)
        return card

    def _hotword_edited(self, h, attr: str, line: QLineEdit):
        text = line.text().strip()
        if not text:
            line.setText(getattr(h, attr))   # an emptied field reverts
            return
        setattr(h, attr, text)
        self.hotwords.save()

    def _set_mode(self, h, mode):
        h.mode = mode
        self.hotwords.save()

    def _remove_hotword(self, h):
        self.hotwords.remove(h)
        self.refresh_hotwords()

    def _add_hotword(self):
        self.hotwords.items.append(hotwords.Hotword("", ""))
        self.refresh_hotwords()
        card = self.hw_grid.itemAt(self.hw_grid.count() - 1).widget()
        self.hw_scroll.ensureWidgetVisible(card)
        card.findChild(QLineEdit).setFocus()

    def hideEvent(self, e):
        self.hotwords.items = [h for h in self.hotwords.items if h.key and h.value]   # drop unfinished rows
        self.hotwords.save()
        self.pages.setCurrentIndex(0)
        super().hideEvent(e)
