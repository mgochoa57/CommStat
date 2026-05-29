# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
user_settings.py - User Settings Management Dialog

Displays the user's callsign, grid square, and state in a single-row table
with Add, Edit, and Delete actions.  At most one entry is allowed.
Editing is done inline directly in the table row.
"""

from typing import Optional

from PyQt5 import QtGui
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QWidget,
)

from constants import DEFAULT_COLORS
from ui_helpers import make_button, make_input, make_combobox, confirm, apply_standard_dialog_chrome

# ── Constants ──────────────────────────────────────────────────────────────────

_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_TITLE_BG = DEFAULT_COLORS.get("title_bar_background", "#F07800")
_TITLE_FG = DEFAULT_COLORS.get("title_bar_foreground", "#FFFFFF")
_DATA_BG  = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG  = DEFAULT_COLORS.get("data_foreground",      "#000000")

_COL_ADD    = "#28a745"
_COL_EDIT   = "#007bff"
_COL_DELETE = "#dc3545"
_COL_CLOSE  = "#555555"
_COL_SAVE   = "#28a745"
_COL_CANCEL = "#555555"

_WIN_W      = 680
_WIN_H      = 260

_TABLE_COLS = ["Callsign", "Grid Square", "State", "Default Map"]

_REGIONS = [
    ("United States",  "us"),
    ("European Union", "eu"),
    ("Middle East",    "mideast"),
    ("SE Asia",        "seasia"),
]
_REGION_LABEL = {v: k for k, v in _REGIONS}
_DEFAULT_REGION = "us"


# ── Dialog ─────────────────────────────────────────────────────────────────────

class UserSettingsDialog(QDialog):
    """User Settings dialog — callsign, grid square, and state; one entry max."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        self._in_edit_mode: bool = False
        self._edit_row: int = -1
        self._adding: bool = False
        self._iw_callsign: Optional[QLineEdit] = None
        self._iw_grid: Optional[QLineEdit] = None
        self._iw_state: Optional[QLineEdit] = None
        self._iw_region: Optional[QComboBox] = None

        apply_standard_dialog_chrome(self, "User Settings", _WIN_W, _WIN_H)

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
        title_lbl = QLabel("User Settings")
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
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(False)
        self.table.setMinimumHeight(100)
        self.table.setMaximumHeight(120)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)

        self.table.setStyleSheet(
            f"QTableWidget {{ background-color:{_DATA_BG}; alternate-background-color:{_DATA_BG};"
            f" gridline-color:#cccccc; color:{_DATA_FG};"
            f" font-family:'Kode Mono'; font-size:13px; }}"
            f"QTableWidget::item {{ padding:4px 6px; }}"
            f"QHeaderView::section {{ background-color:{_TITLE_BG}; color:{_TITLE_FG};"
            f" padding:5px 6px; border:none; font-family:Roboto; font-size:13px;"
            f" font-weight:bold; }}"
            f"QTableWidget::item:selected {{ background-color:#cce5ff; color:#000000; }}"
        )
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self._on_edit)
        body.addWidget(self.table)

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_add    = make_button("Add",    _COL_ADD,    80)
        self.btn_edit   = make_button("Edit",   _COL_EDIT,   80)
        self.btn_delete = make_button("Delete", _COL_DELETE, 80)
        self.btn_save   = make_button("Save",   _COL_SAVE,   80)
        self.btn_cancel = make_button("Cancel", _COL_CANCEL, 80)
        self.btn_close  = make_button("Close",  _COL_CLOSE,  80)

        self.btn_edit.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_save.setEnabled(False)

        self.btn_add.clicked.connect(self._on_add)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_save.clicked.connect(lambda: self._exit_edit_mode(save=True))
        self.btn_cancel.clicked.connect(lambda: self._exit_edit_mode(save=False))
        self.btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_edit)
        btn_row.addWidget(self.btn_delete)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_close)
        body.addLayout(btn_row)

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._in_edit_mode = False
        self._edit_row = -1
        callsign, grid, state = self.db.get_user_settings()
        region = self.db.get_default_map() or _DEFAULT_REGION
        region_label = _REGION_LABEL.get(region, _REGION_LABEL[_DEFAULT_REGION])
        self.table.setRowCount(0)
        mono = QtGui.QFont("Kode Mono")

        if callsign:
            self.table.insertRow(0)
            for col, val in enumerate([callsign, grid, state, region_label]):
                item = QTableWidgetItem(val)
                item.setFont(mono)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                self.table.setItem(0, col, item)

        self._on_selection_changed()

    # ── Selection ──────────────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        if self._in_edit_mode:
            return
        has_row = self.table.rowCount() > 0
        has_sel = bool(self.table.selectedItems())
        self.btn_add.setEnabled(not has_row)
        self.btn_edit.setEnabled(has_sel)
        self.btn_delete.setEnabled(has_sel)

    # ── Inline edit ────────────────────────────────────────────────────────────

    def _enter_edit_mode(self, row: int, adding: bool) -> None:
        self._in_edit_mode = True
        self._edit_row = row
        self._adding = adding

        callsign = "" if adding else (self.table.item(row, 0).text() if self.table.item(row, 0) else "")
        grid     = "" if adding else (self.table.item(row, 1).text() if self.table.item(row, 1) else "")
        state    = "" if adding else (self.table.item(row, 2).text() if self.table.item(row, 2) else "")

        # Region: always read from DB (the cell text is the human label, not the code)
        current_region = self.db.get_default_map() or _DEFAULT_REGION

        self._iw_callsign = make_input(placeholder="Your callsign", max_len=12)
        self._iw_callsign.setText(callsign)
        self._iw_callsign.textChanged.connect(
            lambda t: self._iw_callsign.setText(t.upper()) if t != t.upper() else None
        )
        self._iw_callsign.textChanged.connect(lambda _: self._on_inline_changed())

        self._iw_grid = make_input(placeholder="e.g. EM83cv", max_len=6)
        self._iw_grid.setText(grid)

        self._iw_state = make_input(placeholder="e.g. TX", max_len=2)
        self._iw_state.setText(state)
        self._iw_state.textChanged.connect(
            lambda t: self._iw_state.setText(t.upper()) if t != t.upper() else None
        )

        self._iw_region = make_combobox(_REGIONS)
        idx = self._iw_region.findData(current_region)
        if idx < 0:
            idx = self._iw_region.findData(_DEFAULT_REGION)
        self._iw_region.setCurrentIndex(max(idx, 0))

        # Qt force-stretches a QLineEdit installed via setCellWidget. Wrap each
        # input in a QWidget + HBoxLayout (input on left, stretch on right) so
        # the container fills the cell while the input keeps its fixed width.
        def _wrap_fixed(input_widget: QLineEdit, width_px: int) -> QWidget:
            input_widget.setFixedWidth(width_px)
            container = QWidget()
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            layout.addWidget(input_widget)
            layout.addStretch()
            return container

        self.table.setCellWidget(row, 0, _wrap_fixed(self._iw_callsign, 136))
        self.table.setCellWidget(row, 1, _wrap_fixed(self._iw_grid,      90))
        self.table.setCellWidget(row, 2, _wrap_fixed(self._iw_state,     40))
        self.table.setCellWidget(row, 3, self._iw_region)
        self.table.setRowHeight(row, 42)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)

        self.btn_add.setVisible(False)
        self.btn_edit.setVisible(False)
        self.btn_delete.setVisible(False)
        self.btn_save.setVisible(True)
        self.btn_cancel.setVisible(True)
        self.btn_close.setEnabled(False)

        QWidget.setTabOrder(self._iw_callsign, self._iw_grid)
        QWidget.setTabOrder(self._iw_grid, self._iw_state)
        QWidget.setTabOrder(self._iw_state, self._iw_region)
        QWidget.setTabOrder(self._iw_region, self.btn_save)

        self._on_inline_changed()
        self._iw_callsign.setFocus()

    def _on_inline_changed(self) -> None:
        if not self._in_edit_mode:
            return
        has_callsign = bool(self._iw_callsign and self._iw_callsign.text().strip())
        self.btn_save.setEnabled(has_callsign)

    def _exit_edit_mode(self, save: bool) -> None:
        row = self._edit_row

        if save:
            callsign = self._iw_callsign.text().strip().upper()
            if not callsign:
                QMessageBox.warning(self, "User Settings", "Callsign is required.")
                return
            raw_grid = self._iw_grid.text().strip()
            if len(raw_grid) == 6:
                grid = raw_grid[:2].upper() + raw_grid[2:4] + raw_grid[4:].lower()
            else:
                grid = raw_grid.upper()
            state = self._iw_state.text().strip().upper()
            region = self._iw_region.currentData() or _DEFAULT_REGION
            ok = self.db.set_user_settings(callsign, grid, state)
            if not ok:
                QMessageBox.critical(self, "Error", "Could not save user settings.")
                return
            self.db.set_default_map(region)

        for col in range(4):
            self.table.removeCellWidget(row, col)

        self._iw_callsign = self._iw_grid = self._iw_state = self._iw_region = None
        self._in_edit_mode = False

        self.table.setRowHeight(row, self.table.verticalHeader().defaultSectionSize())
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)

        self.btn_add.setVisible(True)
        self.btn_edit.setVisible(True)
        self.btn_delete.setVisible(True)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_close.setEnabled(True)

        self._load()

    # ── Actions ────────────────────────────────────────────────────────────────

    def _on_add(self) -> None:
        if self._in_edit_mode:
            return
        self.table.insertRow(0)
        self._enter_edit_mode(row=0, adding=True)

    def _on_edit(self) -> None:
        if self._in_edit_mode or self.table.rowCount() == 0:
            return
        self._enter_edit_mode(row=0, adding=False)

    def _on_delete(self) -> None:
        if not confirm(self, "Delete User Settings", "Delete user settings?",
                       no_label="Cancel"):
            return
        self.db.set_user_settings("", "", "")
        self.db.set_default_map(_DEFAULT_REGION)
        self._load()
