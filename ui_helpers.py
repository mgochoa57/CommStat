# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
ui_helpers.py - Shared UI helpers for CommStat dialogs.

Centralizes the styled QPushButton, QLineEdit, and centered-checkbox-cell
factories that were previously copy-pasted across every dialog module.
The canonical implementation comes from qrz_settings.py.
"""

import os
from typing import Tuple

from PyQt5 import QtGui
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QPushButton, QLineEdit, QCheckBox, QComboBox, QWidget, QHBoxLayout, QMessageBox,
    QDialog, QVBoxLayout, QLabel, QTextBrowser,
)

from constants import (
    FONT_ROBOTO, FONT_MONO, FONT_ROBOTO_STACK, FONT_MONO_STACK,
    COLOR_BTN_GREEN, COLOR_BTN_GRAY, COLOR_BTN_BLUE, COLOR_BTN_CLOSE,
    COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    DEFAULT_COLORS, ICON_FILE,
)


# ── Button ─────────────────────────────────────────────────────────────────────

def make_button(label: str, color: str, min_w: int = 90) -> QPushButton:
    """Standard styled action button: Roboto Bold 15px, colored background."""
    b = QPushButton(label)
    b.setMinimumWidth(min_w)
    b.setStyleSheet(
        f"QPushButton {{ background-color:{color}; color:#ffffff; border:none;"
        f" padding:6px 14px; border-radius:4px; font-family:{FONT_ROBOTO_STACK}; font-size:15px;"
        f" font-weight:bold; }}"
        f"QPushButton:hover {{ background-color:{color}; opacity:0.9; }}"
        f"QPushButton:pressed {{ background-color:{color}; }}"
        f"QPushButton:disabled {{ background-color:#cccccc; color:#888888; }}"
    )
    return b


def connect_single(button, handler, window_ms: int = 1000):
    """Connect button.clicked to handler so rapid repeat clicks (e.g. a
    double-click) fire the handler only once. A second click within window_ms
    is ignored. Does NOT touch setEnabled(), so it never interferes with
    validation- or mode-driven enable/disable logic."""
    state = {"busy": False}

    def _wrapped(*_args):
        if state["busy"]:
            return
        state["busy"] = True
        try:
            handler()
        finally:
            QTimer.singleShot(window_ms, lambda: state.update(busy=False))

    button.clicked.connect(_wrapped)
    return _wrapped


# ── Confirmation dialog ───────────────────────────────────────────────────────

def confirm(parent, title: str, text: str,
            yes_label: str = "Yes", no_label: str = "No",
            default_yes: bool = False) -> bool:
    """Styled Yes/No confirmation. Returns True if user clicked the affirmative button."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    yes_btn = make_button(yes_label, COLOR_BTN_GREEN)
    no_btn = make_button(no_label, COLOR_BTN_GRAY)
    box.addButton(yes_btn, QMessageBox.YesRole)
    box.addButton(no_btn, QMessageBox.NoRole)
    box.setDefaultButton(yes_btn if default_yes else no_btn)
    box.exec_()
    return box.clickedButton() is yes_btn


# ── Text prompt dialog ─────────────────────────────────────────────────────────

