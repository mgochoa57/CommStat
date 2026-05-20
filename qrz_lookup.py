# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
qrz_lookup.py - QRZ Callsign Lookup Dialogs for CommStat

Three modal dialog views:
  - QRZLookupDialog     : standalone search (Tools → QRZ Lookup)
  - StatRepDetailDialog : detail view when clicking a StatRep row
  - MessageDetailDialog : detail view when clicking a Message row
"""

import base64
import datetime
import io
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from typing import Dict, Optional

import folium
import maidenhead as mh
from PyQt5 import QtGui
from PyQt5.QtCore import QBuffer, QByteArray, Qt, QThread, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QDesktopServices, QFont, QMovie, QPainter, QPixmap
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QStyledItemDelegate,
    QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from id_utils import generate_time_based_id
from qrz_client import QRZClient, get_qrz_cached, load_qrz_config
from constants import (
    DEFAULT_COLORS, COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    COLOR_BTN_RED, COLOR_BTN_BLUE, COLOR_BTN_CYAN,
)
# Single source of truth for mouse-wheel zoom dampening — see little_gucci.py
from little_gucci import MAP_WHEEL_PX_PER_ZOOM

DB_PATH = "traffic.db3"
_BACKBONE_URL  = base64.b64decode("aHR0cHM6Ly9jb21tc3RhdC5hcHA=").decode()
_DATAFEED_URL  = _BACKBONE_URL + "/datafeed-808585.php"

_PROG_BG    = DEFAULT_COLORS.get("program_background", "#000000")
_PROG_FG    = DEFAULT_COLORS.get("program_foreground", "#FFFFFF")
_DATA_BG    = DEFAULT_COLORS.get("data_background",    "#F8F6F4")
_COL_CANCEL = "#555555"
_COL_PURPLE = "#6f42c1"
_COL_NAV    = "#e07b39"

# StatRep status field order: (display label, statrep row index)
STATUS_FIELDS = [
    ("Map",    8),
    ("Power",  9),
    ("Water", 10),
    ("Med",   11),
    ("Comms", 12),
    ("Travel",13),
    ("Inet",  14),
    ("Fuel",  15),
    ("Food",  16),
    ("Crime", 17),
    ("Civil", 18),
    ("Pol",   19),
]

# Status value → (CSS color string, tooltip text)
STATUS_COLORS: Dict[str, tuple] = {
    "1": ("rgb(0, 128, 0)",     "Green: Normal"),
    "2": ("rgb(255, 255, 0)",   "Yellow: Warning"),
    "3": ("rgb(255, 0, 0)",     "Red: Critical"),
    "4": ("rgb(128, 128, 128)", "Gray: Unknown"),
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _lbl_font() -> QFont:
    return QFont("Roboto", -1, QFont.Bold)


def _mono_font() -> QFont:
    return QFont("Kode Mono")


def _btn(label: str, color: str, min_w: int = 90) -> QPushButton:
    b = QPushButton(label)
    b.setMinimumWidth(min_w)
    b.setStyleSheet(
        f"QPushButton {{ background-color:{color}; color:#ffffff; border:none;"
        f" padding:6px 14px; border-radius:4px; font-family:Roboto; font-size:15px;"
        f" font-weight:bold; }}"
        f"QPushButton:hover {{ background-color:{color}; opacity:0.9; }}"
        f"QPushButton:pressed {{ background-color:{color}; }}"
    )
    return b


def _normalize_qrz(data: dict) -> dict:
    """Normalize QRZ data to consistent display keys.

    Handles both the raw API response (addr1/addr2/call) and the
    cached DB row (address/city/callsign) transparently.
    """
    return {
        "call":     (data.get("call") or data.get("callsign") or "").upper(),
        "name":     " ".join(x for x in (
                        (data.get("fname") or "").strip(),
                        (data.get("name") or "").strip()
                    ) if x).title() or "",
        "born":     str(data.get("born") or ""),
        "expdate":  str(data.get("expdate") or ""),
        "addr1":    data.get("addr1") or data.get("address") or "",
        "addr2":    data.get("addr2") or data.get("city") or "",
        "state":    data.get("state") or "",
        "zip":      str(data.get("zip") or ""),
        "county":   data.get("county") or "",
        "country":  data.get("country") or "",
        "license":  data.get("class") or "",
        "grid":     data.get("grid") or "",
        "lat":      str(data.get("lat") or ""),
        "lon":      str(data.get("lon") or ""),
        "email":    data.get("email") or "",
        "image":    data.get("image") or "",
        "moddate":  data.get("moddate") or "",
    }


def _make_map_html(lat: float, lon: float, internet_available: bool = True,
                   extra_lat: float = None, extra_lon: float = None) -> str:
    """Generate folium map HTML with one or two marker pins.

    The primary pin (lat/lon) is blue. If extra_lat/extra_lon are provided,
    a red secondary pin is added and the map fits both markers.
    """
    if extra_lat is not None and extra_lon is not None:
        center_lat = (lat + extra_lat) / 2
        center_lon = (lon + extra_lon) / 2
        m = folium.Map(
            location=[center_lat, center_lon], zoom_start=4,
            wheelPxPerZoomLevel=MAP_WHEEL_PX_PER_ZOOM, zoomSnap=0.25,
        )
    else:
        m = folium.Map(
            location=[lat, lon], zoom_start=4,
            wheelPxPerZoomLevel=MAP_WHEEL_PX_PER_ZOOM, zoomSnap=0.25,
        )

    folium.raster_layers.TileLayer(
        tiles="tiles://local/{z}/{x}/{y}.png",
        name="Local Tiles", attr="Local Tiles",
        max_zoom=8, control=False,
    ).add_to(m)
    if internet_available:
        folium.raster_layers.TileLayer(
            tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
            name="OpenStreetMap", attr="OpenStreetMap",
            min_zoom=8, control=False,
        ).add_to(m)

    if extra_lat is not None and extra_lon is not None:
        folium.Marker(location=[lat, lon], icon=folium.Icon(color="red")).add_to(m)
        folium.Marker(location=[extra_lat, extra_lon]).add_to(m)
        m.fit_bounds([[min(lat, extra_lat), min(lon, extra_lon)],
                      [max(lat, extra_lat), max(lon, extra_lon)]],
                     max_zoom=4)
    else:
        folium.Marker(location=[lat, lon]).add_to(m)

    buf = io.BytesIO()
    m.save(buf, close_file=False)
    html = buf.getvalue().decode()
    html = html.replace(
        "<head>",
        '<head><meta name="referrer" content="no-referrer-when-downgrade">',
        1
    )
    return html


def _hsep() -> QFrame:
    """Return a styled horizontal separator line."""
    sep = QFrame()
    sep.setFrameShape(QFrame.HLine)
    sep.setStyleSheet("color:#cccccc;")
    return sep


# ── Background workers ─────────────────────────────────────────────────────

class _ImageLoader(QThread):
    """Downloads and scales a QRZ profile image in the background.

    If `max_size` (w, h) is provided, the image is scaled to fit within that
    bounding box while preserving aspect ratio (so wide banners get a shorter
    rendered height instead of an oversized width).
    Otherwise, if `target_height` is provided the image is scaled to that exact
    height; with neither set the height is auto-selected (166 for tall, 126 for wide).
    """
    image_loaded = pyqtSignal(QPixmap)
    gif_loaded   = pyqtSignal(bytes)

    def __init__(self, url: str, target_height: Optional[int] = None,
                 max_size: Optional[tuple] = None):
        super().__init__()
        self.url = url
        self.target_height = target_height
        self.max_size = max_size

    def run(self) -> None:
        try:
            with urllib.request.urlopen(self.url, timeout=10) as resp:
                data = resp.read()
            if self.url.lower().split("?")[0].endswith(".gif"):
                self.gif_loaded.emit(data)
                return
            px = QPixmap()
            px.loadFromData(data)
            if not px.isNull():
                if self.max_size is not None:
                    mw, mh = self.max_size
                    scaled = px.scaled(mw, mh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                else:
                    if self.target_height is not None:
                        target_h = self.target_height
                    else:
                        target_h = 166 if px.height() * 2.0 > px.width() else 126
                    scaled = px.scaledToHeight(target_h, Qt.SmoothTransformation)
                self.image_loaded.emit(scaled)
        except Exception:
            pass


def _get_local_callsign() -> str:
    """Read the local station callsign from the controls table."""
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT callsign FROM controls WHERE id = 1")
            row = cursor.fetchone()
            return (row[0] or "").strip() if row else ""
    except Exception:
        return ""


class _ReadCountThread(QThread):
    """Fetches the delivery read-count (and last-seen) from the backbone server."""
    count_ready = pyqtSignal(str)

    def __init__(self, backbone_url: str, callsign: str, global_id: int):
        super().__init__()
        self.backbone_url = backbone_url
        self.callsign = callsign
        self.global_id = global_id

    def run(self) -> None:
        try:
            url = (f"{self.backbone_url}/get-read-count-808585.php"
                   f"?cs={urllib.parse.quote(self.callsign)}&id={self.global_id}")
            with urllib.request.urlopen(url, timeout=10) as resp:
                text = resp.read().decode().strip()
            self.count_ready.emit(text)
        except Exception:
            self.count_ready.emit("")


class _QRZThread(QThread):
    """Performs a QRZ callsign lookup in the background."""
    result_ready = pyqtSignal(object)  # dict or None

    def __init__(self, callsign: str, username: Optional[str], password: Optional[str]):
        super().__init__()
        self.callsign = callsign
        self.username = username
        self.password = password

    def run(self) -> None:
        client = QRZClient(self.username, self.password)
        self.result_ready.emit(client.lookup(self.callsign))


# ── Clickable image label ──────────────────────────────────────────────────

class _ClickableImageLabel(QLabel):
    """QLabel that opens a URL in the browser when clicked (if one is set)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._url: str = ""

    def set_url(self, url: str) -> None:
        self._url = url
        self.setCursor(QCursor(Qt.PointingHandCursor) if url else QCursor(Qt.ArrowCursor))

    def mousePressEvent(self, event) -> None:
        if self._url and event.button() == Qt.LeftButton:
            QDesktopServices.openUrl(QUrl(self._url))
        else:
            super().mousePressEvent(event)


class _MemoTextEdit(QTextEdit):
    """QTextEdit that emits focus_lost when it loses keyboard focus."""
    focus_lost = pyqtSignal()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.focus_lost.emit()


class _ToggleSwitch(QWidget):
    """iOS-style toggle switch that emits toggled(bool) on state change."""
    toggled = pyqtSignal(bool)

    _W, _H = 50, 26
    _KNOB   = 22
    _MARGIN = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checked = False
        self.setFixedSize(self._W, self._H)
        self.setCursor(QCursor(Qt.PointingHandCursor))

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        self._checked = checked
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._checked = not self._checked
            self.update()
            self.toggled.emit(self._checked)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track_color = QColor("#28a745") if self._checked else QColor("#aaaaaa")
        p.setBrush(track_color)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, (self._H - 20) // 2, self._W, 20, 10, 10)
        knob_x = self._W - self._KNOB - self._MARGIN if self._checked else self._MARGIN
        knob_y = (self._H - self._KNOB) // 2
        p.setBrush(QColor("white"))
        p.drawEllipse(knob_x, knob_y, self._KNOB, self._KNOB)
        p.end()


