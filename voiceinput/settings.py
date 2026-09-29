"""Settings window with a live transcript log. Every change is applied and saved immediately."""
import html
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QVBoxLayout, QWidget)

from . import audio, hotwords
from .paths import DEFAULT_MODELS_DIR
from .models import is_subfolder
from .hotkey import HOTKEYS

HOTWORDS_NOTE = ("選取文字後用語音修正（例如選「城市」說「程式」、選「Cloud」拼 C L A U D E）、說「城市改成程式」，"
                 "或刪掉剛說的話再重講一次，都會自動記成熱詞（說「不要記」可取消），之後辨識時自動取代，英文詞也會用來引導辨識。"
                 "「看上下文」依修正時前後的詞決定要不要換；沒把握時會跳出「可能是…？」讓你說「對」或「不用」，之後同樣語境就不再問。"
                 "「永遠取代」一律取代。")
MODES = [(hotwords.CONTEXT, "看上下文"), (hotwords.ALWAYS, "永遠取代")]


class SettingsDialog(QDialog):
    applied = Signal()
    models_dir_chosen = Signal(str)   # new models folder, "" = default

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

        self.models_dir = QLineEdit(readOnly=True)
        browse = QPushButton("變更…", clicked=self._choose_models_dir)
        reset = QPushButton("預設", clicked=lambda: self._set_models_dir(""))
        models_dir_row = QHBoxLayout()
        models_dir_row.addWidget(self.models_dir, 1)
        models_dir_row.addWidget(browse)
        models_dir_row.addWidget(reset)

        self.strip_punct = QCheckBox("移除句尾標點（。，,.）")
        self.autostart = QCheckBox("開機時自動啟動")
        self.edit_enabled = QCheckBox("選取文字後說話 = 編輯選取的文字（刪除、選字、拼字、改字）")
        self.voice_commands = QCheckBox("語音指令：復原、送出、換行、刪掉上一句、「城市改成程式」（改剛剛說的內容）")
        self.live_caption = QCheckBox("錄音時顯示即時字幕（串流 X-ASR，約 130MB）")
        self.second_opinion = QCheckBox("背景複查可能聽錯的字（第二個語音模型 SenseVoice）")
        self.screen_terms = QCheckBox("用畫面上的英文詞彙輔助辨識（X-ASR）")
        hotwords_btn = QPushButton("熱詞…", clicked=lambda: self._show_page(1))
        edit_row = QHBoxLayout()
        edit_row.addWidget(self.edit_enabled)
        edit_row.addStretch()
        edit_row.addWidget(hotwords_btn)


        self.status = QLabel()
        self.status.setStyleSheet("color: gray")
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("模型資料夾", models_dir_row)
        form.addRow("麥克風", self.mic)
        form.addRow("錄音快捷鍵（按住）", self.hotkey)
        form.addRow("", self.strip_punct)
        form.addRow("", self.autostart)
        form.addRow("", edit_row)
        form.addRow("", self.voice_commands)
        form.addRow("", self.live_caption)
        form.addRow("", self.second_opinion)
        form.addRow("", self.screen_terms)

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
        for box in (self.strip_punct, self.autostart, self.edit_enabled, self.voice_commands,
                    self.live_caption, self.second_opinion, self.screen_terms):
            box.toggled.connect(self._apply)

    def load_values(self):
        self._loading = True
        self._show_models_dir(self.cfg.models_path())
        self._select(self.mic, self.cfg.mic)
        self._select(self.hotkey, self.cfg.hotkey)
        self.strip_punct.setChecked(self.cfg.strip_trailing_punct)
        self.autostart.setChecked(self.cfg.autostart)
        self.edit_enabled.setChecked(self.cfg.edit_enabled)
        self.voice_commands.setChecked(self.cfg.voice_commands)
        self.live_caption.setChecked(self.cfg.live_caption)
        self.second_opinion.setChecked(self.cfg.second_opinion)
        self.screen_terms.setChecked(self.cfg.screen_terms)
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
        self.cfg.edit_enabled = self.edit_enabled.isChecked()
        self.cfg.voice_commands = self.voice_commands.isChecked()
        self.cfg.live_caption = self.live_caption.isChecked()
        self.cfg.second_opinion = self.second_opinion.isChecked()
        self.cfg.screen_terms = self.screen_terms.isChecked()
        self.cfg.save()
        self.applied.emit()

    def _show_models_dir(self, path: Path):
        self.models_dir.setText(str(path))
        self.models_dir.setToolTip(str(path))

    def _choose_models_dir(self):
        path = QFileDialog.getExistingDirectory(self, "選擇模型資料夾（已下載的模型會搬過去）",
                                                str(self.cfg.models_path()))
        if path:
            self._set_models_dir(path)

    def _set_models_dir(self, path: str):
        new = Path(path) if path else DEFAULT_MODELS_DIR
        if is_subfolder(new, self.cfg.models_path()):
            self.set_status("新的模型資料夾不能在目前的模型資料夾裡面，請選別的資料夾。")
            return
        self._show_models_dir(new)
        self.models_dir_chosen.emit(path)

    # --- hotwords page ---
    def _build_hotwords_page(self) -> QWidget:
        back = QPushButton("← 返回設定", clicked=lambda: self._show_page(0))
        add = QPushButton("新增", clicked=self._add_hotword)
        top = QHBoxLayout()
        top.addWidget(back)
        top.addStretch()
        top.addWidget(add)

        self.hw_table = QTableWidget(0, 5)
        self.hw_table.setHorizontalHeaderLabels(["辨識成（key）", "改成", "套用方式", "命中次數", ""])
        header = self.hw_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in (2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.hw_table.verticalHeader().setVisible(False)
        self.hw_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.hw_table.itemChanged.connect(self._hotword_edited)

        note = QLabel(HOTWORDS_NOTE)
        note.setStyleSheet("color: gray")
        note.setWordWrap(True)

        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.addLayout(top)
        v.addWidget(self.hw_table, 1)
        v.addWidget(note)
        return page

    def _show_page(self, i: int):
        if i == 1:
            self.refresh_hotwords()
        self.pages.setCurrentIndex(i)

    def refresh_hotwords(self):
        t = self.hw_table
        t.blockSignals(True)
        t.setRowCount(0)
        for h in self.hotwords.items:
            r = t.rowCount()
            t.insertRow(r)
            for col, text in ((0, h.key), (1, h.value)):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, h)
                t.setItem(r, col, item)
            hits = QTableWidgetItem(str(h.hits))
            hits.setFlags(Qt.ItemIsEnabled)
            hits.setTextAlignment(Qt.AlignCenter)
            t.setItem(r, 3, hits)
            mode = QComboBox()
            for key, label in MODES:
                mode.addItem(label, key)
            self._select(mode, h.mode)
            mode.currentIndexChanged.connect(lambda _, h=h, m=mode: self._set_mode(h, m.currentData()))
            t.setCellWidget(r, 2, mode)
            t.setCellWidget(r, 4, QPushButton("刪除", clicked=lambda _=False, h=h: self._remove_hotword(h)))
        t.blockSignals(False)

    def _hotword_edited(self, item):
        h = item.data(Qt.UserRole)
        text = item.text().strip()
        if not text:
            self.refresh_hotwords()   # an emptied cell reverts
            return
        if item.column() == 0:
            h.key = text
        else:
            h.value = text
        self.hotwords.save()

    def _set_mode(self, h, mode):
        h.mode = mode
        self.hotwords.save()

    def _remove_hotword(self, h):
        self.hotwords.remove(h)
        self.refresh_hotwords()

    def _add_hotword(self):
        h = hotwords.Hotword("", "")
        self.hotwords.items.append(h)
        self.refresh_hotwords()
        row = self.hw_table.rowCount() - 1
        self.hw_table.scrollToBottom()
        self.hw_table.editItem(self.hw_table.item(row, 0))

    def hideEvent(self, e):
        self.hotwords.items = [h for h in self.hotwords.items if h.key and h.value]   # drop unfinished rows
        self.hotwords.save()
        self.pages.setCurrentIndex(0)
        super().hideEvent(e)