def prompt_text(parent, title: str, label: str, default: str = "",
                max_len: int = 30, ok_label: str = "Save",
                panel_bg: str = None, prog_bg: str = None,
                prog_fg: str = None):
    """Ask for a single line of text in a styled dialog. Returns the stripped
    text, or None if the user cancelled or left it empty.

    Same chrome as every other CommStat dialog - Roboto Slab title strip in the
    program colors over a panel-colored body - so a prompt never falls back to
    Qt's unstyled QInputDialog.

    panel_bg / prog_bg / prog_fg: optional live theme colors; callers with a
    running ConfigManager should pass them so the prompt tracks a theme change.
    """
    panel_bg = panel_bg or DEFAULT_COLORS.get("module_background", "#E4E4E4")
    prog_bg = prog_bg or DEFAULT_COLORS.get("program_background", "#A52A2A")
    prog_fg = prog_fg or DEFAULT_COLORS.get("program_foreground", "#FFFFFF")
    panel_fg = DEFAULT_COLORS.get("module_foreground", "#000000")

    dlg = QDialog(parent)
    # Width is fixed, height is left to the layout: macOS and most Linux desktops
    # render the same 13px labels taller than Windows does, and a hardcoded
    # height would clip the button row there.
    apply_standard_dialog_chrome(dlg, title)
    dlg.setFixedWidth(380)
    dlg.setStyleSheet(
        f"QDialog {{ background-color:{panel_bg}; color:{panel_fg}; }}"
        f"QLabel {{ font-size:13px; color:{panel_fg}; }}"
    )

    body = QVBoxLayout(dlg)
    body.setContentsMargins(15, 15, 15, 15)
    body.setSpacing(10)

    title_lbl = QLabel(title)
    title_lbl.setAlignment(Qt.AlignCenter)
    title_lbl.setStyleSheet(
        "font-family: 'Roboto Slab'; font-size: 16px; font-weight: 900;"
        f"background-color: {prog_bg}; color: {prog_fg}; padding: 9px 0px;"
    )
    body.addWidget(title_lbl)

    field_lbl = QLabel(label)
    field_lbl.setFont(label_font())
    body.addWidget(field_lbl)

    field = make_input(default=default, max_len=max_len)
    body.addWidget(field)
    body.addSpacing(4)

    result = {"text": None}

    def _accept():
        text = field.text().strip()
        if not text:
            return          # Nothing to save; leave the dialog open.
        result["text"] = text
        dlg.accept()

    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)
    btn_row.addStretch()
    ok_btn = make_button(ok_label, COLOR_BTN_GREEN, 80)
    ok_btn.clicked.connect(_accept)
    cancel_btn = make_button("Cancel", COLOR_BTN_CLOSE, 80)
    cancel_btn.clicked.connect(dlg.reject)
    btn_row.addWidget(ok_btn)
    btn_row.addWidget(cancel_btn)
    body.addLayout(btn_row)

    field.returnPressed.connect(_accept)
    field.setFocus()
    dlg.exec_()
    return result["text"]


# ── Input ──────────────────────────────────────────────────────────────────────

def make_input(placeholder: str = "", default: str = "", max_len: int = 0) -> QLineEdit:
    """Standard styled QLineEdit: white background, Kode Mono 13px."""
    e = QLineEdit()
    if placeholder:
        e.setPlaceholderText(placeholder)
    if default:
        e.setText(default)
    if max_len:
        e.setMaxLength(max_len)
    e.setMinimumHeight(30)
    e.setStyleSheet(
        f"QLineEdit {{ background-color:white; color:{COLOR_INPUT_TEXT}; border:1px solid {COLOR_INPUT_BORDER};"
        f" border-radius:4px; padding:2px 6px; font-family:{FONT_MONO_STACK}; font-size:13px; }}"
        f"QLineEdit:focus {{ border:1px solid {COLOR_BTN_BLUE}; }}"
    )
    return e


# ── Combo box ──────────────────────────────────────────────────────────────────

def make_combobox(items) -> QComboBox:
    """
    Standard styled QComboBox matching make_input.
    items: iterable of (label, value) tuples. Stored value via itemData.
    """
    cb = QComboBox()
    cb.setMinimumHeight(30)
    cb.setStyleSheet(
        f"QComboBox {{ background-color:white; color:{COLOR_INPUT_TEXT}; border:1px solid {COLOR_INPUT_BORDER};"
        f" border-radius:4px; padding:2px 6px; font-family:{FONT_MONO_STACK}; font-size:13px; }}"
        f"QComboBox:focus {{ border:1px solid {COLOR_BTN_BLUE}; }}"
        f"QComboBox QAbstractItemView {{ background-color:white; color:{COLOR_INPUT_TEXT};"
        " selection-background-color:#cce5ff; selection-color:#000000;"
        f" font-family:{FONT_MONO_STACK}; font-size:13px; }}"
    )
    for label, value in items:
        cb.addItem(label, value)
    return cb


# ── Checkbox cell ──────────────────────────────────────────────────────────────

def make_checkbox_cell(checked: bool = False) -> Tuple[QWidget, QCheckBox]:
    """Return (container_widget, checkbox) with the checkbox centered in the cell."""
    container = QWidget()
    container.setStyleSheet("background-color: transparent;")
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignCenter)
    cb = QCheckBox()
    cb.setChecked(checked)
    cb.setStyleSheet(
        "QCheckBox { background-color: transparent; }"
        "QCheckBox::indicator { width:16px; height:16px; }"
    )
    layout.addWidget(cb)
    return container, cb


# ── Fonts ──────────────────────────────────────────────────────────────────────