# ── Shared QRZ info panel ──────────────────────────────────────────────────

class _QRZInfoSection(QWidget):
    """Three-column QRZ info display used by all three dialogs.

    Left  : section header, callsign, name, address
    Center: last seen, license, grid, lat/lon, email
    Right : profile image, last modified date
    """

    image_width_ready = pyqtSignal(int)
    last_seen_updated = pyqtSignal(str)

    def __init__(self, hdr_bg: str = "", hdr_fg: str = "", skip_last_seen: bool = False, parent=None):
        super().__init__(parent)
        self._img_loader: Optional[_ImageLoader] = None
        self._gif_movie: Optional[QMovie] = None
        self._hdr_bg = hdr_bg
        self._hdr_fg = hdr_fg
        self._skip_last_seen = skip_last_seen
        self._build()

    def _build(self) -> None:
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(10, 8, 10, 0)
        self._main_layout.setSpacing(0)

        outer = QHBoxLayout()
        outer.setSpacing(24)
        self._main_layout.addLayout(outer)

        # ── Columns 1 & 2 (2/3 total) via QGridLayout for row alignment ─
        self._grid = QGridLayout()
        grid = self._grid
        grid.setSpacing(2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self.hdr = QLabel("QRZ API Lookup For:")
        self.hdr.setFont(QFont("Roboto Slab", -1, QFont.Black))
        self.hdr.setStyleSheet(
            f"QLabel {{ background-color: {self._hdr_bg}; color: {self._hdr_fg}; font-size: 16px; padding-top: 9px; padding-bottom: 9px; }}"
            if self._hdr_bg else ""
        )
        self.hdr.setAlignment(Qt.AlignCenter)

        self.lbl_call    = QLabel(); self.lbl_call.setFont(_mono_font())
        self.lbl_name    = QLabel(); self.lbl_name.setFont(_mono_font())
        self.lbl_addr1   = QLabel(); self.lbl_addr1.setFont(_mono_font())
        self.lbl_addr2   = QLabel(); self.lbl_addr2.setFont(_mono_font())
        self.lbl_county  = QLabel(); self.lbl_county.setFont(_mono_font())
        self.lbl_country = QLabel(); self.lbl_country.setFont(_mono_font())
        self.lbl_last_seen = QLabel(); self.lbl_last_seen.setFont(_mono_font())
        self.lbl_license    = QLabel(); self.lbl_license.setFont(_mono_font())
        self.lbl_grid    = QLabel(); self.lbl_grid.setFont(_mono_font())
        self.lbl_lat     = QLabel(); self.lbl_lat.setFont(_mono_font())
        self.lbl_lon     = QLabel(); self.lbl_lon.setFont(_mono_font())

        self.lbl_qrz_status = QLabel()
        self.lbl_qrz_status.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }")
        self.lbl_qrz_status.setWordWrap(True)
        self.lbl_qrz_status.setVisible(False)

        self.last_seen_updated.connect(self._on_last_seen_updated)

        grid.addWidget(self.hdr,              0, 0, 1, 2)
        grid.addWidget(self.lbl_qrz_status,   1, 0, 1, 2)
        grid.addWidget(self.lbl_call,         2, 0)
        grid.addWidget(self.lbl_name,         3, 0)
        grid.addWidget(self.lbl_last_seen,    3, 1)
        grid.addWidget(self.lbl_addr1,        4, 0)
        grid.addWidget(self.lbl_license,      4, 1)
        grid.addWidget(self.lbl_addr2,        5, 0)
        grid.addWidget(self.lbl_grid,         5, 1)
        grid.addWidget(self.lbl_county,       6, 0)
        grid.addWidget(self.lbl_lat,          6, 1)
        grid.addWidget(self.lbl_country,      7, 0)
        grid.addWidget(self.lbl_lon,          7, 1)
        grid.setRowStretch(8, 1)
        outer.addLayout(grid, 2)

        # ── Column 3 (1/3): image + photo status + moddate ───────────────
        right = QVBoxLayout()
        right.setAlignment(Qt.AlignTop | Qt.AlignRight)
        right.setSpacing(4)
        self.lbl_image = _ClickableImageLabel()
        self.lbl_image.setAlignment(Qt.AlignTop | Qt.AlignRight)
        self.lbl_image.setStyleSheet("QLabel { border:none; padding:0px; }")
        self.lbl_moddate = QLabel()
        self.lbl_moddate.setFont(QFont("Roboto"))
        self.lbl_moddate.setStyleSheet("QLabel { font-size: 13px; font-weight: normal; }")
        self.lbl_moddate.setAlignment(Qt.AlignRight)
        moddate_row = QHBoxLayout()
        moddate_row.addStretch()
        moddate_row.addWidget(self.lbl_moddate)
        right.addWidget(self.lbl_image)
        right.addLayout(moddate_row)
        right.addStretch()
        outer.addLayout(right, 1)

    def add_memo_row(self, trailing_space: int = 12) -> QLineEdit:
        """Add a contact-note label, input, and separator spanning all three columns."""
        return self._add_note_input("Add a contact note…", trailing_space=trailing_space)

    def add_statrep_memo_row(self) -> QPlainTextEdit:
        """Add a multi-line status-report-note input that grows to fill extra vertical space."""
        self._main_layout.addSpacing(10)
        memo_input = QPlainTextEdit()
        memo_input.setFont(_mono_font())
        memo_input.setMinimumHeight(34)
        memo_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        memo_input.setTabChangesFocus(True)
        memo_input.setStyleSheet(
            f"QPlainTextEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px; }}"
        )
        memo_input.setPlaceholderText("Add a status report note…")
        self._main_layout.addWidget(memo_input, 1)
        return memo_input

    def _add_note_input(self, placeholder: str, trailing_space: int = 12) -> QLineEdit:
        self._main_layout.addSpacing(10)
        memo_input = QLineEdit()
        memo_input.setFont(_mono_font())
        memo_input.setMinimumHeight(34)
        memo_input.setStyleSheet(
            f"background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
        )
        memo_input.setPlaceholderText(placeholder)
        self._main_layout.addWidget(memo_input)
        if trailing_space:
            self._main_layout.addSpacing(trailing_space)

        return memo_input

    def add_statrep_rows(self) -> None:
        """Add separator + StatRep fields below the QRZ section, spanning all three columns."""
        self._grid.setRowStretch(8, 0)

        sr_grid = QGridLayout()
        sr_grid.setSpacing(2)
        sr_grid.setColumnStretch(0, 1)
        sr_grid.setColumnStretch(1, 1)
        sr_grid.setColumnStretch(2, 1)

        self.lbl_sr_posted    = QLabel(); self.lbl_sr_posted.setFont(_mono_font())
        self.lbl_sr_group     = QLabel(); self.lbl_sr_group.setFont(_mono_font())
        self.lbl_sr_freq      = QLabel(); self.lbl_sr_freq.setFont(_mono_font())
        self.lbl_sr_sr_id     = QLabel(); self.lbl_sr_sr_id.setFont(_mono_font())
        self.lbl_sr_global_id = QLabel(); self.lbl_sr_global_id.setFont(_mono_font())
        self.lbl_sr_grid      = QLabel(); self.lbl_sr_grid.setFont(_mono_font())
        self.lbl_sr_source    = QLabel(); self.lbl_sr_source.setFont(_mono_font())
        self.lbl_sr_delivered = QLabel(); self.lbl_sr_delivered.setFont(_mono_font())

        sr_hdr = QLabel("Status Report Details")
        sr_hdr.setFont(_lbl_font())

        # Row 0: header | Freq:    | Grid:
        sr_grid.addWidget(sr_hdr,                 0, 0)
        sr_grid.addWidget(self.lbl_sr_freq,        0, 1)
        sr_grid.addWidget(self.lbl_sr_grid,        0, 2)
        # Row 1: Posted: | Statrep ID: | Received via:
        sr_grid.addWidget(self.lbl_sr_posted,      1, 0)
        sr_grid.addWidget(self.lbl_sr_sr_id,       1, 1)
        sr_grid.addWidget(self.lbl_sr_source,      1, 2)
        # Row 2: Group:  | Global ID:  | Delivered To:
        sr_grid.addWidget(self.lbl_sr_group,       2, 0)
        sr_grid.addWidget(self.lbl_sr_global_id,   2, 1)
        sr_grid.addWidget(self.lbl_sr_delivered,   2, 2)

        self._main_layout.addLayout(sr_grid)

    def set_qrz_status(self, text: str) -> None:
        self.lbl_qrz_status.setText(text)
        self.lbl_qrz_status.setVisible(True)

    def clear_qrz_status(self) -> None:
        self.lbl_qrz_status.setText("")
        self.lbl_qrz_status.setVisible(False)

    # ── Last Seen lookup ──────────────────────────────────────────────────────

    def _fetch_last_seen(self, target: str) -> None:
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.lbl_last_seen.setText(f'<span style="{_k}">Last Seen:</span> …')
        threading.Thread(target=self._last_seen_thread, args=(target,), daemon=True).start()

    def _last_seen_thread(self, target: str) -> None:
        try:
            my_cs = _get_local_callsign()
            if not my_cs:
                self.last_seen_updated.emit("—")
                return
            url = (
                f"{_BACKBONE_URL}/get-last-seen-808585.php"
                f"?cs={urllib.parse.quote(my_cs)}&lookup={urllib.parse.quote(target)}"
            )
            with urllib.request.urlopen(url, timeout=8) as resp:
                result = resp.read().decode("utf-8").strip()
            self.last_seen_updated.emit(result if result else "—")
        except Exception as e:
            print(f"[QRZInfoSection] last-seen error: {e}")
            self.last_seen_updated.emit("—")

    def _on_last_seen_updated(self, value: str) -> None:
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.lbl_last_seen.setText(f'<span style="{_k}">Last Seen:</span> {value}')

    def update_data(self, data: dict) -> None:
        """Populate all labels from raw QRZ data (API or cached format)."""
        d = _normalize_qrz(data)

        self.hdr.setText(f"QRZ API Lookup For: {d['call']}")
        self.lbl_call.setText("")
        self.lbl_name.setText(f"<b>{d['name']}</b>" if d["name"] else "")

        self.lbl_addr1.setText(d["addr1"])
        city_state = ", ".join(x for x in (d["addr2"], d["state"]) if x)
        if d["zip"]:
            city_state = (city_state + " " + d["zip"]).strip()
        self.lbl_addr2.setText(city_state)

        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.lbl_county.setText(f'<span style="{_k}">County:</span> {d["county"]}' if d["county"] else "")
        self.lbl_country.setText(f'<span style="{_k}">Country:</span> {d["country"]}' if d["country"] else "")

        if d["call"] and not self._skip_last_seen:
            self.lbl_last_seen.setText(f'<span style="{_k}">Last Seen:</span> —')
            self._fetch_last_seen(d["call"])

        if d["license"] and d["expdate"]:
            self.lbl_license.setText(f'<span style="{_k}">License:</span> {d["license"]} (exp: {d["expdate"]})')
        elif d["expdate"]:
            self.lbl_license.setText(f'(exp: {d["expdate"]})')
        elif d["license"]:
            self.lbl_license.setText(f'<span style="{_k}">License:</span> {d["license"]}')
        else:
            self.lbl_license.setText("")
        self.lbl_grid.setText(f'<span style="{_k}">Grid:</span> {d["grid"]}' if d["grid"] else "")
        self.lbl_lat.setText(f'<span style="{_k}">Lat:</span> {d["lat"]}' if d["lat"] else "")
        self.lbl_lon.setText(f'<span style="{_k}">Lon:</span> {d["lon"]}' if d["lon"] else "")

        profile_url = f"https://www.qrz.com/db/{d['call']}" if d["call"] else ""

        self.lbl_moddate.setText(
            f"QRZ profile last modified: {d['moddate'].split()[0]}" if d["moddate"] else ""
        )

        self.lbl_image.clear()
        self.lbl_image.set_url(profile_url)
        if d["image"]:
            self._img_loader = _ImageLoader(d["image"])
            self._img_loader.image_loaded.connect(self._on_image_loaded)
            self._img_loader.gif_loaded.connect(self._on_gif_loaded)
            self._img_loader.start()
        else:
            self._load_default_image()

    def _on_image_loaded(self, px: QPixmap) -> None:
        self.lbl_image.setPixmap(px)
        self.image_width_ready.emit(px.width())

    def _on_gif_loaded(self, data: bytes) -> None:
        # Load first frame into QPixmap to reliably get the native dimensions
        # (QMovie.currentPixmap() before start() often returns a null pixmap)
        px_probe = QPixmap()
        px_probe.loadFromData(data)
        if not px_probe.isNull():
            target_h = 166 if px_probe.height() * 2.0 > px_probe.width() else 126
            scaled_size = px_probe.scaledToHeight(target_h, Qt.SmoothTransformation).size()
        else:
            scaled_size = None

        buf = QBuffer()
        buf.setData(QByteArray(data))
        buf.open(QBuffer.ReadOnly)
        self._gif_movie = QMovie()
        self._gif_movie.setDevice(buf)
        self._gif_movie._buf = buf
        if scaled_size is not None:
            self._gif_movie.setScaledSize(scaled_size)
        self.lbl_image.setMovie(self._gif_movie)
        self._gif_movie.start()
        self.image_width_ready.emit(self._gif_movie.scaledSize().width())

    def _load_default_image(self) -> None:
        px = QPixmap("00-qrz-default.png")
        if not px.isNull():
            target_h = 166 if px.height() * 2.0 > px.width() else 126
            self.lbl_image.setPixmap(px.scaledToHeight(target_h, Qt.SmoothTransformation))
        else:
            self.lbl_image.clear()

    def show_no_data_placeholder(self) -> None:
        """Show label keys and default image with no QRZ data populated."""
        if self._gif_movie:
            self._gif_movie.stop()
            self._gif_movie = None
        self.hdr.setText("QRZ API Lookup For:")
        self.lbl_call.clear()
        self.lbl_name.clear()
        self.lbl_addr1.clear()
        self.lbl_addr2.clear()
        self.lbl_moddate.clear()
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.lbl_county.setText(f"<span style='{_k}'>County:</span>")
        self.lbl_country.setText(f"<span style='{_k}'>Country:</span>")
        if not self._skip_last_seen:
            self.lbl_last_seen.setText(f"<span style='{_k}'>Last Seen:</span>")
        else:
            self.lbl_last_seen.clear()
        self.lbl_license.setText(f"<span style='{_k}'>License:</span>")
        self.lbl_grid.setText(f"<span style='{_k}'>Grid:</span>")
        self.lbl_lat.setText(f"<span style='{_k}'>Lat:</span>")
        self.lbl_lon.setText(f"<span style='{_k}'>Lon:</span>")
        self.lbl_image.set_url("")
        self._load_default_image()

    def clear(self) -> None:
        if self._gif_movie:
            self._gif_movie.stop()
            self._gif_movie = None
        self.hdr.setText("QRZ API Lookup For:")
        self.clear_qrz_status()
        for w in (self.lbl_call, self.lbl_name, self.lbl_addr1, self.lbl_addr2,
                  self.lbl_county, self.lbl_country, self.lbl_last_seen, self.lbl_license,
                  self.lbl_grid, self.lbl_lat, self.lbl_lon,
                  self.lbl_image, self.lbl_moddate):
            w.clear()


