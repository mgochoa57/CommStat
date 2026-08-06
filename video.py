# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
video.py - Share Video Dialog

Allows sharing a YouTube video link via the commstat.app server (internet only).
"""

import base64
import json
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
_TITLE_PLACEHOLDER = "Fills in from the URL — or type your own"
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


_NON_PRINTABLE_RE = re.compile(r"[^ -~]+")


# YouTube's public oEmbed endpoint returns a video's title as JSON with no API
# key and no quota. 404s on a deleted/private/nonexistent id.
_OEMBED_URL = "https://www.youtube.com/oembed"


def _clean_fetched_title(raw: str) -> str:
    """Make a YouTube title safe for the {title}{url}{&&} wire format.

    Braces become parentheses: the receiving parser
    (little_gucci._parse_video) splits on '}{', so a title carrying that
    sequence would corrupt the record. Non-printable-ASCII (emoji, smart
    quotes, em-dashes — common in real titles) collapses to spaces, matching
    what _validate_input already does to typed titles.
    """
    text = (raw or "").replace("{", "(").replace("}", ")")
    text = _NON_PRINTABLE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_TITLE_LENGTH].strip()


def fetch_youtube_title(video_id: str, timeout: int = 6) -> str:
    """Return the video's title, or "" if it can't be retrieved.
    Blocking — call from a worker thread."""
    try:
        query = urllib.parse.urlencode({
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "format": "json",
        })
        with urllib.request.urlopen(
            f"{_OEMBED_URL}?{query}", timeout=timeout,
            context=create_verified_ssl_context()
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        return _clean_fetched_title(data.get("title", ""))
    except Exception as e:
        print(f"[YouTube] Title lookup failed for {video_id} — {e}")
        return ""


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

class VideoDialog(QDialog):
    """Share YouTube Video dialog — post a YouTube link via the commstat.app server."""

    _commsrvr_result = QtCore.pyqtSignal(str)
    # (video_id, title) — video_id lets a stale reply be discarded if the URL
    # changed while the lookup was in flight.
    _title_fetched = QtCore.pyqtSignal(str, str)

    def __init__(
        self,
        on_video_saved: callable = None,
        parent=None,
    ):
        super().__init__(parent)
        self.on_video_saved     = on_video_saved
        self.callsign: str      = ""
        self.selected_group: str = ""
        self._pending_save_data = None
        self._internet_available = bool(parent and getattr(parent, '_internet_available', True))

        self._commsrvr_result.connect(self._on_commsrvr_result)
        self._title_fetched.connect(self._on_title_fetched)

        # Auto-title state: which id we last looked up, and whether the
        # operator has typed their own title (which auto-fill must not clobber).
        self._title_fetch_id: str = ""
        self._title_user_edited: bool = False

        apply_standard_dialog_chrome(self, "Share YouTube Video", _WIN_W, _WIN_H)

        self._setup_ui()
        self._load_config()

        self.group_combo.currentTextChanged.connect(self._on_group_changed)
        self.target_call_field.textChanged.connect(self._on_target_callsign_changed)
        make_uppercase(self.target_call_field)

        # Look the title up a beat after typing/pasting settles, so a pasted
        # URL doesn't fire a request per character.
        self._title_lookup_timer = QtCore.QTimer(self)
        self._title_lookup_timer.setSingleShot(True)
        self._title_lookup_timer.setInterval(400)
        self._title_lookup_timer.timeout.connect(self._start_title_lookup)
        self.url_field.textChanged.connect(lambda _t: self._title_lookup_timer.start())
        # textEdited fires only for real typing, not setText() — so auto-fill
        # never marks itself as a manual edit.
        self.title_field.textEdited.connect(self._on_title_edited)

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

        # ── URL field ─────────────────────────────────────────────────────────
        # URL comes first: it's the only thing the operator has to supply, and
        # the Title below fills itself in from it.
        url_input_lbl = QLabel("URL:")
        url_input_lbl.setFont(label_font())
        body.addWidget(url_input_lbl)

        self.url_field = QLineEdit()
        self.url_field.setMaxLength(MAX_URL_LENGTH)
        self.url_field.setPlaceholderText(f"{MAX_URL_LENGTH} characters max")
        body.addWidget(self.url_field)

        # ── Title field ───────────────────────────────────────────────────────
        title_input_lbl = QLabel("Title:")
        title_input_lbl.setFont(label_font())
        body.addWidget(title_input_lbl)

        self.title_field = _SanitizedLineEdit()
        self.title_field.setMaxLength(MAX_TITLE_LENGTH)
        self.title_field.setPlaceholderText(_TITLE_PLACEHOLDER)
        body.addWidget(self.title_field)

        body.addStretch()

        if not self._internet_available:
            self.group_combo.setEnabled(False)
            self.target_call_field.setEnabled(False)
            self.title_field.setEnabled(False)
            self.url_field.setEnabled(False)

        # ── Buttons ───────────────────────────────────────────────────────────
        self.cancel_button = make_button("Cancel", _COL_CANCEL, min_w=100)
        self.cancel_button.clicked.connect(self.close)

        if self._internet_available:
            self.save_button = make_button("Save Only", COLOR_BTN_CYAN, min_w=100)
            self.save_button.clicked.connect(self._save_only)

            self.transmit_button = make_button("Transmit", COLOR_BTN_BLUE, min_w=100)
            connect_single(self.transmit_button, self._transmit)

            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)
            btn_row.addStretch()
            btn_row.addWidget(self.save_button)
            btn_row.addWidget(self.transmit_button)
            btn_row.addWidget(self.cancel_button)
            body.addLayout(btn_row)
        else:
            no_inet = QLabel("No Internet Connection  ·  Video Sharing Unavailable")
            no_inet.setAlignment(QtCore.Qt.AlignCenter)
            no_inet.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
            no_inet.setFixedHeight(36)
            no_inet.setStyleSheet(
                f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
                " font-size:16px; padding-top:9px; padding-bottom:9px; }"
            )
            no_inet_row = QHBoxLayout()
            no_inet_row.setSpacing(8)
            no_inet_row.addWidget(no_inet, 1)
            no_inet_row.addWidget(self.cancel_button)
            body.addLayout(no_inet_row)

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
            if self.on_video_saved:
                self.on_video_saved()

    # =========================================================================
    # Auto title lookup
    # =========================================================================

    def _on_title_edited(self, text: str) -> None:
        """Typing your own title wins over auto-fill. Emptying the box re-arms
        auto-fill and re-runs the lookup, so clearing it brings the real
        YouTube title back without having to re-paste the URL."""
        self._title_user_edited = bool(text.strip())
        if not self._title_user_edited:
            self._title_fetch_id = ""
            self._title_lookup_timer.start()

    def _start_title_lookup(self) -> None:
        """Kick off a background title lookup for the URL currently entered."""
        if not self._internet_available:
            return
        video_id = _extract_youtube_id(self.url_field.text().strip())
        if not video_id or video_id == self._title_fetch_id:
            return
        if self._title_user_edited:
            return
        self._title_fetch_id = video_id
        self.title_field.setPlaceholderText("Fetching title…")

        def lookup_thread(vid=video_id):
            self._title_fetched.emit(vid, fetch_youtube_title(vid))

        threading.Thread(target=lookup_thread, daemon=True).start()

    def _on_title_fetched(self, video_id: str, title: str) -> None:
        """Fill in a fetched title. The field stays editable — the operator can
        reword it before sending."""
        if video_id != self._title_fetch_id:
            return          # a newer URL was entered while this was in flight
        self.title_field.setPlaceholderText(_TITLE_PLACEHOLDER)
        if not title:
            # Leave the box empty and editable so the video can still be shared.
            return
        if self._title_user_edited:
            return
        self.title_field.setText(title)

    # =========================================================================
    # Validation / message building
    # =========================================================================

    def _validate_input(self) -> Optional[tuple]:
        if not self._get_target():
            self._show_error("Please select a Group or enter a Target Callsign")
            self.group_combo.setFocus()
            return None

        # URL is validated first — it's the field the operator fills in, and
        # the title is derived from it.
        url = re.sub(r"[^ -~]+", " ", self.url_field.text()).strip()
        if len(url) < 1:
            self._show_error("URL is required")
            self.url_field.setFocus()
            return None

        if not _extract_youtube_id(url):
            self._show_error("URL must be a valid YouTube video link")
            self.url_field.setFocus()
            return None

        title = re.sub(r"[^ -~]+", " ", self.title_field.text()).strip()
        if len(title) < 1:
            self._show_error("Title is required")
            self.title_field.setFocus()
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
                "INSERT INTO videos "
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
                    print(f"[Commsrvr] Video submitted successfully (global_id={result})")
                else:
                    print(f"[Commsrvr] Video submission failed — server returned: {result}")
                self._commsrvr_result.emit(result)
            except Exception as e:
                reason = getattr(e, 'reason', e)
                if isinstance(reason, TimeoutError):
                    err = "ERR::Server timeout — the server did not respond in time."
                else:
                    err = f"ERR::Connection error — {e}"
                print(f"[Commsrvr] Video submission failed — {err[5:]}")
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
        if self.on_video_saved:
            self.on_video_saved()

    def _transmit(self) -> None:
        result = self._validate_input()
        if result is None:
            return
        callsign, title, url = result
        self._pending_save_data = self._capture_save_data(callsign, title, url)
        self._submit_to_commsrvr_async()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    dlg = VideoDialog()
    dlg.exec_()
    sys.exit(app.exec_())
