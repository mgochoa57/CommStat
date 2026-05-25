# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
sound_settings.py - Sound Settings dialog.

Three fixed rows (Alerts / Messages / Status Reports), each with a Play
button, a sound-file picker (combo of .wav files in SOUNDS_DIR), and an
Enabled checkbox. Selections persist to config.ini and take effect
immediately via SoundPlayer.reload(event).
"""

import os
from typing import List

from PyQt5 import QtGui
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QComboBox, QWidget,
)

from constants import DEFAULT_COLORS, SOUNDS_DIR
from ui_helpers import (
    make_button, make_checkbox_cell, mono_font, apply_standard_dialog_chrome,
)


_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_TITLE_BG = DEFAULT_COLORS.get("title_bar_background", "#F07800")
_TITLE_FG = DEFAULT_COLORS.get("title_bar_foreground", "#FFFFFF")
_DATA_BG  = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG  = DEFAULT_COLORS.get("data_foreground",      "#000000")

_COL_PLAY  = "#17a2b8"
_COL_CLOSE = "#555555"

_WIN_W = 640
_WIN_H = 320

_TABLE_COLS = ["Play", "Event", "Sound", "Enabled"]

_ROWS = [
    ("alert",   "Alerts"),
    ("message", "Messages"),
    ("statrep", "Status Reports"),
]

_MISSING_SUFFIX = "  (missing)"


def _list_wav_files() -> List[str]:
    try:
        return sorted(
            f for f in os.listdir(SOUNDS_DIR)
            if f.lower().endswith(".wav")
        )
    except OSError:
        return []


class SoundSettingsDialog(QDialog):
    """Sound Settings — per-event sound file + enable toggle."""

    def __init__(self, config, sound_player, parent=None):
        super().__init__(parent)
        self.config = config
        self.sound_player = sound_player
        self._suppress_signals = False

        apply_standard_dialog_chrome(self, "Sound Settings", _WIN_W, _WIN_H)
        self._setup_ui()
        self._populate_rows()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; }}"
            f"QLabel {{ font-size:13px; }}"
        )

        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        title_lbl = QLabel("Sound Settings")
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title_lbl.setFixedHeight(36)
        title_lbl.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab', serif; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        body.addWidget(title_lbl)

        self.table = QTableWidget(len(_ROWS), len(_TABLE_COLS))
        self.table.setHorizontalHeaderLabels(_TABLE_COLS)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        self.table.setStyleSheet(
            f"QTableWidget {{ background-color:{_DATA_BG}; gridline-color:#cccccc;"
            f" color:{_DATA_FG}; font-family:'Kode Mono', monospace; font-size:13px; }}"
            f"QTableWidget::item {{ padding:4px 6px; }}"
            f"QHeaderView::section {{ background-color:{_TITLE_BG}; color:{_TITLE_FG};"
            f" padding:5px 6px; border:none; font-family:Roboto, sans-serif; font-size:13px;"
            f" font-weight:bold; }}"
        )

        self.table.verticalHeader().setDefaultSectionSize(40)
        body.addWidget(self.table)

        tip_lbl = QLabel(
            f"<span style='color:{_PANEL_FG}'>"
            f"Drop additional <b>.wav</b> files into the <code>{SOUNDS_DIR}/</code>"
            f" folder and reopen this dialog to see them in the dropdown."
            f"</span>"
        )
        tip_lbl.setWordWrap(True)
        body.addWidget(tip_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_close = make_button("Close", _COL_CLOSE, 80)
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)
        body.addLayout(btn_row)

    def _populate_rows(self) -> None:
        self._suppress_signals = True
        wav_files = _list_wav_files()

        for row_idx, (event, label) in enumerate(_ROWS):
            # Column 0 — Play button (wrapped so cell padding doesn't collapse)
            play_btn = make_button("▶", _COL_PLAY, 40)
            play_btn.clicked.connect(lambda _checked, e=event: self._on_play(e))
            play_cell = QWidget()
            play_cell.setStyleSheet("background-color: transparent;")
            play_layout = QHBoxLayout(play_cell)
            play_layout.setContentsMargins(6, 4, 10, 4)
            play_layout.addWidget(play_btn)
            self.table.setCellWidget(row_idx, 0, play_cell)

            # Column 1 — Event label (static, bold)
            item = QTableWidgetItem(label)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            f = QtGui.QFont("Roboto")
            f.setBold(True)
            f.setPixelSize(13)
            item.setFont(f)
            self.table.setItem(row_idx, 1, item)

            # Column 2 — Sound file combo
            combo = QComboBox()
            combo.setStyleSheet(
                "QComboBox { background-color:#FFFFFF; color:#000000;"
                " border:1px solid #cccccc; border-radius:4px; padding:2px 4px; }"
                f"QComboBox QAbstractItemView {{ background-color:{_PANEL_BG}; color:#000000;"
                " selection-background-color:#cce5ff; selection-color:#000000; }"
            )
            combo.setFont(mono_font())
            combo.addItems(wav_files)
            current = self.config.get_sound_file(event)
            if current and current not in wav_files:
                # File configured but not present on disk — show it flagged so
                # the user knows why nothing plays for this event.
                display = current + _MISSING_SUFFIX
                combo.addItem(display)
                combo.setCurrentText(display)
            elif current in wav_files:
                combo.setCurrentText(current)
            combo.currentTextChanged.connect(
                lambda text, e=event: self._on_file_changed(e, text)
            )
            self.table.setCellWidget(row_idx, 2, combo)

            # Column 3 — Enabled checkbox
            container, checkbox = make_checkbox_cell(self.config.get_sound_enabled(event))
            checkbox.stateChanged.connect(
                lambda state, e=event: self._on_enabled_changed(e, state == Qt.Checked)
            )
            self.table.setCellWidget(row_idx, 3, container)

        self._suppress_signals = False

    def _on_play(self, event: str) -> None:
        combo = self._combo_for(event)
        if combo is None:
            return
        filename = combo.currentText().replace(_MISSING_SUFFIX, "")
        self.sound_player.preview(filename)

    def _on_file_changed(self, event: str, text: str) -> None:
        if self._suppress_signals:
            return
        filename = text.replace(_MISSING_SUFFIX, "")
        self.config.set_sound_file(event, filename)
        self.sound_player.reload(event)

    def _on_enabled_changed(self, event: str, enabled: bool) -> None:
        if self._suppress_signals:
            return
        self.config.set_sound_enabled(event, enabled)

    def _combo_for(self, event: str) -> QComboBox:
        for row_idx, (e, _label) in enumerate(_ROWS):
            if e == event:
                widget = self.table.cellWidget(row_idx, 2)
                if isinstance(widget, QComboBox):
                    return widget
        return None