# ── Dialog 1: Standalone QRZ Lookup ───────────────────────────────────────

class QRZLookupDialog(QDialog):
    """Standalone QRZ callsign lookup (Tools → QRZ Lookup)."""

    _send_result = pyqtSignal(str)

    def __init__(self, module_background: str = "#f5f5f5",
                 module_foreground: str = "#333333",
                 program_background: str = "",
                 program_foreground: str = "",
                 initial_callsign: str = "",
                 initial_message: str = "",
                 parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint |
            Qt.WindowStaysOnTopHint
        )
        self._module_bg = module_background
        self._module_fg = module_foreground
        self._program_bg = program_background or _PROG_BG
        self._program_fg = program_foreground or _PROG_FG
        self.setWindowTitle("QRZ Lookup")
        self.setModal(True)
        self.setMinimumSize(825, 500)
        self.resize(902, 580)
        if os.path.exists("radiation-32.png"):
            self.setWindowIcon(QtGui.QIcon("radiation-32.png"))
        self._thread: Optional[_QRZThread] = None
        self._send_result.connect(self._on_send_result)
        self._setup_ui()

        cs = initial_callsign.strip().upper()
        if cs:
            self.cs_edit.setText(cs)
            self._search()
        if initial_message:
            self.msg_edit.setPlainText(initial_message)
        if cs or initial_message:
            from PyQt5.QtGui import QTextCursor
            cursor = self.msg_edit.textCursor()
            cursor.movePosition(QTextCursor.Start)
            self.msg_edit.setTextCursor(cursor)
            self.msg_edit.setFocus()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{self._module_bg}; }}"
            f"QLabel {{ color:{self._module_fg}; background-color: transparent; font-size: 13px; }}"
            f"QLineEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
        )
        main = QVBoxLayout(self)
        main.setContentsMargins(15, 15, 15, 15)
        main.setSpacing(10)

        # Title
        title = QLabel("QRZ Lookup")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Roboto Slab", -1, QFont.Black))
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"QLabel {{ background-color: {self._program_bg}; color: {self._program_fg}; "
            "font-size: 16px; padding-top: 9px; padding-bottom: 9px; }"
        )
        main.addWidget(title)

        row = QHBoxLayout()
        self.cs_edit = QLineEdit()
        self.cs_edit.setPlaceholderText("Enter callsign…")
        self.cs_edit.setMaxLength(12)
        self.cs_edit.setFont(_mono_font())
        self.cs_edit.setMinimumHeight(34)
        self.cs_edit.returnPressed.connect(self._search)
        self.cs_edit.textChanged.connect(self._force_upper)
        row.addWidget(self.cs_edit)

        self.btn_search = _btn("Search", COLOR_BTN_BLUE)
        self.btn_search.setFixedWidth(90)
        self.btn_search.setAutoDefault(False)
        self.btn_search.clicked.connect(self._search)
        row.addWidget(self.btn_search)
        main.addLayout(row)

        self.lbl_status = QLabel()
        self.lbl_status.setFont(QFont("Roboto"))
        self.lbl_status.setStyleSheet(
            f"QLabel {{ color:{self._module_fg}; font-family:Roboto; font-size:13px; font-weight:bold; }}"
        )
        main.addWidget(self.lbl_status)

        self.qrz_info = _QRZInfoSection(hdr_bg=self._program_bg, hdr_fg=self._program_fg, parent=self)
        self.qrz_info.image_width_ready.connect(self._adjust_for_image_width)
        self.qrz_info.show_no_data_placeholder()
        self.memo_edit = self.qrz_info.add_memo_row()
        self.memo_edit.editingFinished.connect(self._save_memo)
        main.addWidget(self.qrz_info)

        self.msg_edit = QPlainTextEdit()
        self.msg_edit.setFont(_mono_font())
        self.msg_edit.setPlaceholderText("Enter message…")
        self.msg_edit.setStyleSheet(
            f"background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px;"
        )
        from PyQt5.QtGui import QFontMetrics
        _fm = QFontMetrics(self.msg_edit.font())
        self.msg_edit.setFixedHeight(_fm.lineSpacing() * 6 + 14)
        self.msg_edit.textChanged.connect(self._on_msg_changed)
        main.addWidget(self.msg_edit)
        main.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        self.btn_clear_msg = _btn("Clear", _COL_CANCEL)
        self.btn_clear_msg.setVisible(False)
        self.btn_clear_msg.clicked.connect(self.msg_edit.clear)
        btn_row.addWidget(self.btn_clear_msg)
        self.btn_send = _btn("Send", COLOR_BTN_BLUE)
        self.btn_send.setVisible(False)
        self.btn_send.clicked.connect(self._on_send_internet)
        btn_row.addWidget(self.btn_send)
        self.btn_close_lookup = _btn("Close", _COL_CANCEL)
        self.btn_close_lookup.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_close_lookup)
        main.addLayout(btn_row)

    def _adjust_for_image_width(self, img_width: int) -> None:
        if img_width > 275:
            self.resize(self.width() + (img_width - 275), self.height())

    def _force_upper(self, text: str) -> None:
        if text != text.upper():
            self.cs_edit.blockSignals(True)
            pos = self.cs_edit.cursorPosition()
            self.cs_edit.setText(text.upper())
            self.cs_edit.setCursorPosition(pos)
            self.cs_edit.blockSignals(False)

    def _search(self) -> None:
        cs = self.cs_edit.text().strip().upper()
        if not cs:
            return

        is_active, username, password = load_qrz_config()
        cached_fresh = get_qrz_cached(cs)
        cached_any   = cached_fresh or get_qrz_cached(cs, include_stale=True)

        self.qrz_info.clear()
        self.memo_edit.blockSignals(True)
        self.memo_edit.clear()
        self.memo_edit.blockSignals(False)

        if not username:
            found_str = "found" if cached_any else "NOT found"
            self.lbl_status.setText(
                f"QRZ Subscription not configured — {cs} {found_str} in local database"
            )
            if cached_any:
                self.qrz_info.update_data(cached_any)
                self.memo_edit.blockSignals(True)
                self.memo_edit.setText(cached_any.get("memo") or "")
                self.memo_edit.blockSignals(False)
            else:
                self.qrz_info.show_no_data_placeholder()
            return

        if not is_active:
            found_str = "found" if cached_any else "NOT found"
            self.lbl_status.setText(
                f"QRZ Subscription not enabled — {cs} {found_str} in local database"
            )
            if cached_any:
                self.qrz_info.update_data(cached_any)
                self.memo_edit.blockSignals(True)
                self.memo_edit.setText(cached_any.get("memo") or "")
                self.memo_edit.blockSignals(False)
            else:
                self.qrz_info.show_no_data_placeholder()
            return

        if cached_fresh:
            self.lbl_status.setText("")
            self.qrz_info.update_data(cached_fresh)
            self.memo_edit.blockSignals(True)
            self.memo_edit.setText(cached_fresh.get("memo") or "")
            self.memo_edit.blockSignals(False)
            return

        self.lbl_status.setText(f"Looking up {cs}…")
        self.btn_search.setEnabled(False)
        self.qrz_info.show_no_data_placeholder()
        self._thread = _QRZThread(cs, username, password)
        self._thread.result_ready.connect(self._on_result)
        self._thread.start()

    def _on_result(self, result) -> None:
        self.btn_search.setEnabled(True)
        if result:
            self.lbl_status.setText("")
            self.qrz_info.update_data(result)
            self.memo_edit.blockSignals(True)
            self.memo_edit.setText(result.get("memo") or "")
            self.memo_edit.blockSignals(False)
        else:
            self.lbl_status.setText("No results found.")
            self.qrz_info.show_no_data_placeholder()

    def _save_memo(self) -> None:
        """Save memo text to the qrz table on focus-out."""
        cs = self.cs_edit.text().strip().upper()
        if not cs:
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE qrz SET memo = ? WHERE callsign = ?",
                    (self.memo_edit.text(), cs)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[QRZLookupDialog] Memo save error: {e}")

    def _on_msg_changed(self) -> None:
        has_text = bool(self.msg_edit.toPlainText().strip())
        self.btn_clear_msg.setVisible(has_text)
        self.btn_send.setVisible(has_text)

    def _sanitize_message(self, text: str) -> str:
        import re
        text = text.replace('\r', '').replace('\n', '||')
        return re.sub(r'[^\x20-\x7E]', '', text).strip()

    def _on_send_internet(self) -> None:
        cs = self.cs_edit.text().strip().upper()
        text = self._sanitize_message(self.msg_edit.toPlainText())
        if not cs or not text:
            return
        my_cs = _get_local_callsign()
        if not my_cs:
            QMessageBox.warning(self, "Send Failed", "No operator callsign configured in Settings.")
            return
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        msg_id = generate_time_based_id()
        message_data = f"{my_cs}: {cs} MSG ,{msg_id},{text},{{^%3}}"
        data_string  = f"DM:{now}\t0\t0\t30\t{message_data}"
        threading.Thread(
            target=self._submit_internet, args=(my_cs, data_string), daemon=True
        ).start()

    def _submit_internet(self, callsign: str, data_string: str) -> None:
        try:
            post = urllib.parse.urlencode({'cs': callsign, 'data': data_string}).encode()
            req  = urllib.request.Request(_DATAFEED_URL, data=post, method='POST')
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = resp.read().decode().strip()
            self._send_result.emit(result)
        except Exception as e:
            print(f"[QRZLookupDialog] Internet send error: {e}")
            self._send_result.emit("")

    def _on_send_result(self, result: str) -> None:
        if result.lstrip('-').isdigit():
            QMessageBox.information(self, "Message Sent", "Your message was sent.")
            self.msg_edit.clear()


