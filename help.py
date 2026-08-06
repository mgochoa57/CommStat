# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""help.py - Help content for features that live in the little_gucci.py monolith.

Help for a feature that has its own module (StatRep, JS8 Direct Message, ...)
lives beside that feature, not here — it changes when the feature changes.
This module is only the overflow for little_gucci.py, which is too large to
carry its own help text.

The dialog chrome (title strip, body, Close button) comes from
ui_helpers.show_help_dialog; everything below is content.
"""

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt

from constants import DEFAULT_COLORS, COLOR_BTN_CLOSE
from ui_helpers import make_button, apply_standard_dialog_chrome, show_help_dialog


_PROG_BG  = DEFAULT_COLORS.get("program_background", "#000000")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground", "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",  "#FFFFFF")


# ── Menu bar ▸ Help ────────────────────────────────────────────────────────────
# Deliberately NOT built on show_help_dialog: this is a two-line link card, not
# a help document, and a scrolling body frame would look wrong at 370x170.

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

        title = QtWidgets.QLabel("Join The Telegram Community")
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
        close_btn = make_button("Close", COLOR_BTN_CLOSE, 80)
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)


# ── Filter menu ▸ Alerts, Messages & Videos ▸ Help ─────────────────────────────

_ALERTS_MESSAGES_HTML = """
<div style="font-family: Roboto; font-size: 13px; color: #333333;">
<p>By default, CommStat saves <b>alerts</b>, <b>messages</b>, and <b>videos</b>
addressed to your callsign, as well as those addressed to any group listed in
your <b>Groups</b> menu.</p>

<p>Checking <b>Save all Alerts</b>, <b>Save all Messages</b>, or
<b>Save all Videos</b> tells CommStat to <i>also</i> save that type of traffic
when sent to <b>any</b> group.</p>

<p>These options only affect group traffic. They never let you see alerts,
messages, or videos that are addressed to other callsigns.</p>
</div>
"""


def show_alerts_messages_help(parent=None, **colors) -> None:
    """Explain the Save all Alerts / Messages / Videos Config checkboxes."""
    show_help_dialog(parent, "Alerts, Messages & Videos",
                     _ALERTS_MESSAGES_HTML, width=470, height=330, **colors)


# ── Map ▸ Filter dropdown ▸ Help ───────────────────────────────────────────────

_MAP_FILTER_HTML = """
<div style="font-family: Roboto; font-size: 13px; color: #333333;">

<p>The <b>Map Filter</b> button in the top-right corner of the map offers two
ways to filter: <b>Map</b> and <b>Custom</b>. Each one changes what the map and
the <b>StatRep table</b> show, and each one answers a different question.</p>

<p style="background-color:#FFF6DA; padding:8px;">
<b>In both modes</b>, the <b>date range</b> you picked in the Filter menu still
applies. Neither mode will show you records older than that range.</p>

<hr>

<h3 style="color:#28a745;">MAP &mdash; let the map do the filtering</h3>
<p>Use this when you want to ask <i>"what is happening in this area?"</i></p>
<ul>
<li>The map ignores your Filter menu choices and plots
    <b>every station</b> in the date range &mdash; including green pins and
    internet-sourced reports you may have hidden.</li>
<li>The StatRep table is then cut down to <b>only the stations whose pin you can
    currently see on the map</b>.</li>
<li>Pan or zoom and the table follows about a second later.</li>
</ul>
<p style="background-color:#F0F7F1; padding:8px;">
<b>Example.</b> You switch to Map and see 40 pins nationwide, and 40 rows in the
table. You zoom into Colorado &mdash; the table drops to the 3 Colorado stations.
You zoom all the way into one pin, and the table shows that single station.
Zoom back out and the rows come back.</p>

<hr>