def label_font() -> QtGui.QFont:
    """Roboto Bold — for QLabel headings within dialogs."""
    return QtGui.QFont(FONT_ROBOTO, -1, QtGui.QFont.Bold)


def mono_font() -> QtGui.QFont:
    """Kode Mono — for table cells and data display."""
    return QtGui.QFont(FONT_MONO)


# ── Dialog chrome ──────────────────────────────────────────────────────────────

def apply_standard_dialog_chrome(dialog, title: str, w: int = 0, h: int = 0) -> None:
    """Set window flags, title, optional fixed size, and app icon for any CommStat dialog."""
    dialog.setWindowFlags(
        Qt.Window |
        Qt.CustomizeWindowHint |
        Qt.WindowTitleHint |
        Qt.WindowCloseButtonHint |
        Qt.WindowStaysOnTopHint
    )
    dialog.setWindowTitle(title)
    if w and h:
        dialog.setFixedSize(w, h)
    if os.path.exists(ICON_FILE):
        dialog.setWindowIcon(QtGui.QIcon(ICON_FILE))


# ── Help dialogs ───────────────────────────────────────────────────────────────

# Every CommStat help popup shares this shape: a Roboto Slab title strip in the
# program colors, an HTML body, and a Close button. Feature modules supply only
# the body — the scaffolding lives here so all five popups stay identical and
# nobody re-implements the chrome.
_HELP_BODY_CSS = f"""
QTextBrowser {{ background-color: #FFFFFF; color: {COLOR_INPUT_TEXT}; border: 1px solid #C8C8C8; padding: 10px; }}
"""


def make_help_dialog(parent, title: str, body_html: str,
                     width: int = 460, height: int = 0,
                     panel_bg: str = None, prog_bg: str = None,
                     prog_fg: str = None) -> QDialog:
    """Build (but do not show) a standard CommStat help dialog.

    Args:
        parent:     Dialog parent.
        title:      Shown both in the OS title bar and the colored title strip.
        body_html:  Rich-text body. Qt supports a subset of HTML/CSS —
                    headings, <b>/<i>, <ul>, <table bgcolor=…> all render.
        width:      Dialog width.
        height:     Fixed height; 0 makes the dialog resizable, which is the
                    better default once the body is long enough to scroll.
        panel_bg / prog_bg / prog_fg:
                    Optional live theme colors. Callers with access to the
                    running ConfigManager can pass them so the popup tracks a
                    theme change; everyone else gets the DEFAULT_COLORS values.

    Returns the QDialog so callers can exec_() or show() it.
    """
    panel_bg = panel_bg or DEFAULT_COLORS.get("module_background", "#E4E4E4")
    prog_bg = prog_bg or DEFAULT_COLORS.get("program_background", "#A52A2A")
    prog_fg = prog_fg or DEFAULT_COLORS.get("program_foreground", "#FFFFFF")

    dlg = QDialog(parent)
    apply_standard_dialog_chrome(dlg, title)
    if height:
        dlg.setFixedSize(width, height)
    else:
        dlg.setMinimumSize(min(width, 620), 420)
        dlg.resize(width, 560)
    dlg.setStyleSheet(f"QDialog {{ background-color: {panel_bg}; }}")

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(15, 15, 15, 15)
    layout.setSpacing(10)

    title_lbl = QLabel(title)
    title_lbl.setAlignment(Qt.AlignCenter)
    title_lbl.setStyleSheet(
        "font-family: 'Roboto Slab'; font-size: 16px; font-weight: 900;"
        f"background-color: {prog_bg}; color: {prog_fg}; padding: 9px 0px;"
    )
    layout.addWidget(title_lbl)

    body = QTextBrowser()
    body.setOpenExternalLinks(True)
    body.setStyleSheet(_HELP_BODY_CSS)
    body.setHtml(body_html)
    body.moveCursor(QtGui.QTextCursor.Start)
    layout.addWidget(body, 1)

    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)
    btn_row.addStretch()
    close_btn = make_button("Close", COLOR_BTN_CLOSE, 80)
    close_btn.clicked.connect(dlg.close)
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)

    return dlg


def show_help_dialog(parent, title: str, body_html: str,
                     width: int = 460, height: int = 0, **colors) -> None:
    """Build and modally show a standard CommStat help dialog."""
    make_help_dialog(parent, title, body_html, width, height, **colors).exec_()