# ── Dialog 2: JS8 Message (RF) ─────────────────────────────────────────────

class JS8MessageDialog(QDialog):
    """QRZ lookup with inline JS8 RF transmit controls (JS8 Message menu item)."""

    def __init__(self, program_background: str = "",
                 program_foreground: str = "",
                 module_background: str = "#f5f5f5",
                 module_foreground: str = "#333333",
                 tcp_pool=None,
                 connector_manager=None,
                 parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint
        )
        self._program_bg = program_background or _PROG_BG
        self._program_fg = program_foreground or _PROG_FG
        self._module_bg = module_background
        self._module_fg = module_foreground
        self._tcp_pool = tcp_pool
        self._connector_manager = connector_manager
        self._qrz_thread: Optional[_QRZThread] = None
        self.setWindowTitle("JS8 Message")
        self.setModal(True)
        self.setMinimumSize(825, 460)
        self.resize(900, 530)
        if os.path.exists("radiation-32.png"):
            self.setWindowIcon(QtGui.QIcon("radiation-32.png"))
        self._setup_ui()
        self._populate_rigs()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{self._module_bg}; }}"
            f"QLabel {{ color:{self._module_fg}; background-color: transparent; font-size: 13px; }}"
            f"QLineEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
            f"QComboBox {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px; combobox-popup:0; }}"
            f"QComboBox QAbstractItemView::item {{ min-height:22px; padding:0 6px; }}"
        )
        main = QVBoxLayout(self)
        main.setContentsMargins(15, 15, 15, 15)
        main.setSpacing(10)

        title = QLabel("JS8 MESSAGE")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Roboto Slab", -1, QFont.Black))
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"QLabel {{ background-color: {self._program_bg}; color: {self._program_fg}; "
            "font-size: 16px; padding-top: 9px; padding-bottom: 9px; }"
        )
        main.addWidget(title)

        search_row = QHBoxLayout()
        self.cs_edit = QLineEdit()
        self.cs_edit.setPlaceholderText("Enter callsign…")
        self.cs_edit.setMaxLength(12)
        self.cs_edit.setFont(_mono_font())
        self.cs_edit.setMinimumHeight(34)
        self.cs_edit.returnPressed.connect(self._search)
        self.cs_edit.textChanged.connect(self._force_upper)
        search_row.addWidget(self.cs_edit)
        self.btn_search = _btn("Search", COLOR_BTN_BLUE)
        self.btn_search.setFixedWidth(90)
        self.btn_search.setAutoDefault(False)
        self.btn_search.clicked.connect(self._search)
        search_row.addWidget(self.btn_search)
        main.addLayout(search_row)

        self.lbl_status = QLabel()
        self.lbl_status.setFont(QFont("Roboto"))
        self.lbl_status.setStyleSheet("QLabel { color:#888888; font-size:10px; font-weight:normal; }")
        main.addWidget(self.lbl_status)

        self.qrz_info = _QRZInfoSection(hdr_bg=self._program_bg, hdr_fg=self._program_fg, parent=self)
        self.qrz_info.image_width_ready.connect(self._adjust_for_image_width)
        self.qrz_info.show_no_data_placeholder()
        self.contact_memo_edit = self.qrz_info.add_memo_row()
        self.contact_memo_edit.editingFinished.connect(self._save_contact_memo)
        main.addWidget(self.qrz_info)

        # RF controls row
        rf_row = QHBoxLayout()
        rf_row.setSpacing(12)

        rig_lbl = QLabel("Rig:")
        rig_lbl.setFont(_lbl_font())
        self.rig_combo = QComboBox()
        self.rig_combo.setFont(_mono_font())
        self.rig_combo.setMinimumWidth(140)
        self.rig_combo.setMaxVisibleItems(30)
        self.rig_combo.setItemDelegate(QStyledItemDelegate(self.rig_combo))
        self.rig_combo.currentTextChanged.connect(self._on_rig_changed)
        rf_row.addWidget(rig_lbl)
        rf_row.addWidget(self.rig_combo)

        mode_lbl = QLabel("Mode:")
        mode_lbl.setFont(_lbl_font())
        self.mode_combo = QComboBox()
        self.mode_combo.setFont(_mono_font())
        self.mode_combo.setMaxVisibleItems(30)
        self.mode_combo.setItemDelegate(QStyledItemDelegate(self.mode_combo))
        self.mode_combo.addItems(["Normal", "Fast", "Turbo", "Ultra", "Slow"])
        rf_row.addWidget(mode_lbl)
        rf_row.addWidget(self.mode_combo)

        freq_lbl = QLabel("Frequency:")
        freq_lbl.setFont(_lbl_font())
        self.freq_edit = QLineEdit()
        self.freq_edit.setReadOnly(True)
        self.freq_edit.setFont(_mono_font())
        self.freq_edit.setPlaceholderText("—")
        self.freq_edit.setMaximumWidth(100)
        self.freq_edit.setStyleSheet(
            f"background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:2px 4px;"
            f" font-family:'Kode Mono'; font-size:13px;"
        )
        rf_row.addWidget(freq_lbl)
        rf_row.addWidget(self.freq_edit)
        rf_row.addStretch()
        main.addLayout(rf_row)

        # Message box — 2 rows, 100 char limit
        self.msg_edit = QPlainTextEdit()
        self.msg_edit.setFont(_mono_font())
        self.msg_edit.setPlaceholderText("Enter message… (100 characters max)")
        self.msg_edit.setStyleSheet(
            f"background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px;"
        )
        self.msg_edit.setFixedHeight(34)
        self.msg_edit.textChanged.connect(self._on_msg_changed)
        main.addWidget(self.msg_edit)

        main.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        self.btn_clear_msg = _btn("Clear", _COL_CANCEL)
        self.btn_clear_msg.setVisible(False)
        self.btn_clear_msg.clicked.connect(self.msg_edit.clear)
        btn_row.addWidget(self.btn_clear_msg)
        self.btn_transmit = _btn("Transmit", COLOR_BTN_CYAN)
        self.btn_transmit.setVisible(False)
        self.btn_transmit.clicked.connect(self._on_transmit)
        btn_row.addWidget(self.btn_transmit)
        self.btn_close = _btn("Close", _COL_CANCEL)
        self.btn_close.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_close)
        main.addLayout(btn_row)

    def _adjust_for_image_width(self, img_width: int) -> None:
        if img_width > 275:
            self.resize(self.width() + (img_width - 275), self.height())

    def _force_upper(self, text: str) -> None:
        if text != text.upper():
            self.cs_edit.blockSignals(True)
            pos = self.cs_edit.cursorPosition()
            self.cs_edit.setText(text.upper())
            self.cs_edit.setCursorPosition(pos)
            self.cs_edit.blockSignals(False)

    def _populate_rigs(self) -> None:
        self.rig_combo.clear()
        if self._tcp_pool:
            names = self._tcp_pool.get_all_rig_names()
            if len(names) == 1:
                self.rig_combo.addItems(names)
                self._on_rig_changed(names[0])
                return
            if len(names) > 1:
                self.rig_combo.addItem("Select a rig…")
                self.rig_combo.addItems(names)
                return
        self.rig_combo.addItem("No rigs configured")

    def _on_rig_changed(self, rig_name: str) -> None:
        if not self._tcp_pool or not rig_name or rig_name in ("No rigs configured", "Select a rig…"):
            self.freq_edit.setText("—")
            return
        client = self._tcp_pool.get_client(rig_name)
        if not client or not client.is_connected():
            self.freq_edit.setText("—")
            return
        # Show cached frequency immediately
        if client.frequency:
            self.freq_edit.setText(f"{client.frequency:.3f} MHz")
        else:
            self.freq_edit.setText("Fetching…")
        # Connect signal for live updates and request a fresh value
        try:
            client.frequency_received.disconnect(self._on_frequency_received)
        except (TypeError, RuntimeError):
            pass
        client.frequency_received.connect(self._on_frequency_received)
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(200, client.get_frequency)

    def _on_frequency_received(self, rig_name: str, dial_freq: int) -> None:
        self.freq_edit.setText(f"{dial_freq / 1_000_000:.3f} MHz")

    def _search(self) -> None:
        cs = self.cs_edit.text().strip().upper()
        if not cs:
            return
        self.lbl_status.setText(f"Looking up {cs}…")
        self.btn_search.setEnabled(False)
        self.qrz_info.clear()
        self.qrz_info.show_no_data_placeholder()
        _, username, password = load_qrz_config()
        self._qrz_thread = _QRZThread(cs, username, password)
        self._qrz_thread.result_ready.connect(self._on_qrz_result)
        self._qrz_thread.start()

    def _on_qrz_result(self, result) -> None:
        self.btn_search.setEnabled(True)
        if result:
            self.lbl_status.setText("")
            self.qrz_info.update_data(result)
            self.contact_memo_edit.blockSignals(True)
            self.contact_memo_edit.setText(result.get("memo") or "")
            self.contact_memo_edit.blockSignals(False)
        else:
            self.lbl_status.setText("No results found.")
            self.qrz_info.show_no_data_placeholder()

    def _on_msg_changed(self) -> None:
        text = self.msg_edit.toPlainText()
        if len(text) > 100:
            cursor = self.msg_edit.textCursor()
            pos = cursor.position()
            self.msg_edit.blockSignals(True)
            self.msg_edit.setPlainText(text[:100])
            from PyQt5.QtGui import QTextCursor
            c = self.msg_edit.textCursor()
            c.setPosition(min(pos, 100))
            self.msg_edit.setTextCursor(c)
            self.msg_edit.blockSignals(False)
            text = text[:100]
        has_text = bool(text.strip())
        self.btn_clear_msg.setVisible(has_text)
        self.btn_transmit.setVisible(has_text)

    def _save_contact_memo(self) -> None:
        cs = self.cs_edit.text().strip().upper()
        if not cs:
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE qrz SET memo = ? WHERE callsign = ?",
                    (self.contact_memo_edit.text(), cs)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[JS8MessageDialog] Contact memo save error: {e}")

    @staticmethod
    def _sanitize(text: str) -> str:
        import re
        text = text.replace('\r', '').replace('\n', '||')
        return re.sub(r'[^\x20-\x7E]', '', text).strip()

    def _on_transmit(self) -> None:
        cs = self.cs_edit.text().strip().upper()
        text = self._sanitize(self.msg_edit.toPlainText())
        if not cs or not text:
            return
        if not self._tcp_pool:
            QMessageBox.warning(self, "Transmit", "No TCP connection available.")
            return
        rig_name = self.rig_combo.currentText()
        if rig_name in ("No rigs configured", "Select a rig…"):
            QMessageBox.warning(self, "Transmit", "Please select a rig.")
            return
        client = self._tcp_pool.get_client(rig_name)
        if not client or not client.is_connected():
            QMessageBox.warning(self, "Transmit", f"Rig '{rig_name}' is not connected.")
            return
        my_cs = _get_local_callsign()
        if not my_cs:
            QMessageBox.warning(self, "Transmit", "No operator callsign configured in Settings.")
            return
        client.send_message("MODE.SET_SPEED", params={"SPEED": self.mode_combo.currentIndex()})
        msg_id = generate_time_based_id()
        payload = f"{my_cs}: {cs} MSG ,{msg_id},{text},{{^%}}"
        client.send_tx_message(payload)
        QMessageBox.information(self, "JS8 Message", "Message queued for transmission.")
        self.msg_edit.clear()


