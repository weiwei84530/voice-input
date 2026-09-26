"""Settings window with a live transcript log. Every change is applied and saved immediately."""
import html
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QVBoxLayout, QWidget)

from . import audio, hotwords, llm
from .paths import DEFAULT_MODELS_DIR
from .models import MODELS, is_installed
from .hotkey import HOTKEYS

RULES_HINT = "例如：\n- 「cloud code」一律寫成「Claude Code」\n- 「那個」不要刪"
RULES_NOTE = "LLM 會逐字照規則執行，例如寫「句尾加句號」，連問句也會被加上句號。規則空白時不會執行 LLM。"
HOTWORDS_NOTE = ("選取文字後用語音修正（例如選「城市」說「程式」、選「Cloud」拼 C L A U D E），會自動記成熱詞，"
                 "之後辨識時自動取代。「看上下文」會先判斷句子是否適合再取代（需要本機 LLM，尚未完成，目前不會套用）；"
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

        self.models_dir = QLineEdit(readOnly=True)
        browse = QPushButton("變更…", clicked=self._choose_models_dir)
        reset = QPushButton("預設", clicked=lambda: self._set_models_dir(""))
        models_dir_row = QHBoxLayout()
        models_dir_row.addWidget(self.models_dir, 1)
        models_dir_row.addWidget(browse)
        models_dir_row.addWidget(reset)

        self.strip_punct = QCheckBox("移除句尾標點（。，,.）")
        self.autostart = QCheckBox("開機時自動啟動")
        self.edit_enabled = QCheckBox("選取文字後說話 = 編輯選取的文字（刪除、拼字、改字）")
        hotwords_btn = QPushButton("熱詞…", clicked=lambda: self._show_page(1))
        edit_row = QHBoxLayout()
        edit_row.addWidget(self.edit_enabled)
        edit_row.addStretch()
        edit_row.addWidget(hotwords_btn)

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
        form.addRow("模型資料夾", models_dir_row)
        form.addRow("麥克風", self.mic)
        form.addRow("錄音快捷鍵（按住）", self.hotkey)
        form.addRow("", self.strip_punct)
        form.addRow("", self.autostart)
        form.addRow("", edit_row)
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

        for combo in (self.model, self.mic, self.hotkey):
            combo.currentIndexChanged.connect(self._apply)
        for box in (self.strip_punct, self.autostart, self.llm_enabled, self.edit_enabled):
            box.toggled.connect(self._apply)

    def load_values(self):
        self._loading = True
        for i in range(self.model.count()):
            key = self.model.itemData(i)
            self.model.setItemText(i, MODELS[key]["label"] + ("" if is_installed(key) else "（未下載）"))
        self._select(self.model, self.cfg.model)
        self._show_models_dir(self.cfg.models_path())
        self._select(self.mic, self.cfg.mic)
        self._select(self.hotkey, self.cfg.hotkey)
        self.strip_punct.setChecked(self.cfg.strip_trailing_punct)
        self.autostart.setChecked(self.cfg.autostart)
        self.edit_enabled.setChecked(self.cfg.edit_enabled)
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
        """Render entries newest first, in pipeline order. Each entry: time, asr, asr_s, fmt, llm, llm_s
        (llm None = LLM disabled, row hidden; llm_s None = LLM did not run, fmt is what was pasted)."""
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
                    + ("" if e["llm"] is None else
                       row("LLM", e["llm_s"], e["llm"], "" if e["llm_s"] is not None else "gray"))
                    + "</table>")

        self.log.setHtml("<br>".join(block(e) for e in reversed(entries)))
        self.log.verticalScrollBar().setValue(0)

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
        self.cfg.edit_enabled = self.edit_enabled.isChecked()
        self.cfg.llm_enabled = self.llm_enabled.isChecked()
        self.cfg.save()
        self._update_rules_enabled()
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
        self._show_models_dir(Path(path) if path else DEFAULT_MODELS_DIR)
        self.models_dir_chosen.emit(path)

    def _save_rules(self):
        self.cfg.llm_user_rules = self.rules.toPlainText()
        self.cfg.save()

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
        if self._rules_timer.isActive():
            self._rules_timer.stop()
            self._save_rules()
        self.hotwords.items = [h for h in self.hotwords.items if h.key and h.value]   # drop unfinished rows
        self.hotwords.save()
        self.pages.setCurrentIndex(0)
        super().hideEvent(e)
