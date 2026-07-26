# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
media.py - Share Media Dialog

Allows sharing a YouTube video link via the commstat.app server (internet only).
"""

import base64
import re
import sqlite3
import sys
import threading
import urllib.parse
import urllib.request
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QDateTime
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox,
    QMessageBox,
)

from constants import DEFAULT_COLORS, COLOR_BTN_BLUE, COLOR_BTN_CYAN
from little_gucci import create_verified_ssl_context
from ui_helpers import make_button, label_font, apply_standard_dialog_chrome, connect_single


# =============================================================================
# Constants
# =============================================================================

MAX_TITLE_LENGTH = 100
MAX_URL_LENGTH   = 200
DATABASE_FILE    = "traffic.db3"

_COMMSRVR = base64.b64decode("aHR0cHM6Ly9jb21tc3RhdC5hcHA=").decode()
_DATAFEED = _COMMSRVR + "/datafeed-808585.php"

_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")

_COL_CANCEL = "#555555"

_WIN_W = 640
_WIN_H = 340

_YOUTUBE_ID_RE = re.compile(
    r'(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?v=|embed/|shorts/))'
    r'([A-Za-z0-9_-]{11})'
)


def _extract_youtube_id(url: str) -> Optional[str]:
    """Pull the 11-char video id out of a YouTube watch/share/embed URL."""
    match = _YOUTUBE_ID_RE.search(url or "")
    return match.group(1) if match else None


# =============================================================================
# Helpers
# =============================================================================

def make_uppercase(field: QLineEdit) -> None:
    def to_upper(text):
        if text != text.upper():
            pos = field.cursorPosition()
            field.setText(text.upper())
            field.setCursorPosition(pos)
    field.textEdited.connect(to_upper)


_NON_PRINTABLE_RE = re.compile(r"[^ -~]+")


class _SanitizedLineEdit(QLineEdit):
    """QLineEdit that strips non-printable-ASCII chars (and leading/trailing
    whitespace) from pasted text before it's inserted."""

    def insertFromMimeData(self, source: QtCore.QMimeData) -> None:
        text = _NON_PRINTABLE_RE.sub(" ", source.text()).strip()
        if text:
            self.insert(text)


# =============================================================================
# Dialog
# =============================================================================