# ── Dialog 3: StatRep Detail ───────────────────────────────────────────────

class StatRepDetailDialog(QDialog):
    """Detail view for a StatRep row: QRZ info + 12 status indicators + map + comments."""

    pin_changed = pyqtSignal(bool)
    record_deleted = pyqtSignal()

    def __init__(self, record_id: str, callsign: str,
                 internet_available: bool = True,
                 backbone_url: str = "",
                 module_background: str = "#f5f5f5",
                 module_foreground: str = "#333333",
                 title_bar_background: str = "#555555",
                 title_bar_foreground: str = "#D2D0CF",
                 data_background: str = "#D2D0CF",
                 program_background: str = "",
                 program_foreground: str = "",
                 condition_green: str = "",
                 condition_yellow: str = "",
                 condition_red: str = "",
                 condition_gray: str = "",
                 tcp_pool=None,
                 connector_manager=None,
                 record_list: list = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint |
            Qt.WindowMaximizeButtonHint |
            Qt.WindowStaysOnTopHint
        )
        self._record_id = record_id
        self.callsign = callsign
        self.internet_available = internet_available
        self._backbone_url = backbone_url
        self._module_bg = module_background
        self._module_fg = module_foreground
        self._title_bg = title_bar_background
        self._title_fg = title_bar_foreground
        self._data_bg = data_background
        self._program_bg = program_background or _PROG_BG
        self._program_fg = program_foreground or _PROG_FG
        self._status_colors = {
            "1": (condition_green  or STATUS_COLORS["1"][0], STATUS_COLORS["1"][1]),
            "2": (condition_yellow or STATUS_COLORS["2"][0], STATUS_COLORS["2"][1]),
            "3": (condition_red    or STATUS_COLORS["3"][0], STATUS_COLORS["3"][1]),
            "4": (condition_gray   or STATUS_COLORS["4"][0], STATUS_COLORS["4"][1]),
        }
        self._tcp_pool = tcp_pool
        self._connector_manager = connector_manager
        self._thread: Optional[_QRZThread] = None
        self._rc_thread: Optional[_ReadCountThread] = None
        self._map_loaded = False
        self._last_nav: str = "older"
        self._record_list: list = list(record_list) if record_list else []
        self._global_id = 0
        self._row_data: dict = {}
        self._sr_datetime: str = ""
        self._statrep_lat: Optional[float] = None
        self._statrep_lon: Optional[float] = None
        self._statrep_grid: str = ""
        self.setWindowTitle(f"StatRep — {callsign}")
        self.setModal(True)
        self.setMinimumSize(996, 696)
        self.resize(996, 696)
        if os.path.exists("radiation-32.png"):
            self.setWindowIcon(QtGui.QIcon("radiation-32.png"))
        self._setup_ui()
        self._load_statrep()
        self._start_qrz()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{self._module_bg}; }}"
            f"QLabel {{ color:{self._module_fg}; background-color: transparent; font-size: 13px; }}"
        )
        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(8)

        self.qrz_info = _QRZInfoSection(hdr_bg=self._program_bg, hdr_fg=self._program_fg, parent=self)
        self.contact_memo_edit = self.qrz_info.add_memo_row()
        self.contact_memo_edit.editingFinished.connect(self._save_contact_memo)
        self.qrz_info.add_statrep_rows()
        self.statrep_memo_edit = self.qrz_info.add_statrep_memo_row()
        self.qrz_info.image_width_ready.connect(self._adjust_for_image_width)
        main.addWidget(self.qrz_info, 1)

        # Status grid
        sg_widget = QWidget()
        sg_widget.setStyleSheet(f"border-top:1px solid #D2D0CF; border-left:1px solid #D2D0CF;")
        sg_grid = QGridLayout(sg_widget)
        sg_grid.setContentsMargins(0, 0, 0, 0)
        sg_grid.setSpacing(0)
        self._squares: Dict[str, QLabel] = {}
        for col_idx, (label_text, _) in enumerate(STATUS_FIELDS):
            hdr = QLabel(label_text)
            hdr.setAlignment(Qt.AlignCenter)
            hdr.setFont(_lbl_font())
            hdr.setStyleSheet(
                f"QLabel {{ background-color:{self._title_bg}; color:{self._title_fg};"
                "border-right:1px solid #D2D0CF; border-bottom:1px solid #D2D0CF; padding: 5px 2px; }"
            )
            sg_grid.addWidget(hdr, 0, col_idx)
            sq = QLabel()
            sq.setFixedHeight(16)
            sq.setStyleSheet("QLabel { background-color:rgb(255,255,255); border-right:1px solid #D2D0CF; border-bottom:1px solid #D2D0CF; }")
            sq.setToolTip("No status")
            sg_grid.addWidget(sq, 1, col_idx)
            sg_grid.setColumnStretch(col_idx, 1)
            self._squares[label_text] = sq
        main.addWidget(sg_widget)

        lower = QHBoxLayout()
        lower.setSpacing(10)
        self.map_view = QWebEngineView()
        self.map_view.setFixedSize(480, 220)
        lower.addWidget(self.map_view, alignment=Qt.AlignTop)

        self.comments = QTextBrowser()
        self.comments.setFont(_mono_font())
        self.comments.setFixedHeight(220)
        self.comments.setMinimumWidth(480)
        self.comments.setStyleSheet(
            f"background-color:{self._data_bg}; color:#000000;"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px;"
        )
        self.comments.setOpenLinks(False)
        self.comments.anchorClicked.connect(lambda url: QDesktopServices.openUrl(url))
        lower.addWidget(self.comments)
        main.addLayout(lower)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.pin_toggle = _ToggleSwitch()
        self.pin_toggle.toggled.connect(self._save_pinned)
        self.lbl_pin = QLabel("Pinned")
        self.lbl_pin.setFont(_lbl_font())
        btn_row.addWidget(self.pin_toggle)
        btn_row.addWidget(self.lbl_pin)
        btn_row.addStretch()

        self.btn_delete = _btn("Delete", COLOR_BTN_RED)
        self.btn_delete.clicked.connect(self._on_delete)
        btn_row.addWidget(self.btn_delete)

        btn_newer = _btn("Next", _COL_NAV)
        btn_newer.clicked.connect(self._on_newer)
        btn_row.addWidget(btn_newer)

        btn_older = _btn("Previous", _COL_NAV)
        btn_older.clicked.connect(self._on_older)
        btn_row.addWidget(btn_older)

        self.btn_message_sr = _btn("Message", COLOR_BTN_BLUE)
        self.btn_message_sr.clicked.connect(self._on_message_clicked)
        btn_row.addWidget(self.btn_message_sr)

        btn_brevity = _btn("Brevity", _COL_PURPLE)
        btn_brevity.clicked.connect(self._on_brevity)
        btn_row.addWidget(btn_brevity)

        btn_forward = _btn("Forward", COLOR_BTN_CYAN)
        btn_forward.clicked.connect(self._on_forward)
        btn_row.addWidget(btn_forward)

        btn_close = _btn("Close", _COL_CANCEL)
        btn_close.clicked.connect(self.reject)
        btn_row.addWidget(btn_close)

        main.addLayout(btn_row)

    def _adjust_for_image_width(self, img_width: int) -> None:
        if img_width > 400:
            self.resize(self.width() + (img_width - 400), self.height())

    def _load_statrep(self) -> None:
        """Load status fields, comments, and map from the database."""
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT datetime, global_id, map, power, water, med, telecom, travel,
                           internet, fuel, food, crime, civil, political, comments, grid, sr_id,
                           freq, target, memo, pinned, source, scope
                    FROM statrep WHERE id = ?
                """, (self._record_id,))
                row = cursor.fetchone()
        except sqlite3.Error as e:
            print(f"[StatRepDetailDialog] DB error: {e}")
            return
        if not row:
            return

        self._sr_datetime = row[0] or ""

        self._row_data = {
            "map": row[2], "power": row[3], "water": row[4],
            "med": row[5], "telecom": row[6], "travel": row[7],
            "internet": row[8], "fuel": row[9], "food": row[10],
            "crime": row[11], "civil": row[12], "political": row[13],
            "comments": row[14], "grid": row[15],
            "sr_id": row[16],
            "scope": row[22],
            "origin_callsign": self.callsign,
        }

        global_id = row[1] or 0
        self._global_id = global_id
        freq_mhz = (float(row[17]) / 1_000_000) if row[17] else 0.0
        sr_id    = row[16] or ""
        group    = ("@" + (row[18] or "").lstrip("@")) if (row[18] or "").strip("@") else ""
        sr_grid  = row[15] or ""
        source   = row[21] if row[21] is not None else 0
        _source_map = {1: "RF via JS8Call", 2: "Internet", 3: "Internet Only"}
        source_text  = _source_map.get(int(source), "Unknown")

        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.qrz_info.lbl_sr_posted.setText(
            f'<span style="{_k}">Posted:</span>  {row[0]}' if row[0] else f'<span style="{_k}">Posted:</span>'
        )
        self.qrz_info.lbl_sr_source.setText(f'<span style="{_k}">Received via:</span>  {source_text}')
        self.qrz_info.lbl_sr_global_id.setText(
            f'<span style="{_k}">Global ID:</span>  {global_id}' if global_id else f'<span style="{_k}">Global ID:</span>'
        )
        self.qrz_info.lbl_sr_group.setText(
            f'<span style="{_k}">Group:</span>  {group}' if group else f'<span style="{_k}">Group:</span>'
        )
        self.qrz_info.lbl_sr_grid.setText(
            f'<span style="{_k}">Grid:</span>  {sr_grid}' if sr_grid else f'<span style="{_k}">Grid:</span>'
        )
        self.qrz_info.lbl_sr_freq.setText(
            f'<span style="{_k}">Freq:</span>  {freq_mhz:.3f} MHz' if freq_mhz else f'<span style="{_k}">Freq:</span>'
        )
        self.qrz_info.lbl_sr_sr_id.setText(
            f'<span style="{_k}">Statrep ID:</span>  {sr_id}' if sr_id else f'<span style="{_k}">Statrep ID:</span>'
        )
        self.qrz_info.lbl_sr_delivered.setText(f'<span style="{_k}">Delivered To:</span>')

        if global_id and self._backbone_url and self.internet_available:
            local_cs = _get_local_callsign()
            if local_cs:
                self._rc_thread = _ReadCountThread(self._backbone_url, local_cs, global_id)
                self._rc_thread.count_ready.connect(self._on_read_count)
                self._rc_thread.start()

        for i, (label_text, _) in enumerate(STATUS_FIELDS):
            val = str(row[i + 2]) if row[i + 2] is not None else ""
            sq = self._squares[label_text]
            color_str, tip = self._status_colors.get(val, ("rgb(255,255,255)", "No status"))
            sq.setStyleSheet(f"QLabel {{ background-color:{color_str}; border:1px solid #D2D0CF; }}")
            sq.setToolTip(tip)

        self.comments.setHtml(_text_to_html((row[14] or "").replace("||", "\n"), self._data_bg))

        self.statrep_memo_edit.blockSignals(True)
        self.statrep_memo_edit.setPlainText(row[19] or "")
        self.statrep_memo_edit.blockSignals(False)

        self.pin_toggle.blockSignals(True)
        self.pin_toggle.setChecked(bool(row[20]))
        self.pin_toggle.blockSignals(False)

        grid = row[15]
        if grid:
            try:
                coords = mh.to_location(grid, center=True)
                lat, lon = float(coords[0]), float(coords[1])
                self._statrep_lat = lat
                self._statrep_lon = lon
                self._statrep_grid = grid[:4].upper()
                self.map_view.setHtml(
                    _make_map_html(lat, lon, self.internet_available),
                    QUrl("http://localhost/")
                )
                self._map_loaded = True
            except Exception as e:
                print(f"[StatRepDetailDialog] Map error for grid {grid}: {e}")

    def _on_read_count(self, text: str) -> None:
        if not text:
            return
        count_str = text.split(",", 1)[0].strip()
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.qrz_info.lbl_sr_delivered.setText(f'<span style="{_k}">Delivered To:</span>  {count_str} CommStat users')

    def _save_pinned(self, checked: bool) -> None:
        """Save pinned state to the database and notify the main window."""
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE statrep SET pinned = ? WHERE id = ?",
                    (1 if checked else 0, self._record_id)
                )
                conn.commit()
            self.pin_changed.emit(checked)
        except sqlite3.Error as e:
            print(f"[StatRepDetailDialog] Pinned save error: {e}")

    def _on_brevity(self) -> None:
        selected = self.comments.textCursor().selectedText().strip()
        if not selected:
            matches = _BREVITY_RE.findall(self.comments.toPlainText())
            if matches:
                selected = matches[0]
        from brevity import BrevityApp
        win = BrevityApp(self._module_bg, self._module_fg, selected or "", parent=self)
        self._brevity_window = win
        win.show()
        parent_center = self.frameGeometry().center()
        win_rect = win.frameGeometry()
        win_rect.moveCenter(parent_center)
        win.move(win_rect.topLeft())

    def _on_forward(self) -> None:
        if not self._tcp_pool or not self._connector_manager or not self._row_data:
            return

        if self._sr_datetime:
            try:
                sr_dt_str = self._sr_datetime.replace(" UTC", "").strip()
                sr_dt = datetime.datetime.strptime(sr_dt_str, "%Y-%m-%d %H:%M:%S")
                sr_dt = sr_dt.replace(tzinfo=datetime.timezone.utc)
                age = datetime.datetime.now(datetime.timezone.utc) - sr_dt
                if age.total_seconds() > 86400:
                    from PyQt5.QtWidgets import QMessageBox
                    msg = QMessageBox(self)
                    msg.setWindowTitle("Cannot Forward")
                    msg.setText(
                        "This Status Report cannot be forwarded because it is more than 24 hours old."
                    )
                    msg.setIcon(QMessageBox.Warning)
                    msg.setStandardButtons(QMessageBox.Close)
                    msg.exec_()
                    return
            except (ValueError, TypeError):
                pass

        from statrep import StatRepDialog
        dlg = StatRepDialog(
            self._tcp_pool, self._connector_manager, self,
            module_background=self._module_bg,
        )
        dlg.prefill(self._row_data)
        dlg.exec_()

    def _on_message_clicked(self) -> None:
        dlg = QRZLookupDialog(
            module_background=self._module_bg,
            module_foreground=self._module_fg,
            program_background=self._program_bg,
            program_foreground=self._program_fg,
            initial_callsign=self.callsign,
            parent=self,
        )
        dlg.exec_()

    def _on_newer(self) -> None:
        self._last_nav = "newer"
        self._navigate("newer")

    def _on_older(self) -> None:
        self._last_nav = "older"
        self._navigate("older")

    def _navigate(self, direction: str) -> None:
        if self._record_list:
            idx = next(
                (i for i, (rid, _) in enumerate(self._record_list)
                 if str(rid) == str(self._record_id)),
                None
            )
            if idx is None:
                return
            if direction == "newer":
                if idx <= 0:
                    return
                next_id, next_cs = self._record_list[idx - 1]
            else:
                if idx >= len(self._record_list) - 1:
                    return
                next_id, next_cs = self._record_list[idx + 1]
            self._reload(next_id, next_cs)
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cursor = conn.cursor()
                if direction == "newer":
                    cursor.execute(
                        "SELECT id, from_callsign FROM statrep WHERE id > ? ORDER BY id ASC LIMIT 1",
                        (self._record_id,)
                    )
                else:
                    cursor.execute(
                        "SELECT id, from_callsign FROM statrep WHERE id < ? ORDER BY id DESC LIMIT 1",
                        (self._record_id,)
                    )
                row = cursor.fetchone()
        except sqlite3.Error as e:
            print(f"[StatRepDetailDialog] Navigate error: {e}")
            return
        if not row:
            return
        self._reload(row[0], row[1] or "")

    def _reload(self, record_id, callsign: str) -> None:
        self._save_statrep_memo()
        if self._thread and self._thread.isRunning():
            self._thread.result_ready.disconnect()
            self._thread = None
        if self._rc_thread and self._rc_thread.isRunning():
            self._rc_thread.count_ready.disconnect()
            self._rc_thread = None
        self._record_id = record_id
        self.callsign = callsign
        self._map_loaded = False
        self._global_id = 0
        self._row_data = {}
        self._sr_datetime = ""
        self._statrep_lat = None
        self._statrep_lon = None
        self._statrep_grid = ""
        self.setWindowTitle(f"StatRep — {callsign}")
        self.map_view.setHtml("", QUrl("http://localhost/"))
        self._load_statrep()
        self._start_qrz()

    def _on_delete(self) -> None:
        from ui_helpers import confirm
        if not confirm(
            self, "Confirm Delete",
            "Status Report records will be deleted from all CommStat users if you are "
            "the creator of the record. If you are not the creator, the record will only "
            "be deleted locally.",
        ):
            return
        if self._global_id and self._backbone_url:
            try:
                local_cs = _get_local_callsign()
                url = (f"{self._backbone_url}/statrep-delete-808585.php"
                       f"?cs={urllib.parse.quote(local_cs)}&id={self._global_id}")
                with urllib.request.urlopen(url, timeout=10):
                    pass
            except Exception as e:
                print(f"[StatRepDetailDialog] Delete request failed: {e}")
        deleted_id = self._record_id
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute("DELETE FROM statrep WHERE id = ?", (deleted_id,))
                conn.commit()
        except sqlite3.Error:
            pass
        self.record_deleted.emit()
        direction = self._last_nav
        if self._record_list:
            idx = next(
                (i for i, (rid, _) in enumerate(self._record_list)
                 if str(rid) == str(deleted_id)),
                None
            )
            if idx is not None:
                self._record_list.pop(idx)
            if self._record_list:
                if direction == "newer":
                    next_idx = max(0, idx - 1) if idx is not None else 0
                else:
                    next_idx = min(idx, len(self._record_list) - 1) if idx is not None else len(self._record_list) - 1
                next_id, next_cs = self._record_list[next_idx]
                self._record_id = next_id
                self._reload(next_id, next_cs)
            else:
                self.accept()
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cursor = conn.cursor()
                if direction == "newer":
                    cursor.execute(
                        "SELECT id, from_callsign FROM statrep WHERE id > ? ORDER BY id ASC LIMIT 1",
                        (deleted_id,)
                    )
                else:
                    cursor.execute(
                        "SELECT id, from_callsign FROM statrep WHERE id < ? ORDER BY id DESC LIMIT 1",
                        (deleted_id,)
                    )
                row = cursor.fetchone()
        except sqlite3.Error:
            row = None
        if row:
            self._record_id = row[0]
            self._reload(row[0], row[1] or "")
        else:
            self.accept()

    def _start_qrz(self) -> None:
        cached_fresh = get_qrz_cached(self.callsign)
        cached_any   = cached_fresh or get_qrz_cached(self.callsign, include_stale=True)
        is_active, username, password = load_qrz_config()

        if not username:
            found_str = "found" if cached_any else "NOT found"
            self.qrz_info.set_qrz_status(
                f"QRZ Subscription not configured — {self.callsign} {found_str} in local database"
            )
            if cached_any:
                self._on_qrz_result(cached_any)
            else:
                self.qrz_info.show_no_data_placeholder()
            return

        if not is_active:
            found_str = "found" if cached_any else "NOT found"
            self.qrz_info.set_qrz_status(
                f"QRZ Subscription not enabled — {self.callsign} {found_str} in local database"
            )
            if cached_any:
                self._on_qrz_result(cached_any)
            else:
                self.qrz_info.show_no_data_placeholder()
            return

        if cached_fresh:
            self._on_qrz_result(cached_fresh)
            return

        # No fresh cache (missing or stale) — live lookup; QRZClient handles stale refresh
        self._thread = _QRZThread(self.callsign, username, password)
        self._thread.result_ready.connect(self._on_qrz_result)
        self._thread.start()

    def _save_contact_memo(self) -> None:
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE qrz SET memo = ? WHERE callsign = ?",
                    (self.contact_memo_edit.text(), self.callsign)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[StatRepDetailDialog] Contact memo save error: {e}")

    def _save_statrep_memo(self) -> None:
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE statrep SET memo = ? WHERE id = ?",
                    (self.statrep_memo_edit.toPlainText(), self._record_id)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[StatRepDetailDialog] StatRep memo save error: {e}")

    def closeEvent(self, event) -> None:
        self._save_statrep_memo()
        super().closeEvent(event)

    def _on_qrz_result(self, result) -> None:
        if not result:
            return
        self.qrz_info.update_data(result)
        self.contact_memo_edit.blockSignals(True)
        self.contact_memo_edit.setText(result.get("memo") or "")
        self.contact_memo_edit.blockSignals(False)
        d = _normalize_qrz(result)

        if not self._map_loaded:
            if d["lat"] and d["lon"]:
                try:
                    lat, lon = float(d["lat"]), float(d["lon"])
                    self.map_view.setHtml(
                        _make_map_html(lat, lon, self.internet_available),
                        QUrl("http://localhost/")
                    )
                    self._map_loaded = True
                except (ValueError, TypeError):
                    pass
        else:
            qrz_grid = (d.get("grid") or "")[:4].upper()
            if qrz_grid and qrz_grid != self._statrep_grid and d["lat"] and d["lon"]:
                try:
                    qrz_lat, qrz_lon = float(d["lat"]), float(d["lon"])
                    self.map_view.setHtml(
                        _make_map_html(
                            self._statrep_lat, self._statrep_lon,
                            self.internet_available,
                            extra_lat=qrz_lat, extra_lon=qrz_lon,
                        ),
                        QUrl("http://localhost/")
                    )
                except (ValueError, TypeError):
                    pass


# ── Dialog 3: Message Detail ───────────────────────────────────────────────

import html as _html_mod
import re as _re

_URL_RE = _re.compile(r'(https?://[^\s<>"\']+)', _re.IGNORECASE)
_BREVITY_RE = _re.compile(r'\b([0-9][A-Z]{5})\b')


def _text_to_html(text: str, bg: str) -> str:
    """Convert plain text to HTML, turning URLs into clickable links and highlighting brevity codes."""
    escaped = _html_mod.escape(text)
    highlighted = _BREVITY_RE.sub(
        r'<span style="background-color:#FFD700;font-weight:bold;">\1</span>',
        escaped
    )
    linked = _URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" style="color:#0078d7;">{m.group(1)}</a>',
        highlighted
    )
    lines = linked.replace("\n", "<br>")
    return (
        f'<html><body style="background-color:{bg};color:#000000;'
        f'font-family:\'Kode Mono\';font-size:13px;">{lines}</body></html>'
    )


class MessageDetailDialog(QDialog):
    """Detail view for a Message row: QRZ info + map + message text."""

    record_deleted = pyqtSignal()

    def __init__(self, callsign: str, message_text: str,
                 internet_available: bool = True,
                 module_background: str = "#f5f5f5",
                 module_foreground: str = "#333333",
                 data_background: str = "#D2D0CF",
                 program_background: str = "",
                 program_foreground: str = "",
                 msg_id: str = "",
                 parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint |
            Qt.WindowMaximizeButtonHint |
            Qt.WindowStaysOnTopHint
        )
        self.callsign = callsign
        self.message_text = message_text
        self.internet_available = internet_available
        self._module_bg = module_background
        self._module_fg = module_foreground
        self._data_bg = data_background
        self._program_bg = program_background or _PROG_BG
        self._program_fg = program_foreground or _PROG_FG
        self._msg_id = msg_id
        self._thread: Optional[_QRZThread] = None
        self._map_loaded = False
        self._deleted_any = False
        self._last_nav: str = "older"
        self._msg_datetime: str = ""
        self.setWindowTitle(f"Message — {callsign}")
        self.setModal(True)
        self.setMinimumSize(996, 555)
        self.resize(996, 555)
        if os.path.exists("radiation-32.png"):
            self.setWindowIcon(QtGui.QIcon("radiation-32.png"))
        self._setup_ui()
        self._fetch_datetime()
        self._start_qrz()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{self._module_bg}; }}"
            f"QLabel {{ color:{self._module_fg}; background-color: transparent; font-size: 13px; }}"
        )
        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(8)

        self.qrz_info = _QRZInfoSection(hdr_bg=self._program_bg, hdr_fg=self._program_fg, parent=self)
        self.contact_memo_edit = self.qrz_info.add_memo_row(trailing_space=0)
        self.contact_memo_edit.editingFinished.connect(self._save_contact_memo)
        main.addWidget(self.qrz_info)
        main.addStretch(1)

        lower = QHBoxLayout()
        lower.setSpacing(10)
        self.map_view = QWebEngineView()
        self.map_view.setFixedSize(480, 220)
        lower.addWidget(self.map_view, alignment=Qt.AlignTop)

        self.msg_text = QTextBrowser()
        self.msg_text.setFont(_mono_font())
        self.msg_text.setFixedHeight(220)
        self.msg_text.setMinimumWidth(480)
        self.msg_text.setStyleSheet(
            f"background-color:{self._data_bg}; border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px;"
        )
        self.msg_text.setOpenLinks(False)
        self.msg_text.anchorClicked.connect(lambda url: QDesktopServices.openUrl(url))
        self.msg_text.setHtml(_text_to_html(self.message_text.replace("||", "\n"), self._data_bg))
        lower.addWidget(self.msg_text)
        main.addLayout(lower)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        self.btn_delete = _btn("Delete", COLOR_BTN_RED)
        self.btn_delete.clicked.connect(self._on_delete)
        btn_row.addWidget(self.btn_delete)

        btn_newer = _btn("Next", _COL_NAV)
        btn_newer.clicked.connect(self._on_newer)
        btn_row.addWidget(btn_newer)

        btn_older = _btn("Previous", _COL_NAV)
        btn_older.clicked.connect(self._on_older)
        btn_row.addWidget(btn_older)

        self.btn_reply = _btn("Reply", COLOR_BTN_BLUE)
        self.btn_reply.clicked.connect(self._on_reply_clicked)
        btn_row.addWidget(self.btn_reply)

        self.btn_message_msg = _btn("Message", COLOR_BTN_BLUE)
        self.btn_message_msg.clicked.connect(self._on_message_clicked)
        btn_row.addWidget(self.btn_message_msg)

        self.btn_close = _btn("Close", _COL_CANCEL)
        self.btn_close.clicked.connect(self._on_close_clicked)
        btn_row.addWidget(self.btn_close)

        main.addLayout(btn_row)

    def _save_contact_memo(self) -> None:
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE qrz SET memo = ? WHERE callsign = ?",
                    (self.contact_memo_edit.text(), self.callsign)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[MessageDetailDialog] Contact memo save error: {e}")

    def _on_message_clicked(self) -> None:
        dlg = QRZLookupDialog(
            module_background=self._module_bg,
            module_foreground=self._module_fg,
            program_background=self._program_bg,
            program_foreground=self._program_fg,
            initial_callsign=self.callsign,
            parent=self,
        )
        dlg.exec_()

    def _on_reply_clicked(self) -> None:
        original = self.message_text.replace("||", "\n")
        prefill = "\n\n----------\n" + original
        dlg = QRZLookupDialog(
            module_background=self._module_bg,
            module_foreground=self._module_fg,
            program_background=self._program_bg,
            program_foreground=self._program_fg,
            initial_callsign=self.callsign,
            initial_message=prefill,
            parent=self,
        )
        dlg.exec_()

    def _on_close_clicked(self) -> None:
        if self._deleted_any:
            self.accept()
        else:
            self.reject()

    def _fetch_datetime(self) -> None:
        if not self._msg_id:
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cur = conn.cursor()
                cur.execute("SELECT datetime FROM messages WHERE msg_id = ?", (self._msg_id,))
                row = cur.fetchone()
                self._msg_datetime = row[0] if row else ""
        except sqlite3.Error:
            pass

    def _reload(self, msg_id: str, callsign: str, message_text: str, msg_datetime: str) -> None:
        self._msg_id = msg_id
        self.callsign = callsign
        self.message_text = message_text
        self._msg_datetime = msg_datetime
        self.setWindowTitle(f"Message — {callsign}")
        self.msg_text.setHtml(_text_to_html(message_text.replace("||", "\n"), self._data_bg))
        self._map_loaded = False
        self.map_view.setHtml("", QUrl("http://localhost/"))
        self.contact_memo_edit.blockSignals(True)
        self.contact_memo_edit.clear()
        self.contact_memo_edit.blockSignals(False)
        self.qrz_info.update_data({"call": callsign})
        if self._thread is not None:
            try:
                self._thread.result_ready.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._thread = None
        self._start_qrz()

    def _on_newer(self) -> None:
        self._last_nav = "newer"
        self._navigate("newer")

    def _on_older(self) -> None:
        self._last_nav = "older"
        self._navigate("older")

    def _navigate(self, direction: str) -> None:
        if not self._msg_datetime:
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cur = conn.cursor()
                if direction == "newer":
                    cur.execute(
                        "SELECT msg_id, from_callsign, message, datetime FROM messages "
                        "WHERE datetime > ? ORDER BY datetime ASC LIMIT 1",
                        (self._msg_datetime,)
                    )
                else:
                    cur.execute(
                        "SELECT msg_id, from_callsign, message, datetime FROM messages "
                        "WHERE datetime < ? ORDER BY datetime DESC LIMIT 1",
                        (self._msg_datetime,)
                    )
                row = cur.fetchone()
        except sqlite3.Error as e:
            print(f"[MessageDetailDialog] Navigate error: {e}")
            return
        if not row:
            return
        self._reload(row[0], row[1] or "", row[2] or "", row[3] or "")

    def _on_delete(self) -> None:
        deleted_dt = self._msg_datetime
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cur = conn.cursor()
                if not deleted_dt:
                    cur.execute("SELECT datetime FROM messages WHERE msg_id = ?", (self._msg_id,))
                    row = cur.fetchone()
                    deleted_dt = row[0] if row else ""
                cur.execute("DELETE FROM messages WHERE msg_id = ?", (self._msg_id,))
                conn.commit()
                self._deleted_any = True
                next_row = None
                if deleted_dt:
                    direction = self._last_nav
                    if direction == "newer":
                        cur.execute(
                            "SELECT msg_id, from_callsign, message, datetime FROM messages "
                            "WHERE datetime > ? ORDER BY datetime ASC LIMIT 1",
                            (deleted_dt,)
                        )
                    else:
                        cur.execute(
                            "SELECT msg_id, from_callsign, message, datetime FROM messages "
                            "WHERE datetime < ? ORDER BY datetime DESC LIMIT 1",
                            (deleted_dt,)
                        )
                    next_row = cur.fetchone()
        except sqlite3.Error:
            self.accept()
            return
        self.record_deleted.emit()
        if not next_row:
            self.accept()
            return
        self._reload(next_row[0], next_row[1] or "", next_row[2] or "", next_row[3] or "")

    def _start_qrz(self) -> None:
        cached_fresh = get_qrz_cached(self.callsign)
        cached_any   = cached_fresh or get_qrz_cached(self.callsign, include_stale=True)
        is_active, username, password = load_qrz_config()

        if not username:
            found_str = "found" if cached_any else "NOT found"
            self.qrz_info.set_qrz_status(
                f"QRZ Subscription not configured — {self.callsign} {found_str} in local database"
            )
            if cached_any:
                self._on_qrz_result(cached_any)
            else:
                self.qrz_info.show_no_data_placeholder()
            return

        if not is_active:
            found_str = "found" if cached_any else "NOT found"
            self.qrz_info.set_qrz_status(
                f"QRZ Subscription not enabled — {self.callsign} {found_str} in local database"
            )
            if cached_any:
                self._on_qrz_result(cached_any)
            else:
                self.qrz_info.show_no_data_placeholder()
            return

        if cached_fresh:
            self._on_qrz_result(cached_fresh)
            return

        # No fresh cache (missing or stale) — live lookup; QRZClient handles stale refresh
        self._thread = _QRZThread(self.callsign, username, password)
        self._thread.result_ready.connect(self._on_qrz_result)
        self._thread.start()

    def _on_qrz_result(self, result) -> None:
        if not result:
            return
        self.qrz_info.update_data(result)
        self.contact_memo_edit.blockSignals(True)
        self.contact_memo_edit.setText(result.get("memo") or "")
        self.contact_memo_edit.blockSignals(False)
        d = _normalize_qrz(result)
        if not self._map_loaded:
            lat, lon = None, None
            if d["lat"] and d["lon"]:
                try:
                    lat, lon = float(d["lat"]), float(d["lon"])
                except (ValueError, TypeError):
                    pass
            if lat is None and d["grid"]:
                try:
                    coords = mh.to_location(d["grid"], center=True)
                    lat, lon = float(coords[0]), float(coords[1])
                except Exception:
                    pass
            if lat is not None and lon is not None:
                self._map_loaded = True
                self.map_view.setHtml(
                    _make_map_html(lat, lon, self.internet_available),
                    QUrl("http://localhost/")
                )
        # map already loaded — nothing more to do for message detail view


# ── Dialog: Delivery Confirmation popup (backbone ::DELIVERED::) ──────────

class DeliveryConfirmationDialog(QDialog):
    """Two-column popup shown when the backbone confirms a message delivery.

    Patterned after the QRZ Lookup dialog:
      - Title bar (program colors): "DELIVERY CONFIRMATION"
      - Column 1: QRZ data for the recipient (no QRZ profile URL)
      - Column 2: QRZ profile photo at fixed 120 px height; the dialog
                  expands horizontally to accommodate wider images
      - Below: read-only text box containing the delivered message
    """

    _PHOTO_H = 140
    _PHOTO_MAX_W = 440      # cap photo width — wide banners shrink in height to fit
    _PHOTO_DEFAULT_W = 460  # column-2 budget at default dialog width; grow only if exceeded

    def __init__(self, callsign: str, message: str,
                 module_background: str = "#f5f5f5",
                 module_foreground: str = "#333333",
                 program_background: str = "",
                 program_foreground: str = "",
                 parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint
        )
        self._callsign = (callsign or "").strip().upper()
        self._message = (message or "").replace("||", "\n")
        self._module_bg = module_background
        self._module_fg = module_foreground
        self._program_bg = program_background or _PROG_BG
        self._program_fg = program_foreground or _PROG_FG
        self._img_loader: Optional[_ImageLoader] = None
        self._gif_movie: Optional[QMovie] = None
        self._qrz_thread: Optional[_QRZThread] = None
        self.setWindowTitle("Delivery Confirmation")
        self.setModal(True)
        self.resize(510, 440)
        self.setMinimumSize(510, 440)
        if os.path.exists("radiation-32.png"):
            self.setWindowIcon(QtGui.QIcon("radiation-32.png"))
        self._setup_ui()
        self._populate_qrz()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{self._module_bg}; }}"
            f"QLabel {{ color:{self._module_fg}; background-color: transparent; font-size: 13px; }}"
            f"QPlainTextEdit {{ background-color:#e9ecef; color:#333333;"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
        )

        main = QVBoxLayout(self)
        main.setContentsMargins(15, 15, 15, 15)
        main.setSpacing(10)

        # Title bar (program colors)
        title = QLabel("Delivery Confirmation")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Roboto Slab", -1, QFont.Black))
        title.setStyleSheet(
            f"QLabel {{ background-color: {self._program_bg}; color: {self._program_fg};"
            " font-size: 16px; padding-top: 9px; padding-bottom: 9px; }"
        )
        main.addWidget(title)

        # ── Two-column row ───────────────────────────────────────────────
        cols = QHBoxLayout()
        cols.setSpacing(60)

        # Column 1: QRZ data (without URL)
        self._grid = QGridLayout()
        self._grid.setSpacing(2)
        self._grid.setColumnStretch(0, 0)

        self.lbl_call    = QLabel(); self.lbl_call.setFont(_mono_font())
        self.lbl_name    = QLabel(); self.lbl_name.setFont(_mono_font())
        self.lbl_addr1   = QLabel(); self.lbl_addr1.setFont(_mono_font())
        self.lbl_addr2   = QLabel(); self.lbl_addr2.setFont(_mono_font())
        self.lbl_grid    = QLabel(); self.lbl_grid.setFont(_mono_font())
        self.lbl_county  = QLabel(); self.lbl_county.setFont(_mono_font())
        self.lbl_country = QLabel(); self.lbl_country.setFont(_mono_font())

        for row, w in enumerate((
            self.lbl_call, self.lbl_name, self.lbl_addr1, self.lbl_addr2,
            self.lbl_grid, self.lbl_county, self.lbl_country,
        )):
            self._grid.addWidget(w, row, 0)
        self._grid.setRowStretch(self._grid.rowCount(), 1)
        cols.addLayout(self._grid, 0)

        # Column 2: photo
        right = QVBoxLayout()
        right.setAlignment(Qt.AlignTop | Qt.AlignRight)
        right.setSpacing(0)
        self.lbl_image = QLabel()
        self.lbl_image.setAlignment(Qt.AlignTop | Qt.AlignRight)
        self.lbl_image.setFixedHeight(self._PHOTO_H)
        self.lbl_image.setStyleSheet("QLabel { border:none; padding:0px; }")
        right.addWidget(self.lbl_image)
        right.addStretch()
        cols.addLayout(right, 1)

        main.addLayout(cols)

        # ── Read-only message text box ───────────────────────────────────
        self.msg_view = QPlainTextEdit()
        self.msg_view.setFont(_mono_font())
        self.msg_view.setReadOnly(True)
        self.msg_view.setPlainText(self._message)
        from PyQt5.QtGui import QFontMetrics
        _fm = QFontMetrics(self.msg_view.font())
        self.msg_view.setFixedHeight(_fm.lineSpacing() * 4 + 14 + 40)
        main.addWidget(self.msg_view)

        confirm_lbl = QLabel("This Message Was Delivered Successfully")
        confirm_lbl.setAlignment(Qt.AlignCenter)
        confirm_lbl.setFont(QFont("Roboto", -1, QFont.Bold))
        confirm_lbl.setStyleSheet(
            f"QLabel {{ color:{self._module_fg}; background-color: transparent;"
            " font-family:Roboto; font-size:13px; font-weight:bold; padding-top:6px; }}"
        )
        main.addWidget(confirm_lbl)
        main.addStretch()

        # ── Close button ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        self.btn_close = _btn("Close", _COL_CANCEL)
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)
        main.addLayout(btn_row)

    # ── QRZ population ────────────────────────────────────────────────────

    def _populate_qrz(self) -> None:
        """Show cached QRZ data immediately; fall back to API lookup when missing."""
        is_active, username, password = load_qrz_config()
        cached = (
            get_qrz_cached(self._callsign)
            or get_qrz_cached(self._callsign, include_stale=True)
        )
        if cached:
            self._update_data(cached)
            return

        self._show_placeholder()
        if is_active and username:
            self._qrz_thread = _QRZThread(self._callsign, username, password)
            self._qrz_thread.result_ready.connect(self._on_qrz_result)
            self._qrz_thread.start()

    def _on_qrz_result(self, result) -> None:
        if result:
            self._update_data(result)

    def _update_data(self, data: dict) -> None:
        d = _normalize_qrz(data)
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"

        self.lbl_call.setText(f"<span style='{_k}'>Callsign:</span> {d['call']}")
        self.lbl_name.setText(f"<b>{d['name']}</b>" if d["name"] else "")
        self.lbl_addr1.setText(d["addr1"])
        city_state = ", ".join(x for x in (d["addr2"], d["state"]) if x)
        if d["zip"]:
            city_state = (city_state + " " + d["zip"]).strip()
        self.lbl_addr2.setText(city_state)

        self.lbl_grid.setText(
            f'<span style="{_k}">Grid:</span> {d["grid"]}' if d["grid"] else ""
        )
        self.lbl_county.setText(
            f'<span style="{_k}">County:</span> {d["county"]}' if d["county"] else ""
        )
        self.lbl_country.setText(
            f'<span style="{_k}">Country:</span> {d["country"]}' if d["country"] else ""
        )

        if d["image"]:
            self._img_loader = _ImageLoader(
                d["image"], max_size=(self._PHOTO_MAX_W, self._PHOTO_H)
            )
            self._img_loader.image_loaded.connect(self._on_image_loaded)
            self._img_loader.gif_loaded.connect(self._on_gif_loaded)
            self._img_loader.start()
        else:
            self._load_default_image()

    def _show_placeholder(self) -> None:
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.lbl_call.setText(f"<span style='{_k}'>Callsign:</span> {self._callsign}")
        self.lbl_grid.setText(f"<span style='{_k}'>Grid:</span>")
        self.lbl_county.setText(f"<span style='{_k}'>County:</span>")
        self.lbl_country.setText(f"<span style='{_k}'>Country:</span>")
        self._load_default_image()

    # ── Image handling ────────────────────────────────────────────────────

    def _load_default_image(self) -> None:
        px = QPixmap("00-qrz-default.png")
        if not px.isNull():
            scaled = px.scaled(
                self._PHOTO_MAX_W, self._PHOTO_H,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            self.lbl_image.setPixmap(scaled)
        else:
            self.lbl_image.clear()

    def _on_image_loaded(self, px: QPixmap) -> None:
        self.lbl_image.setPixmap(px)
        # Defer the dialog-grow check so the layout has resolved column widths.
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(0, lambda w=px.width(): self._adjust_for_image_width(w))

    def _on_gif_loaded(self, data: bytes) -> None:
        px_probe = QPixmap()
        px_probe.loadFromData(data)
        if not px_probe.isNull():
            scaled_size = px_probe.scaled(
                self._PHOTO_MAX_W, self._PHOTO_H,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            ).size()
        else:
            scaled_size = None
        buf = QBuffer()
        buf.setData(QByteArray(data))
        buf.open(QBuffer.ReadOnly)
        self._gif_movie = QMovie()
        self._gif_movie.setDevice(buf)
        self._gif_movie._buf = buf
        if scaled_size is not None:
            self._gif_movie.setScaledSize(scaled_size)
        self.lbl_image.setMovie(self._gif_movie)
        self._gif_movie.start()
        if scaled_size is not None:
            from PyQt5.QtCore import QTimer
            w = scaled_size.width()
            QTimer.singleShot(0, lambda: self._adjust_for_image_width(w))

    def _adjust_for_image_width(self, img_width: int) -> None:
        """Grow the dialog horizontally when the photo is wider than column 2.

        Column 2's actual usable width depends on the resolved width of the
        text grid in column 1; we measure it from the live layout instead of
        relying on a fixed budget.
        """
        avail = self.lbl_image.width()
        if avail > 0 and img_width > avail:
            deficit = img_width - avail
            self.resize(self.width() + deficit + 4, self.height())
