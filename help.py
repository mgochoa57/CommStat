# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""help.py - Help dialog for CommStat"""

import os
from PyQt5 import QtWidgets, QtGui
from PyQt5.QtCore import Qt
from constants import DEFAULT_COLORS, ICON_FILE
from ui_helpers import make_button, apply_standard_dialog_chrome


_PROG_BG  = DEFAULT_COLORS.get("program_background", "#000000")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground", "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",  "#FFFFFF")


class HelpDialog(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        apply_standard_dialog_chrome(self, "Help", 370, 170)
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        self.setStyleSheet(f"background-color: {_PANEL_BG};")

        title = QtWidgets.QLabel("JOIN THE TELEGRAM COMMUNITY")
        title.setStyleSheet(
            f"font-family: 'Roboto Slab'; font-size: 16px; font-weight: 900;"
            f"background-color: {_PROG_BG}; color: {_PROG_FG}; padding: 9px 0px;"
        )
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        body_style = (
            "font-family: Roboto; font-size: 13px; font-weight: normal;"
            "color: #333333; background: transparent;"
        )

        msg = QtWidgets.QLabel("Click this link to join the Telegram community.")
        msg.setStyleSheet(body_style)
        msg.setAlignment(Qt.AlignCenter)
        msg.setWordWrap(True)
        layout.addWidget(msg)

        layout.addSpacing(2)

        link = QtWidgets.QLabel(
            '<a href="https://t.me/+3k3n7O8a1yI1N2E5">'
            'https://t.me/+3k3n7O8a1yI1N2E5</a>'
        )
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        link.setStyleSheet(body_style)
        link.setAlignment(Qt.AlignCenter)
        link.setWordWrap(True)
        layout.addWidget(link)

        layout.addStretch()

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        close_btn = make_button("Close", "#555555", 80)
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)


class AlertsMessagesHelpDialog(QtWidgets.QDialog):
    """Explains the 'Save all Alerts' / 'Save all Messages' Config checkboxes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        apply_standard_dialog_chrome(self, "Alerts & Messages — Help", 460, 300)
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        self.setStyleSheet(f"background-color: {_PANEL_BG};")

        title = QtWidgets.QLabel("ALERTS & MESSAGES")
        title.setStyleSheet(
            f"font-family: 'Roboto Slab'; font-size: 16px; font-weight: 900;"
            f"background-color: {_PROG_BG}; color: {_PROG_FG}; padding: 9px 0px;"
        )
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        body_style = (
            "font-family: Roboto; font-size: 13px; font-weight: normal;"
            "color: #333333; background: transparent;"
        )

        body = QtWidgets.QLabel(
            "By default, CommStat saves <b>alerts</b> and <b>messages</b> "
            "addressed to your callsign, as well as those addressed to any group "
            "listed in your <b>Groups</b> menu."
            "<br><br>"
            "Checking <b>Save all Alerts</b> or <b>Save all Messages</b> tells "
            "CommStat to <i>also</i> save alerts or messages sent to <b>any</b> "
            "group."
            "<br><br>"
            "These options only affect group traffic. They never let you see alerts "
            "or messages that are addressed to other callsigns."
        )
        body.setTextFormat(Qt.RichText)
        body.setStyleSheet(body_style)
        body.setWordWrap(True)
        layout.addWidget(body)

        layout.addStretch()

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        close_btn = make_button("Close", "#555555", 80)
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)