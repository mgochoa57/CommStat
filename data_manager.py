# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
data_manager.py - Data Manager Dialog

Shows row-count/oldest-record stats for the four CommStat record tables
(Alerts, Messages, Status Reports, Videos) and a form to bulk-delete rows
older than N days from one of them.
"""

from datetime import datetime, timezone

from PyQt5 import QtGui, QtWidgets
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView,
)

from constants import DEFAULT_COLORS, COLOR_BTN_GREEN, COLOR_INPUT_TEXT, COLOR_INPUT_BORDER
from ui_helpers import make_button, make_combobox, confirm, apply_standard_dialog_chrome

# ── Constants ──────────────────────────────────────────────────────────────────

_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_TITLE_BG = DEFAULT_COLORS.get("title_bar_background", "#F07800")
_TITLE_FG = DEFAULT_COLORS.get("title_bar_foreground", "#FFFFFF")
_DATA_BG  = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG  = DEFAULT_COLORS.get("data_foreground",      "#000000")

_COL_RUN   = COLOR_BTN_GREEN
_COL_CLOSE = "#555555"

_WIN_W = 560
_WIN_H = 340

_TABLE_COLS = ["Table Name", "Row Count", "Oldest Record", "Days Old"]
_RECORD_TABLES = [
    ("Alerts",         "alerts"),
    ("Messages",       "messages"),
    ("Status Reports", "statrep"),
    ("Videos",         "videos"),
]


# ── Dialog ─────────────────────────────────────────────────────────────────────

class DataManagerDialog(QDialog):
    """Data Manager dialog — table stats plus an older-than-N-days purge form."""

    def __init__(self, db_manager, parent=None):
        super().__init__(parent)
        self.db = db_manager

        apply_standard_dialog_chrome(self, "Data Manager", _WIN_W, _WIN_H)

        self._setup_ui()
        self._load_summary()

        # The table's height is computed from font metrics at runtime (see
        # _setup_ui), so re-fit the fixed dialog height to the actual layout
        # instead of trusting the _WIN_H guess above.
        self.setFixedHeight(self.layout().sizeHint().height())

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
        title_lbl = QLabel("Data Manager")
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title_lbl.setFixedHeight(36)
        title_lbl.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        body.addWidget(title_lbl)

        # ── Summary table ─────────────────────────────────────────────────────
        self.table = QTableWidget(len(_RECORD_TABLES), len(_TABLE_COLS))
        self.table.setHorizontalHeaderLabels(_TABLE_COLS)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(False)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)

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

        # Size the table to exactly fit its header + fixed row count, then pad
        # 10px so Qt doesn't sprout a vertical scrollbar over a few stray
        # pixels of rounding.
        row_h = self.table.verticalHeader().defaultSectionSize()
        header_h = self.table.horizontalHeader().sizeHint().height()
        frame = 2 * self.table.frameWidth()
        self.table.setFixedHeight(header_h + row_h * len(_RECORD_TABLES) + frame + 1)

        # ── Purge form ────────────────────────────────────────────────────────
        form_row = QHBoxLayout()
        form_row.setSpacing(6)

        form_row.addWidget(QLabel("Delete all"))

        self.table_combo = make_combobox(_RECORD_TABLES)
        form_row.addWidget(self.table_combo)

        form_row.addWidget(QLabel("that are more than"))

        self.days_spin = QSpinBox()
        self.days_spin.setRange(0, 3650)
        self.days_spin.setValue(30)
        self.days_spin.setMinimumHeight(30)
        self.days_spin.setStyleSheet(
            f"QSpinBox {{ background-color:white; color:{COLOR_INPUT_TEXT}; border:1px solid {COLOR_INPUT_BORDER};"
            f" border-radius:4px; padding:2px 6px; font-family:'Kode Mono'; font-size:13px; }}"
        )
        form_row.addWidget(self.days_spin)

        form_row.addWidget(QLabel("days old."))
        form_row.addStretch()

        self.btn_run = make_button("Run", _COL_RUN, 70)
        self.btn_run.clicked.connect(self._on_run)
        form_row.addWidget(self.btn_run)

        body.addLayout(form_row)

        # ── Close ─────────────────────────────────────────────────────────────
        close_row = QHBoxLayout()
        close_row.addStretch()
        self.btn_close = make_button("Close", _COL_CLOSE, 80)
        self.btn_close.clicked.connect(self.accept)
        close_row.addWidget(self.btn_close)
        body.addLayout(close_row)

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load_summary(self) -> None:
        mono = QtGui.QFont("Kode Mono")
        now = datetime.now(timezone.utc)
        for row, (label, table_key) in enumerate(_RECORD_TABLES):
            count, oldest = self.db.get_table_stats(table_key)
            oldest_date = "—"
            days_old = "—"
            if oldest:
                oldest_date = oldest.split(" ")[0]
                try:
                    oldest_dt = datetime.strptime(oldest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    days_old = str((now - oldest_dt).days)
                except ValueError:
                    pass
            values = [label, str(count), oldest_date, days_old]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setFont(mono)
                item.setFlags(Qt.ItemIsEnabled)
                if col > 0:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, item)

    # ── Actions ────────────────────────────────────────────────────────────────

    def _on_run(self) -> None:
        table_key = self.table_combo.currentData()
        label = self.table_combo.currentText()
        days = self.days_spin.value()

        if not confirm(
            self, "Data Manager",
            f"Delete all {label} records older than {days} day(s)? This cannot be undone.",
            no_label="Cancel"
        ):
            return

        deleted = self.db.delete_rows_older_than(table_key, days)
        QMessageBox.information(self, "Data Manager", f"Deleted {deleted} record(s).")
        self._load_summary()


if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication(sys.argv)
    print("This dialog requires a DatabaseManager instance.")
    sys.exit(1)
