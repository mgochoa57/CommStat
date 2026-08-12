# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
group_members.py - Group Members Dialog

Lists the member callsigns of one group with Add and Remove actions.
Adding searches the local qrz table first, then the QRZ.com XML API; a
callsign found nowhere can be entered manually. Members render on the map
as persistent pink halo pins when Filter -> Member Pins is checked.
"""

from PyQt5 import QtGui, QtWidgets
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QWidget,
)

from constants import (
    DEFAULT_COLORS,
    COLOR_BTN_GREEN, COLOR_BTN_BLUE, COLOR_BTN_CLOSE,
    COLOR_DISABLED_BG, COLOR_DISABLED_TEXT,
    COLOR_INPUT_TEXT, COLOR_INPUT_BORDER, FONT_MONO_STACK,
)
from ui_helpers import make_button, make_input, confirm, apply_standard_dialog_chrome
from qrz_client import get_qrz_cached, load_qrz_config

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
_COL_REMOVE = "#dc3545"

_WIN_W = 640
_WIN_H = 480

_TABLE_COLS = ["Callsign", "Name", "City", "State", "Grid"]

# QThreads started for a lookup are parked here so closing the dialog
# mid-lookup never destroys a still-running QThread.
_live_threads = set()


def _field_style(read_only: bool) -> str:
    bg = COLOR_DISABLED_BG if read_only else "white"
    fg = COLOR_DISABLED_TEXT if read_only else COLOR_INPUT_TEXT
    return (
        f"QLineEdit {{ background-color:{bg}; color:{fg}; border:1px solid {COLOR_INPUT_BORDER};"
        f" border-radius:4px; padding:2px 6px; font-family:{FONT_MONO_STACK}; font-size:13px; }}"
        f"QLineEdit:focus {{ border:1px solid {COLOR_BTN_BLUE}; }}"
    )


# ── Dialog ─────────────────────────────────────────────────────────────────────

class GroupMembersDialog(QDialog):
    """Group Members dialog — list, add, and remove member callsigns for a group."""

    def __init__(self, db_manager, group_id: int, group_name: str, parent=None):
        super().__init__(parent)
        self.db = db_manager
        self.group_id = group_id
        self.group_name = group_name

        self._thread = None
        self._pending_cs: str = ""
        self._manual_mode: bool = False

        apply_standard_dialog_chrome(self, f"@{group_name} Members", _WIN_W, _WIN_H)

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
        title_lbl = QLabel(f"@{self.group_name} Members")
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title_lbl.setFixedHeight(36)
        title_lbl.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        body.addWidget(title_lbl)

        # ── Add panel (hidden until Add is clicked) ───────────────────────────
        self.add_panel = QWidget()
        panel_layout = QVBoxLayout(self.add_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        # Row A: callsign input + Search
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(QLabel("Callsign:"))

        # Deferred import: this dialog is only constructed from groups.py at
        # runtime, once little_gucci is fully loaded in sys.modules.
        from little_gucci import UpperCaseLineEdit
        self.cs_edit = UpperCaseLineEdit()
        self.cs_edit.setPlaceholderText("Callsign")
        self.cs_edit.setMaxLength(12)
        self.cs_edit.setMinimumHeight(30)
        self.cs_edit.setFixedWidth(140)
        self.cs_edit.setStyleSheet(_field_style(read_only=False))
        self.cs_edit.returnPressed.connect(self._on_search)
        search_row.addWidget(self.cs_edit)

        self.btn_search = make_button("Search", COLOR_BTN_BLUE, 80)
        self.btn_search.clicked.connect(self._on_search)
        search_row.addWidget(self.btn_search)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("QLabel { font-family:Roboto; font-size:12px; }")
        search_row.addWidget(self.lbl_status)
        search_row.addStretch()
        panel_layout.addLayout(search_row)

        # Row B: detail fields + Save/Cancel (hidden until a search resolves)
        self.detail_row = QWidget()
        detail_grid = QGridLayout(self.detail_row)
        detail_grid.setContentsMargins(0, 0, 0, 0)
        detail_grid.setHorizontalSpacing(8)
        detail_grid.setVerticalSpacing(2)

        self.f_callsign = make_input(max_len=12)
        self.f_name     = make_input(max_len=40)
        self.f_city     = make_input(max_len=30)
        self.f_state    = make_input(max_len=2)
        self.f_grid     = make_input(max_len=8)
        self._fields = [self.f_callsign, self.f_name, self.f_city, self.f_state, self.f_grid]

        self.btn_save_member = make_button("Save", COLOR_BTN_GREEN, 80)
        self.btn_save_member.clicked.connect(self._on_save)
        self.btn_cancel_add = make_button("Cancel", COLOR_BTN_CLOSE, 80)
        self.btn_cancel_add.clicked.connect(self._reset_add_panel)

        for col, (label, field) in enumerate(zip(_TABLE_COLS, self._fields)):
            detail_grid.addWidget(QLabel(label), 0, col)
            detail_grid.addWidget(field, 1, col)
        detail_grid.addWidget(self.btn_save_member, 1, 5)
        detail_grid.addWidget(self.btn_cancel_add, 1, 6)
        detail_grid.setColumnStretch(1, 2)   # Name
        detail_grid.setColumnStretch(2, 2)   # City
        self.detail_row.setVisible(False)
        panel_layout.addWidget(self.detail_row)

        self.add_panel.setVisible(False)
        body.addWidget(self.add_panel)

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

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)

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
        body.addWidget(self.table)

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_add    = make_button("Add",    _COL_ADD,        80)
        self.btn_remove = make_button("Remove", _COL_REMOVE,     80)
        self.btn_close  = make_button("Close",  COLOR_BTN_CLOSE, 80)

        self.btn_remove.setEnabled(False)

        self.btn_add.clicked.connect(self._on_add)
        self.btn_remove.clicked.connect(self._on_remove)
        self.btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_close)
        body.addLayout(btn_row)

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        members = self.db.get_group_members(self.group_id)
        self.table.setRowCount(0)
        mono = QtGui.QFont("Kode Mono")

        for m in members:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [m["callsign"], m["name"], m["city"], m["state"], m["grid"]]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setFont(mono)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                if col == 0:
                    item.setData(Qt.UserRole, m["id"])
                self.table.setItem(row, col, item)

        self._on_selection_changed()

    # ── Selection ──────────────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        self.btn_remove.setEnabled(bool(self.table.selectedItems()))

    # ── Add / search flow ──────────────────────────────────────────────────────

    def _on_add(self) -> None:
        self.add_panel.setVisible(True)
        self._reset_add_panel()

    def _reset_add_panel(self) -> None:
        """Clear the detail fields and return to the callsign+Search row."""
        self._manual_mode = False
        self.detail_row.setVisible(False)
        for field in self._fields:
            field.clear()
        self.lbl_status.setText("")
        self.cs_edit.clear()
        self.cs_edit.setFocus()

    def _on_search(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        cs = self.cs_edit.text().strip().upper()
        if not cs:
            return
        # Captured before the thread starts; the result slot never re-reads
        # the widget for identity.
        self._pending_cs = cs
        self.detail_row.setVisible(False)

        # Local database first — stale and manually entered rows are still
        # valid member identities.
        cached = get_qrz_cached(cs, include_stale=True)
        if cached:
            self.lbl_status.setText(f"{cs} found in local database")
            self._show_found(cached)
            return

        is_active, username, password = load_qrz_config()
        if not username or not is_active:
            reason = "not configured" if not username else "not enabled"
            self.lbl_status.setText(f"QRZ Subscription {reason} — enter details manually")
            self._enter_manual_mode(cs)
            return

        self.lbl_status.setText(f"Looking up {cs}…")
        self.btn_search.setEnabled(False)
        from qrz_lookup import _QRZThread
        self._thread = _QRZThread(cs, username, password)
        _live_threads.add(self._thread)
        self._thread.finished.connect(
            lambda t=self._thread: _live_threads.discard(t)
        )
        self._thread.result_ready.connect(self._on_qrz_result)
        self._thread.start()

    def _on_qrz_result(self, result) -> None:
        self.btn_search.setEnabled(True)
        # Ignore the raw API dict's shape — on success lookup() has already
        # normalized and saved the record, so re-read the local cache.
        row = get_qrz_cached(self._pending_cs, include_stale=True)
        if row:
            self.lbl_status.setText(f"{self._pending_cs} found on QRZ.com")
            self._show_found(row)
        else:
            self.lbl_status.setText(
                f"{self._pending_cs} not found — enter details manually"
            )
            self._enter_manual_mode(self._pending_cs)

    def _show_found(self, row: dict) -> None:
        """Populate the detail fields read-only from a qrz table row."""
        self._manual_mode = False
        values = [
            row.get("callsign") or "", row.get("name") or "",
            row.get("city") or "", row.get("state") or "", row.get("grid") or "",
        ]
        for field, val in zip(self._fields, values):
            field.setText(val)
            field.setReadOnly(True)
            field.setStyleSheet(_field_style(read_only=True))
        self.detail_row.setVisible(True)

    def _enter_manual_mode(self, cs: str) -> None:
        """Let the user type the details for a callsign found nowhere."""
        self._manual_mode = True
        for field in self._fields:
            field.clear()
            field.setReadOnly(False)
            field.setStyleSheet(_field_style(read_only=False))
        self.f_callsign.setText(cs)
        self.f_callsign.setReadOnly(True)
        self.f_callsign.setStyleSheet(_field_style(read_only=True))
        self.detail_row.setVisible(True)
        self.f_name.setFocus()

    def _on_save(self) -> None:
        cs = self.f_callsign.text().strip().upper()
        if not cs:
            return
        if self._manual_mode:
            qrz_id = self.db.add_qrz_manual(
                cs,
                self.f_name.text(),
                self.f_city.text(),
                self.f_state.text(),
                self.f_grid.text(),
            )
        else:
            qrz_id = self.db.get_qrz_id(cs)
        if qrz_id is None:
            QMessageBox.critical(self, "Error", f"Could not save {cs}.")
            return
        if not self.db.add_group_member(self.group_id, qrz_id):
            QMessageBox.warning(
                self, "Members",
                f"{cs} is already a member of @{self.group_name}."
            )
        self._load()
        # Rapid entry: the callsign box and Search stay up for the next one.
        self._reset_add_panel()

    # ── Remove ─────────────────────────────────────────────────────────────────

    def _on_remove(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        cs = item.text()
        qrz_id = item.data(Qt.UserRole)
        if not confirm(self, "Remove Member",
                       f"Remove {cs} from @{self.group_name}?",
                       no_label="Cancel"):
            return
        if not self.db.remove_group_member(self.group_id, qrz_id):
            QMessageBox.critical(self, "Error", "Could not remove member.")
            return
        self._load()

    # ── Teardown ───────────────────────────────────────────────────────────────

    def done(self, result: int) -> None:
        # A lookup may still be in flight; detach it from this dialog and let
        # it finish on its own (kept alive by _live_threads).
        if self._thread is not None and self._thread.isRunning():
            try:
                self._thread.result_ready.disconnect(self._on_qrz_result)
            except TypeError:
                pass
        super().done(result)


if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication(sys.argv)
    print("This dialog requires a DatabaseManager instance.")
    sys.exit(1)