class MediaDialog(QDialog):
    """Share YouTube Video dialog — post a YouTube link via the commstat.app server."""

    _commsrvr_result = QtCore.pyqtSignal(str)

    def __init__(
        self,
        on_media_saved: callable = None,
        parent=None,
    ):
        super().__init__(parent)
        self.on_media_saved     = on_media_saved
        self.callsign: str      = ""
        self.selected_group: str = ""
        self._pending_save_data = None

        self._commsrvr_result.connect(self._on_commsrvr_result)

        apply_standard_dialog_chrome(self, "Share YouTube Video", _WIN_W, _WIN_H)

        self._setup_ui()
        self._load_config()

        self.group_combo.currentTextChanged.connect(self._on_group_changed)
        self.target_call_field.textChanged.connect(self._on_target_callsign_changed)
        make_uppercase(self.target_call_field)

    # =========================================================================
    # UI Construction
    # =========================================================================

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; }}"
            f"QLabel {{ font-family:Roboto; font-size:13px; color:{_PANEL_FG}; }}"
            f"QLineEdit {{ background-color:white; color:#333333; border:1px solid #cccccc;"
            f" border-radius:4px; padding:2px 6px; font-family:'Kode Mono'; font-size:13px; }}"
            f"QLineEdit:focus {{ border:1px solid #007bff; }}"
            f"QComboBox {{ background-color:white; color:#333333; border:1px solid #cccccc;"
            f" border-radius:4px; padding:2px 4px; font-family:'Kode Mono'; font-size:13px;"
            f" combobox-popup:0; }}"
            f"QComboBox QAbstractItemView {{ background-color:white; color:#333333;"
            f" selection-background-color:#cce5ff; selection-color:#000000; }}"
            f"QComboBox QAbstractItemView::item {{ min-height:22px; padding:0 6px; }}"
        )

        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        # ── Title ─────────────────────────────────────────────────────────────
        title_lbl = QLabel("Share YouTube Video")
        title_lbl.setAlignment(QtCore.Qt.AlignCenter)
        title_lbl.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title_lbl.setFixedHeight(36)
        title_lbl.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        body.addWidget(title_lbl)

        # ── Target ────────────────────────────────────────────────────────────
        target_lbl = QLabel("Target:")
        target_lbl.setFont(label_font())
        body.addWidget(target_lbl)

        target_row = QHBoxLayout()
        target_row.setSpacing(8)

        self.group_combo = QComboBox()
        self.group_combo.setMinimumWidth(150)
        self.group_combo.setMaxVisibleItems(30)
        self.group_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.group_combo))
        target_row.addWidget(self.group_combo)

        or_lbl = QLabel("OR Callsign")
        or_lbl.setFont(label_font())
        target_row.addWidget(or_lbl)

        self.target_call_field = QLineEdit()
        self.target_call_field.setMaxLength(12)
        self.target_call_field.setPlaceholderText("e.g. N0CALL")
        self.target_call_field.setFixedWidth(150)
        target_row.addWidget(self.target_call_field)
        target_row.addStretch()
        body.addLayout(target_row)

        # ── Title field ───────────────────────────────────────────────────────
        title_input_lbl = QLabel("Title:")
        title_input_lbl.setFont(label_font())
        body.addWidget(title_input_lbl)

        self.title_field = _SanitizedLineEdit()
        self.title_field.setMaxLength(MAX_TITLE_LENGTH)
        self.title_field.setPlaceholderText(f"{MAX_TITLE_LENGTH} characters max")
        body.addWidget(self.title_field)

        # ── URL field ─────────────────────────────────────────────────────────
        url_input_lbl = QLabel("URL:")
        url_input_lbl.setFont(label_font())
        body.addWidget(url_input_lbl)

        self.url_field = QLineEdit()
        self.url_field.setMaxLength(MAX_URL_LENGTH)
        self.url_field.setPlaceholderText(f"{MAX_URL_LENGTH} characters max")
        body.addWidget(self.url_field)

        body.addStretch()

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        self.save_button = make_button("Save Only", COLOR_BTN_CYAN, min_w=100)
        self.save_button.clicked.connect(self._save_only)
        btn_row.addWidget(self.save_button)

        self.transmit_button = make_button("Transmit", COLOR_BTN_BLUE, min_w=100)
        connect_single(self.transmit_button, self._transmit)
        btn_row.addWidget(self.transmit_button)

        self.cancel_button = make_button("Cancel", _COL_CANCEL, min_w=100)
        self.cancel_button.clicked.connect(self.close)
        btn_row.addWidget(self.cancel_button)

        body.addLayout(btn_row)

    # =========================================================================
    # Config / DB
    # =========================================================================

    def _load_config(self) -> None:
        self.selected_group = self._get_active_group_from_db()
        all_groups = self._get_all_groups_from_db()
        self.group_combo.addItem("")
        for group in all_groups:
            self.group_combo.addItem(group)

    def _get_active_group_from_db(self) -> str:
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM groups ORDER BY name LIMIT 1")
                result = cursor.fetchone()
                if result:
                    return result[0]
        except sqlite3.Error as e:
            print(f"Error reading active group from database: {e}")
        return ""

    def _get_all_groups_from_db(self) -> list:
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM groups ORDER BY name")
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            print(f"Error reading groups from database: {e}")
        return []

    def _get_internet_user_settings(self) -> tuple:
        """Return (callsign, gridsquare, state) from User Settings."""
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT callsign, gridsquare, state FROM controls WHERE id = 1")
                row = cursor.fetchone()
                if row:
                    return (
                        (row[0] or "").strip().upper(),
                        (row[1] or "").strip(),
                        (row[2] or "").strip().upper(),
                    )
        except sqlite3.Error:
            pass
        return ("", "", "")

    def _on_group_changed(self, group: str) -> None:
        if group:
            self.target_call_field.blockSignals(True)
            self.target_call_field.clear()
            self.target_call_field.blockSignals(False)

    def _on_target_callsign_changed(self, text: str) -> None:
        if text:
            self.group_combo.blockSignals(True)
            self.group_combo.setCurrentIndex(0)
            self.group_combo.blockSignals(False)

    def _get_target(self) -> str:
        call_target = self.target_call_field.text().strip().upper()
        if call_target:
            return call_target
        group = self.group_combo.currentText()
        if group:
            return "@" + group
        return ""

    def _show_error(self, message: str) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("CommStat Error")
        msg.setText(message)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _on_commsrvr_result(self, result: str) -> None:
        if result.startswith("ERR::"):
            from qrz_lookup import InternetDeliveryFailureDialog
            parent = self if self.isVisible() else (self.parent() or self)
            InternetDeliveryFailureDialog(result[5:], parent=parent).exec_()
        elif result.isdigit():
            global_id = int(result)
            self._save_to_database(global_id)
            self.close()
            if self.on_media_saved:
                self.on_media_saved()

    # =========================================================================
    # Validation / message building
    # =========================================================================

    def _validate_input(self) -> Optional[tuple]:
        if not self._get_target():
            self._show_error("Please select a Group or enter a Target Callsign")
            self.group_combo.setFocus()
            return None

        title = re.sub(r"[^ -~]+", " ", self.title_field.text()).strip()
        if len(title) < 1:
            self._show_error("Title is required")
            self.title_field.setFocus()
            return None

        url = re.sub(r"[^ -~]+", " ", self.url_field.text()).strip()
        if len(url) < 1:
            self._show_error("URL is required")
            self.url_field.setFocus()
            return None

        if not _extract_youtube_id(url):
            self._show_error("URL must be a valid YouTube video link")
            self.url_field.setFocus()
            return None

        callsign, grid, state = self._get_internet_user_settings()
        if not callsign or not grid or not state:
            self._show_error(
                "Cannot transmit — User Settings are not fully configured.\n\n"
                "Please set your callsign, grid square, and state at:\n"
                "Menu → Config → User Settings"
            )
            return None

        return (callsign, title, url)

    def _build_message(self, callsign: str, title: str, url: str) -> str:
        target = self._get_target()
        return f"{callsign}: {target} {{{title}}}{{{url}}}{{&&}}"

    # =========================================================================
    # DB save (backbone global_id pattern)
    # =========================================================================

    def _capture_save_data(self, callsign: str, title: str, url: str) -> dict:
        now = QDateTime.currentDateTime()
        return {
            'callsign': callsign,
            'target': self._get_target(),
            'title': title,
            'url': url,
            'datetime': now.toUTC().toString("yyyy-MM-dd HH:mm:ss"),
            'date': now.toUTC().toString("yyyy-MM-dd"),
        }

    def _save_to_database(self, global_id: int = 0) -> None:
        d = self._pending_save_data
        with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
            conn.execute(
                "INSERT INTO media "
                "(global_id, datetime, date, from_callsign, target, title, url, played) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 0)",
                (global_id, d['datetime'], d['date'], d['callsign'], d['target'], d['title'], d['url'])
            )
            conn.commit()

    # =========================================================================
    # Commsrvr submission
    # =========================================================================

    def _submit_to_commsrvr_async(self) -> None:
        d = self._pending_save_data
        callsign = d['callsign']
        message = self._build_message(d['callsign'], d['title'], d['url'])
        now = d['datetime']

        def submit_thread():
            try:
                # freq/0/db are unused placeholders — kept only so the incoming
                # heartbeat feed's shared 6-field envelope parser (little_gucci.py
                # _handle_commsrvr_data_messages) can split this line the same way
                # it splits alert/statrep/message lines.
                data_string = f"{now}\t0\t0\t0\t{message}"
                post_data = urllib.parse.urlencode({
                    'cs': callsign, 'data': data_string
                }).encode('utf-8')
                req = urllib.request.Request(_DATAFEED, data=post_data, method='POST')
                with urllib.request.urlopen(req, timeout=5, context=create_verified_ssl_context()) as response:
                    result = response.read().decode('utf-8').strip()
                if result.isdigit():
                    print(f"[Commsrvr] Media submitted successfully (global_id={result})")
                else:
                    print(f"[Commsrvr] Media submission failed — server returned: {result}")
                self._commsrvr_result.emit(result)
            except Exception as e:
                reason = getattr(e, 'reason', e)
                if isinstance(reason, TimeoutError):
                    err = "ERR::Server timeout — the server did not respond in time."
                else:
                    err = f"ERR::Connection error — {e}"
                print(f"[Commsrvr] Media submission failed — {err[5:]}")
                self._commsrvr_result.emit(err)

        threading.Thread(target=submit_thread, daemon=True).start()

    # =========================================================================
    # Button handlers
    # =========================================================================

    def _save_only(self) -> None:
        result = self._validate_input()
        if result is None:
            return
        callsign, title, url = result
        self._pending_save_data = self._capture_save_data(callsign, title, url)
        self._save_to_database(0)
        self.close()
        if self.on_media_saved:
            self.on_media_saved()

    def _transmit(self) -> None:
        result = self._validate_input()
        if result is None:
            return
        callsign, title, url = result
        self._pending_save_data = self._capture_save_data(callsign, title, url)
        self._submit_to_commsrvr_async()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    dlg = MediaDialog()
    dlg.exec_()
    sys.exit(app.exec_())
