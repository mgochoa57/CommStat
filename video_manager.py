# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
video_manager.py - Video Manager Dialog

Lists every row in the videos table (oldest first) with a Play button that
opens the video's URL in the OS browser, and a Delete button that removes
the row.
"""

from PyQt5 import QtGui
from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QWidget,
)

from constants import DEFAULT_COLORS, COLOR_BTN_CYAN, COLOR_BTN_RED, COLOR_BTN_CLOSE
from ui_helpers import make_button, confirm, apply_standard_dialog_chrome

# ── Constants ──────────────────────────────────────────────────────────────────

_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_TITLE_BG = DEFAULT_COLORS.get("title_bar_background", "#F07800")
_TITLE_FG = DEFAULT_COLORS.get("title_bar_foreground", "#FFFFFF")
_DATA_BG  = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG  = DEFAULT_COLORS.get("data_foreground",      "#000000")

_COL_PLAY  = COLOR_BTN_CYAN
_COL_DELETE = COLOR_BTN_RED
_COL_CLOSE = COLOR_BTN_CLOSE

# 50% wider and 40% taller than User Settings (680 x 260).
_WIN_W = 1020
_WIN_H = 364

_TABLE_COLS = ["Play", "Title", "From", "Target", "Date", "Delete"]


# ── Dialog ─────────────────────────────────────────────────────────────────────

class VideoManagerDialog(QDialog):
    """Video Manager dialog — lists all received/sent videos with Play/Delete."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        # No fixed size passed to the chrome helper — that's what keeps the
        # window resizable. Floor is User Settings' original size; initial
        # size is the 50%-wider/40%-taller dimensions the table needs.
        apply_standard_dialog_chrome(self, "Video Manager")
        self.setMinimumSize(680, 260)
        self.resize(_WIN_W, _WIN_H)

        self._setup_ui()
        self._load()

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; color:{_PANEL_FG}; }}"
            f"QLabel {{ font-size:13px; color:{_PANEL_FG}; }}"
        )

        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        # ── Title ─────────────────────────────────────────────────────────────
        title_lbl = QLabel("Video Manager")
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title_lbl.setFixedHeight(36)
        title_lbl.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        body.addWidget(title_lbl)

        # ── Table ─────────────────────────────────────────────────────────────
        self.table = QTableWidget(0, len(_TABLE_COLS))
        self.table.setHorizontalHeaderLabels(_TABLE_COLS)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setAlternatingRowColors(False)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)

        self.table.setStyleSheet(
            f"QTableWidget {{ background-color:{_DATA_BG}; alternate-background-color:{_DATA_BG};"
            f" gridline-color:#cccccc; color:{_DATA_FG};"
            f" font-family:'Kode Mono'; font-size:13px; }}"
            f"QTableWidget::item {{ padding:4px 6px; }}"
            f"QHeaderView::section {{ background-color:{_TITLE_BG}; color:{_TITLE_FG};"
            f" padding:5px 6px; border:none; font-family:Roboto; font-size:13px;"
            f" font-weight:bold; }}"
        )
        body.addWidget(self.table)

        # ── Buttons: Close only ──────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_close = make_button("Close", _COL_CLOSE, 80)
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)
        body.addLayout(btn_row)

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        rows = self.db.get_all_videos()
        self.table.setRowCount(len(rows))
        mono = QtGui.QFont("Kode Mono")

        for row_idx, (video_id, title, from_callsign, target, date, url) in enumerate(rows):
            play_btn = make_button("▶", _COL_PLAY, 40)
            play_btn.clicked.connect(lambda _checked, u=url: self._on_play(u))
            play_cell = QWidget()
            play_cell.setStyleSheet("background-color: transparent;")
            play_layout = QHBoxLayout(play_cell)
            play_layout.setContentsMargins(6, 4, 10, 4)
            play_layout.addWidget(play_btn)
            self.table.setCellWidget(row_idx, 0, play_cell)

            for col, val in ((1, title), (2, from_callsign), (3, target), (4, date)):
                item = QTableWidgetItem(val)
                item.setFont(mono)
                item.setFlags(Qt.ItemIsEnabled)
                self.table.setItem(row_idx, col, item)

            delete_btn = make_button("Delete", _COL_DELETE, 70)
            delete_btn.clicked.connect(lambda _checked, vid=video_id, t=title: self._on_delete(vid, t))
            delete_cell = QWidget()
            delete_cell.setStyleSheet("background-color: transparent;")
            delete_layout = QHBoxLayout(delete_cell)
            delete_layout.setContentsMargins(6, 4, 10, 4)
            delete_layout.addWidget(delete_btn)
            self.table.setCellWidget(row_idx, 5, delete_cell)

    # ── Actions ────────────────────────────────────────────────────────────────

    def _on_play(self, url: str) -> None:
        """Open the video's URL in the OS browser, unless Off-Grid Mode is on."""
        import netguard
        if not netguard.guard('"Play Video" link'):
            QMessageBox.information(
                self, "Off-Grid Mode",
                "\"Play Video\" opens an external website and is disabled while"
                " Off-Grid Mode is on.\n\nSwitch back to ONLINE in the header to use it."
            )
            return
        QDesktopServices.openUrl(QUrl(url))

    def _on_delete(self, video_id: int, title: str) -> None:
        if not confirm(self, "Delete Video", f"Delete \"{title}\"?", no_label="Cancel"):
            return
        self.db.delete_video(video_id)
        self._load()