<h3 style="color:#6f42c1;">CUSTOM &mdash; filter by what is in the record</h3>
<p>Use this when you want to ask <i>"where is this station / this group /
this grid?"</i></p>
<p>A <b>Custom Filtering</b> bar appears directly above the StatRep table
column headings, with four boxes: <b>From</b>, <b>To</b>, <b>ID</b> and
<b>Grid</b>. It filters the <b>table and the map together</b>, and it also
ignores your Filter menu choices.</p>

<p><b style="color:#6f42c1;">How matching works</b></p>
<ul>
<li><b>Partial matches count.</b> Typing <b>EM8</b> in Grid finds EM83CV,
    EM84KQ and so on. Capitals do not matter &mdash; the boxes uppercase what
    you type automatically.</li>
<li><b>Commas mean "or" inside one box.</b> <b>N0DDK, W1ABC</b> in the From box
    finds either station.</li>
<li><b>The Use: AND / Use: OR buttons join the boxes.</b> The word shown between
    the boxes changes to match, so the bar always reads as a sentence.</li>
<li><b>Empty boxes are ignored.</b> Leave all four empty and you get every
    station in the date range.</li>
</ul>

<p><b style="color:#6f42c1;">Examples</b></p>
<table cellspacing="0" cellpadding="7" width="100%" style="font-size:12px;">
<tr bgcolor="#E6E6E6">
  <td width="46%"><b>What you enter</b></td>
  <td><b>What you get</b></td>
</tr>
<tr bgcolor="#FAFAFA">
  <td>From = <b>N0DDK</b></td>
  <td>Only status reports sent by N0DDK.</td>
</tr>
<tr bgcolor="#F2F2F2">
  <td>From = <b>N0DDK, W1ABC, K9ROB</b></td>
  <td>Reports from any of those three stations.</td>
</tr>
<tr bgcolor="#FAFAFA">
  <td>Grid = <b>EM</b></td>
  <td>Every station in an EM grid square &mdash; a quick way to see one
      region.</td>
</tr>
<tr bgcolor="#F2F2F2">
  <td>To = <b>AMRRON</b></td>
  <td>Traffic sent to the @AMRRON group. You do not need to type the @.</td>
</tr>
<tr bgcolor="#FAFAFA">
  <td>ID = <b>A31</b></td>
  <td>The status report whose ID contains A31.</td>
</tr>
<tr bgcolor="#F2F2F2">
  <td>From = <b>N0DDK</b><br>Grid = <b>EM83</b><br>with <b>Use: AND</b></td>
  <td><b>Both</b> must be true &mdash; N0DDK's reports, but only the ones in
      grid EM83.</td>
</tr>
<tr bgcolor="#FAFAFA">
  <td>From = <b>N0DDK</b><br>Grid = <b>EM83</b><br>with <b>Use: OR</b></td>
  <td><b>Either</b> can be true &mdash; all of N0DDK's reports, <i>plus</i>
      everything else in grid EM83.</td>
</tr>
<tr bgcolor="#F2F2F2">
  <td>From = <b>N0DDK, W1ABC</b><br>Grid = <b>EM83</b><br>with <b>Use: AND</b></td>
  <td>Commas still mean "or" inside the box: either station, but only when it
      is in grid EM83.</td>
</tr>
</table>

<hr>

<h3 style="color:#555555;">Good to know</h3>
<ul>
<li>While <b>Map</b> or <b>Custom</b> is on, your Filter menu group checkboxes,
    <b>Hide Green Pins</b> and <b>Hide Internet Feed</b> are <b>ignored</b>.
    Switch back to <b>Off</b> to use them again.</li>
<li>Anything you type in the Custom boxes is remembered while CommStat is
    running, so you can switch modes and come back to it. It clears when you
    restart.</li>
<li>The table updates as you type, a moment after you stop.</li>
</ul>

</div>
"""


def show_map_filter_help(parent=None, **colors) -> None:
    """Explain the map's Map and Custom filtering modes."""
    show_help_dialog(parent, "Map & Custom Filtering",
                     _MAP_FILTER_HTML, width=720, **colors)
