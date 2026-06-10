# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)
#
# In the beginning was the word.
# and the word was with God.
# and the word was God.
# And the Word was made flesh, and dwelt among us, and we beheld his glory.
#
# The truth is not hard to find,
# it's hard to embrace.


"""
A PyQt5 application for monitoring JS8Call communications,
displaying status reports, messages, and live data feeds.
"""

import sys
import os
import io
import re
import base64
import faulthandler
import socket
import sqlite3
import threading
import subprocess
import urllib.request
import ssl

# Print a C-level traceback on segfaults so silent Qt crashes (especially on
# Linux) leave something actionable in the terminal instead of vanishing.
faulthandler.enable()


def _enable_windows_vt_mode() -> None:
    # Classic Windows conhost ignores ANSI escapes by default, so ConsoleColors
    # leak as raw "←[92m...←[0m" sequences. Enabling VT processing on the
    # console stdout/stderr handles makes the escapes render as color.
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ENABLE_VT = 0x0004
        for std_handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_handle)
            if not handle or handle == ctypes.c_void_p(-1).value:
                continue
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT)
    except Exception:
        pass


_enable_windows_vt_mode()
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from configparser import ConfigParser
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Set

import folium
import maidenhead as mh

# Optional: PyEnchant for smart title case (acronym detection)
try:
    import enchant
    ENCHANT_AVAILABLE = True
except ImportError:
    ENCHANT_AVAILABLE = False

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QColor, QDesktopServices
from PyQt5.QtWidgets import QTableWidgetItem
from PyQt5.QtCore import QBuffer, QIODevice, QTimer, QDateTime, Qt, QUrl
from PyQt5.QtWidgets import qApp
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage, QWebEngineProfile
from PyQt5.QtWebEngineCore import QWebEngineUrlSchemeHandler, QWebEngineUrlScheme, QWebEngineUrlRequestJob
from PyQt5.QtMultimedia import QSoundEffect
from connector_manager import ConnectorManager
from js8_tcp_client import TCPConnectionPool
from id_utils import generate_time_based_id
from constants import *


# =============================================================================
# Constants
# =============================================================================

# Commsrvr server for remote announcements and slideshow images
# This allows the developer to push messages/images to all CommStat users
_COMMSRVR = base64.b64decode("aHR0cHM6Ly9jb21tc3RhdC5hcHA=").decode()
_PING = _COMMSRVR + "/heartbeat-808585.php"

# Pixels of mouse-wheel scroll required to advance one Leaflet zoom level
# on the bottom-left map. Leaflet default is 60; raised to dampen sensitivity.
# Paired with zoomSnap=0.25 in the folium.Map() call so fractional zoom is
# allowed — otherwise every wheel tick rounds up to a full zoom level.
MAP_WHEEL_PX_PER_ZOOM = 360

# Contacts capture (Direct Message Part 1):
#  - The sender of the RX.DIRECTED (from_call) is the RELAY — the station we
#    directly heard. The callsign parsed out of the body is the TARGET —
#    the station the relay reported hearing.
#  - _CONTACTS_PATTERN matches '<TARGET> <KEYWORD(S)> <+/-SNR>' at the start
#    of an RX.DIRECTED body. We keep only SNR-style replies (see allow-list)
#    so time-coordination chatter like 'YES +13 (NOW)' does not pollute the roster.
#  - _CONTACTS_BASE_CS_PATTERN validates a base callsign AFTER stripping any
#    '/' suffix (KO4BIA/P -> KO4BIA, K2DHS/10 -> K2DHS).
_CONTACTS_PATTERN = re.compile(
    r"^([A-Z0-9/]{3,12})\s+((?:[A-Z]+\s+){0,2}[A-Z]+)\s+([+\-]\d{1,3})\b",
    re.IGNORECASE,
)
_CONTACTS_ALLOWED_KEYWORDS = {"SNR", "HEARTBEAT SNR"}
_CONTACTS_BASE_CS_PATTERN = re.compile(r"^[A-Z0-9]{3,8}$")

# Hearing-report path (third contacts-capture path):
#  - body matches '<ADDRESSEE_CS> HEARING <CS1> <CS2> ...'
#  - The sender (from_call) is the relay; each listed callsign is a target
#    the relay reports hearing. The leading addressee is who the report was
#    directed at and is discarded. SNR is unknown for both ends of the link,
#    so a fixed placeholder is written into both SNR columns.
_CONTACTS_HEARING_PATTERN = re.compile(
    r"^[@A-Z0-9/]{3,12}\s+HEARING\s+(.+)$",
    re.IGNORECASE,
)
_CONTACTS_HEARING_DEFAULT_SNR = -99

# Solar/radio image dialogs: (menu_label, image_url, link_html, loading_text, error_prefix)
SOLAR_IMAGE_DIALOGS = [
    ("Band Conditions", "https://www.hamqsl.com/solar101pic.php",
     '<a href="https://www.hamqsl.com/solar.html">Solar-Terrestrial Data provided by N0NBH</a>',
     "Loading band conditions...", "Failed to load band conditions"),
    ("Solar Flux", "https://www.hamqsl.com/marston.php",
     '<a href="https://www.hamqsl.com/solar.html">Solar-Terrestrial Data provided by N0NBH</a>',
     "Loading solar flux data...", "Failed to load solar flux data"),
    ("World Map", "https://www.hamqsl.com/solarmuf.php",
     '<a href="https://www.hamqsl.com/solar.html">View more at hamqsl.com</a>',
     "Loading solar conditions...", "Failed to load solar data"),
]

# Live weather map links opened in the user's browser from the Tools menu.
WEATHER_MAP_LINKS = [
    ("Nat'l Weather Service", "https://www.weather.gov/"),
    ("Ventusky", "https://www.ventusky.com/"),
    ("Windy.com", "https://www.windy.com/"),
    ("Zoom Earth", "https://zoom.earth/"),
]


def hz_to_mhz(freq_hz: float, offset: float = 0) -> float:
    """Convert frequency in Hz to MHz, optionally subtracting an offset first."""
    return (freq_hz - offset) / 1000000 if freq_hz else 0.0


def check_internet() -> bool:
    """
    Check internet connectivity by attempting to connect to DNS servers.

    Returns:
        True if internet is available, False otherwise.
    """
    # Why: this runs synchronously from __init__ before the UI paints, so a
    # slow-DNS day blocked startup for up to 9s. 1s per probe keeps the
    # worst case at 3s while still tolerating typical network latency.
    for host, port in (("www.google.com", 80), ("www.cloudflare.com", 443), ("8.8.8.8", 443)):
        try:
            sock = socket.create_connection((host, port), timeout=1)
            sock.close()
            return True
        except (socket.timeout, socket.error):
            continue
    return False


def expand_abbreviations(text: str, abbreviations: Dict[str, str] = None) -> str:
    """Expand common JS8Call abbreviations to full words.

    Args:
        text: Input text with possible abbreviations.
        abbreviations: Dictionary mapping abbreviations to expansions.
                      If None, no expansion is performed.

    Returns:
        Text with abbreviations expanded.
    """
    if not text or not abbreviations:
        return text

    words = text.split()
    result = []

    for word in words:
        # Preserve punctuation
        prefix = ""
        suffix = ""
        clean_word = word

        # Extract leading punctuation
        while clean_word and not clean_word[0].isalnum():
            prefix += clean_word[0]
            clean_word = clean_word[1:]

        # Extract trailing punctuation
        while clean_word and not clean_word[-1].isalnum():
            suffix = clean_word[-1] + suffix
            clean_word = clean_word[:-1]

        # Check for abbreviation (case-insensitive)
        upper_word = clean_word.upper()
        if upper_word in abbreviations:
            expanded = abbreviations[upper_word]
            result.append(prefix + expanded + suffix)
        else:
            result.append(word)

    return ' '.join(result)


def smart_title_case(text: str, abbreviations: Dict[str, str] = None, apply_normalization: bool = True) -> str:
    """Convert text to smart title case with acronym detection.

    First expands JS8Call abbreviations, then applies title case rules:
    - Words 1-2 chars stay lowercase (e.g., "a", "of", "to"), EXCEPT at sentence start
    - Dictionary words get title case
    - Non-dictionary words become ALL CAPS (treated as acronyms)
    - First word of each sentence is always capitalized
    - All-caps words from abbreviation expansion are preserved (e.g., SC, NY, TX)

    Args:
        text: Input text to format.
        abbreviations: Dictionary mapping abbreviations to expansions.
        apply_normalization: If False, returns text unchanged. Defaults to True.

    Returns:
        Formatted text with abbreviations expanded and smart title case (if enabled).
    """
    # If normalization is disabled, return text as-is
    if not apply_normalization:
        return text

    # First expand abbreviations
    text = expand_abbreviations(text, abbreviations)
    if not text:
        return text

    # Identify words that should be preserved as all-caps
    # These are words that:
    # 1. Are in the abbreviations dictionary
    # 2. Expand to an all-caps form (e.g., state codes: TX, NY, SC)
    preserved_caps = set()
    if abbreviations:
        for word in text.split():
            clean = ''.join(c for c in word if c.isalnum())
            upper_clean = clean.upper()
            # Check if this word is in abbreviations and expands to all-caps
            if upper_clean in abbreviations:
                expansion = abbreviations[upper_clean]
                # If expansion is all uppercase, preserve it
                if expansion.isupper():
                    preserved_caps.add(expansion.upper())

    # Initialize dictionary if available
    dictionary = None
    if ENCHANT_AVAILABLE:
        try:
            dictionary = enchant.Dict("en_US")
        except Exception:
            pass

    words = text.lower().split()
    result = []

    for i, word in enumerate(words):
        # Strip punctuation for checking, preserve for output
        clean_word = ''.join(c for c in word if c.isalnum())

        # Skip empty words (punctuation only)
        if not clean_word:
            result.append(word)
            continue

        # Check if this word should be preserved as all-caps
        if clean_word.upper() in preserved_caps:
            # Reconstruct word with original punctuation but uppercase letters
            rebuilt = ""
            for c in word:
                rebuilt += c.upper() if c.isalnum() else c
            result.append(rebuilt)
            continue

        # Check if this is the start of a sentence
        is_sentence_start = (i == 0)  # First word
        if i > 0:
            # Check if previous word ends with sentence-ending punctuation
            prev_word = result[-1]
            if prev_word.rstrip().endswith(('.', '!', '?')):
                is_sentence_start = True

        if is_sentence_start:
            # Always capitalize first letter of sentence, even if 1-2 chars
            if dictionary and not dictionary.check(clean_word) and not dictionary.check(clean_word.capitalize()):
                # Not in dictionary - treat as acronym
                result.append(word.upper())
            else:
                # Regular word or short word at sentence start - capitalize
                result.append(word.capitalize())
        elif len(clean_word) <= 2:
            # Short words stay lowercase (mid-sentence)
            result.append(word)
        elif dictionary and not dictionary.check(clean_word) and not dictionary.check(clean_word.capitalize()):
            # Not in dictionary - treat as acronym
            result.append(word.upper())
        else:
            # Regular word - title case
            result.append(word.capitalize())

    return ' '.join(result)


# =============================================================================
# STATREP Parsing Helper Functions
# =============================================================================

def strip_duplicate_callsign(value: str, from_call: str) -> str:
    """
    Remove duplicate callsign from message value if present.

    JS8Call bug causes: "W8APP: W8APP: @GROUP ..." instead of "W8APP: @GROUP ..."
    Must handle both formats since most users still have the buggy version.

    Args:
        value: Message text from JS8Call TCP stream
        from_call: Sender callsign from JSON params (may include /P suffix)

    Returns:
        Cleaned message text with duplicate removed
    """
    # Extract base callsign (remove /P, /M suffixes)
    base_call = from_call.split("/")[0] if from_call else ""
    if not base_call:
        return value

    # Pattern: "CALLSIGN: CALLSIGN: remainder"
    # Use word boundary to avoid partial matches
    pattern = rf'\b{re.escape(base_call)}\s*:\s*{re.escape(base_call)}\s*:\s*'
    if re.match(pattern, value, re.IGNORECASE):
        # Remove first occurrence, keep second
        value = re.sub(pattern, f'{base_call}: ', value, count=1, flags=re.IGNORECASE)

    return value


def sanitize_ascii(text: str) -> str:
    """
    Remove non-ASCII characters (keep only printable ASCII 32-126).

    Args:
        text: Input text

    Returns:
        Sanitized text with only printable ASCII characters
    """
    return re.sub(r'[^ -~]+', '', text).strip()


def parse_message_datetime(utc: str) -> tuple:
    """
    Parse UTC timestamp and generate time-based ID.

    Args:
        utc: UTC timestamp string (format: "YYYY-MM-DD   HH:MM:SS" or "YYYY-MM-DD HH:MM:SS")

    Returns:
        (date_only_str, time_based_id)
    """
    dt_str = utc.replace("   ", " ").strip()
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    date_only = utc.split()[0] if utc else ""
    msg_id = generate_time_based_id(dt)

    return (date_only, msg_id)


def expand_plus_shorthand(srcode: str) -> str:
    """Expand '+' shorthand to '111111111111' (all green status)."""
    return "111111111111" if srcode == "+" else srcode


def calculate_f304_status(digits: str, grid_found: bool) -> str:
    """
    Calculate map status for F!304/F!301 based on digit score.

    Rules:
    - Count 4 as 1, all others as face value
    - Score > 12: Red (3)
    - Score > 10: Yellow (2)
    - Grid found: Green (1)
    - Else: Unknown (4)
    """
    digit_score = sum(1 if int(d) == 4 else int(d) for d in digits)

    if digit_score > 12:
        return "3"
    elif digit_score > 10:
        return "2"
    elif grid_found:
        return "1"
    else:
        return "4"


def map_f304_digits_to_fields(digits: str) -> dict:
    """
    Map 8-digit F!304 format to database fields.

    Digit positions:
    [0]=Landline, [1]=Telecom, [2]=AM/FM/TV, [3]=Internet,
    [4]=Water, [5]=Power, [6]=Nat Gas, [7]=NOAA

    Returns dict with: power, water, telecom, internet, and comment parts
    """
    # Direct mappings
    commpw = digits[5]  # Commercial power
    pubwtr = digits[4]  # Public water
    net = digits[3]     # Internet

    # Telecom mapping (position 1)
    ota_digit = int(digits[1])
    if ota_digit == 1:
        ota = "1"
    elif ota_digit in [2, 3]:
        ota = "2"
    elif ota_digit == 4:
        ota = "3"
    else:
        ota = "4"

    # Comment additions (fields not in 12-digit format)
    YESNO_MAP = {1: "Yes", 2: "Limited", 3: "No", 4: "Unknown"}
    landline = YESNO_MAP.get(int(digits[0]), "Unknown")
    amfmtv = YESNO_MAP.get(int(digits[2]), "Unknown")
    natgas = YESNO_MAP.get(int(digits[6]), "Unknown")
    noaa = YESNO_MAP.get(int(digits[7]), "Unknown")

    return {
        'power': commpw,
        'water': pubwtr,
        'telecom': ota,
        'internet': net,
        'comment_parts': [
            f"Landline = {landline}",
            f"AM/FM/TV = {amfmtv}",
            f"Nat Gas = {natgas}",
            f"NOAA = {noaa}"
        ]
    }


def map_f301_digits_to_fields(digits: str) -> dict:
    """
    Map 9-digit F!301 format to database fields.

    First digit = scope (1-5), remaining 8 follow F!304 rules.

    Returns dict with: scope, power, water, telecom, internet, and comment parts
    """
    # Scope mapping
    SCOPE_MAP = {
        "1": "My Location",
        "2": "My Community",
        "3": "My County",
        "4": "My Region",
        "5": "Other Location"
    }
    scope = SCOPE_MAP.get(digits[0], "Unknown")

    # Remaining 8 digits follow F!304 format
    f304_fields = map_f304_digits_to_fields(digits[1:])

    # F!301 doesn't include Landline in comments
    f304_fields['comment_parts'] = f304_fields['comment_parts'][1:]  # Skip Landline
    f304_fields['scope'] = scope

    return f304_fields


def _strip_cs_suffix(callsign: str) -> str:
    """Return the base callsign: everything before the first '/', uppercased."""
    if not callsign:
        return ""
    return callsign.split("/", 1)[0].upper()


def parse_contacts_observation(value: str) -> Optional[Tuple[str, int]]:
    """
    Parse an RX.DIRECTED body for a target-SNR observation reported by the relay.

    Matches '<TARGET_CS> SNR|HEARTBEAT SNR <SIGNED_NUMBER>' at the start of the
    body. The target callsign is suffix-stripped and validated against
    _CONTACTS_BASE_CS_PATTERN; the keyword must be in _CONTACTS_ALLOWED_KEYWORDS.

    Args:
        value: Raw message body (params['value']).

    Returns:
        (target_base_cs, target_snr_int) if the body is a valid SNR observation,
        otherwise None.
    """
    if not value:
        return None
    match = _CONTACTS_PATTERN.match(value.strip())
    if not match:
        return None
    raw_cs, raw_kw, raw_snr = match.group(1), match.group(2), match.group(3)
    keyword = " ".join(raw_kw.upper().split())
    if keyword not in _CONTACTS_ALLOWED_KEYWORDS:
        return None
    base_cs = _strip_cs_suffix(raw_cs)
    if not _CONTACTS_BASE_CS_PATTERN.match(base_cs):
        return None
    try:
        return base_cs, int(raw_snr)
    except (TypeError, ValueError):
        return None


def parse_hearing_observation(value: str) -> Optional[List[str]]:
    """
    Parse a directed-message body for a HEARING report.

    Matches '<ADDRESSEE_CS> HEARING <CS1> <CS2> ...' at the start of the body.
    The addressee is discarded — it's just who the report was directed at.
    Each listed callsign is suffix-stripped, validated against
    _CONTACTS_BASE_CS_PATTERN, and deduplicated in order so JS8 decode garbage
    (e.g., '……M4TWA') is dropped silently.

    Returns the ordered list of validated heard callsigns, or None if the
    body does not match the pattern or yields no valid callsigns.
    """
    if not value:
        return None
    match = _CONTACTS_HEARING_PATTERN.match(value.strip())
    if not match:
        return None
    tail = match.group(1).rstrip(" \t♢").strip()
    if not tail:
        return None
    seen: Set[str] = set()
    heard: List[str] = []
    for token in tail.split():
        base = _strip_cs_suffix(token)
        if not _CONTACTS_BASE_CS_PATTERN.match(base):
            continue
        if base in seen:
            continue
        seen.add(base)
        heard.append(base)
    return heard if heard else None


# =============================================================================
# Helper Functionsn
# =============================================================================

def create_insecure_ssl_context():
    """Create SSL context that bypasses certificate verification.

    Some ham radio sites and RSS feeds have certificate issues,
    so we need to disable verification for those requests.
    """
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


# =============================================================================
# Tile Scheme Handler for Map
# =============================================================================

class TileSchemeHandler(QWebEngineUrlSchemeHandler):
    """Serves map tiles from tilesPNG2 via the tiles:// custom URL scheme."""

    # Why: cap the in-flight buffer set so a missed `job.destroyed` signal
    # (e.g. when the renderer crashes mid-load) can't grow the set without
    # bound. 256 covers a full screen of tiles at our largest zoom with
    # headroom. Oldest entries are evicted FIFO.
    _MAX_LIVE_BUFS = 256

    def __init__(self, tile_dir: str, parent=None):
        super().__init__(parent)
        self._tile_dir = tile_dir
        # deque-as-FIFO so we can bound it; set lookups not needed.
        from collections import deque
        self._live_bufs: deque = deque(maxlen=self._MAX_LIVE_BUFS)

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        path = job.requestUrl().path().lstrip('/')
        tile_path = os.path.join(self._tile_dir, path)
        if os.path.exists(tile_path):
            try:
                with open(tile_path, 'rb') as f:
                    data = f.read()
                buf = QBuffer()
                buf.setData(data)
                buf.open(QIODevice.ReadOnly)
                # Hold a reference until the job is destroyed OR the buffer is
                # closed, whichever fires first. The deque's maxlen evicts the
                # oldest reference if neither signal arrives.
                self._live_bufs.append(buf)

                def _drop(*_):
                    try:
                        self._live_bufs.remove(buf)
                    except ValueError:
                        pass

                job.destroyed.connect(_drop)
                buf.aboutToClose.connect(_drop)
                job.reply(b'image/png', buf)
            except Exception:
                job.fail(QWebEngineUrlRequestJob.RequestFailed)
        else:
            job.fail(QWebEngineUrlRequestJob.UrlNotFound)


# =============================================================================
# Large Map Breakout Window
# =============================================================================

class LargeMapDialog(QtWidgets.QDialog):
    """Non-modal window showing a larger version of the main map."""

    _ASPECT_RATIO = 16 / 9  # 800 x 450

    def __init__(self, html: str, main_window, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CommStat — Map")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        # Why: without this, closing the dialog only hides it — the
        # QWebEngineView and its renderer subprocess stay alive holding shm fds.
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(800, 450)
        if os.path.exists("radiation-32.png"):
            self.setWindowIcon(QtGui.QIcon("radiation-32.png"))

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.map_view = QWebEngineView()
        self.map_view.setPage(CustomWebEnginePage(main_window))
        layout.addWidget(self.map_view)

        if html:
            self.map_view.setHtml(html, QUrl("http://localhost/"))

    def resizeEvent(self, event):
        new_w = event.size().width()
        new_h = event.size().height()
        if new_w / max(new_h, 1) != self._ASPECT_RATIO:
            # Anchor to whichever dimension changed more
            if abs(new_w - event.oldSize().width()) >= abs(new_h - event.oldSize().height()):
                self.resize(new_w, round(new_w / self._ASPECT_RATIO))
            else:
                self.resize(round(new_h * self._ASPECT_RATIO), new_h)
        super().resizeEvent(event)

    def update_map(self, html: str) -> None:
        self.map_view.setHtml(html, QUrl("http://localhost/"))

    def closeEvent(self, event):
        # Tear down the QWebEngineView so the Chromium renderer subprocess and
        # its shm fds are released. Without this, repeated open/close of the
        # large-map window leaks renderer resources until EMFILE.
        try:
            self.map_view.setPage(None)
            self.map_view.deleteLater()
        except Exception:
            pass
        super().closeEvent(event)


# =============================================================================
# Clickable Label for Slideshow
# =============================================================================

class ClickableLabel(QtWidgets.QLabel):
    """A QLabel that emits a clicked signal when clicked."""
    clicked = QtCore.pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# =============================================================================
# Custom Web Engine Page for Map Links
# =============================================================================

class CustomWebEnginePage(QWebEnginePage):
    """Handles navigation requests from the map and video player."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_widget = parent

    def javaScriptConsoleMessage(self, level, message, line, source):
        if 'webkitStorageInfo' in message:
            return
        super().javaScriptConsoleMessage(level, message, line, source)

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        """Intercept custom URL schemes for statrep links and video events."""
        url_str = url.toString()

        # Handle video-ended event
        if url_str == "commstat://video-ended":
            if hasattr(self.parent_widget, '_on_video_skip'):
                self.parent_widget._on_video_skip()
            return False  # Prevent navigation

        # Handle statrep links from map popups: /statrep/{id}/{callsign}
        if url.path().startswith("/statrep/"):
            parts = url.path().strip("/").split("/")
            if len(parts) >= 3:
                sr_id = parts[1]
                callsign = parts[2]
                mw = self.parent_widget
                if sr_id and callsign and mw:
                    def _open_dialog(sr_id=sr_id, callsign=callsign, mw=mw):
                        try:
                            from qrz_lookup import StatRepDetailDialog
                            _FC = 3
                            table = mw.statrep_table
                            def build_record_list():
                                items = []
                                for r in range(table.rowCount()):
                                    ci = table.item(r, _FC)
                                    if ci:
                                        rid = ci.data(QtCore.Qt.UserRole)
                                        if rid is not None:
                                            items.append((rid, ci.text().strip()))
                                return items
                            dlg = StatRepDetailDialog(
                                sr_id, callsign, mw._internet_available,
                                commsrvr_url=_COMMSRVR,
                                module_background=mw.config.get_color('module_background'),
                                module_foreground=mw.config.get_color('module_foreground'),
                                title_bar_background=mw.config.get_color('title_bar_background'),
                                title_bar_foreground=mw.config.get_color('title_bar_foreground'),
                                data_background=mw.config.get_color('data_background'),
                                program_background=mw.config.get_color('program_background'),
                                program_foreground=mw.config.get_color('program_foreground'),
                                condition_green=mw.config.get_color('condition_green'),
                                condition_yellow=mw.config.get_color('condition_yellow'),
                                condition_red=mw.config.get_color('condition_red'),
                                condition_gray=mw.config.get_color('condition_gray'),
                                tcp_pool=mw.tcp_pool,
                                connector_manager=mw.connector_manager,
                                record_list_provider=build_record_list,
                                parent=mw
                            )
                            dlg.pin_changed.connect(
                                lambda _: mw._save_map_position(callback=mw._load_map)
                            )
                            dlg.record_deleted.connect(mw._load_statrep_data)
                            dlg.record_deleted.connect(
                                lambda: mw._save_map_position(callback=mw._load_map)
                            )
                            dlg.exec_()
                        except Exception as e:
                            print(f"[Map popup] Failed to open StatRepDetailDialog: {e}")
                    QTimer.singleShot(0, _open_dialog)
            return False  # Prevent navigation
        return super().acceptNavigationRequest(url, navigation_type, is_main_frame)


# =============================================================================
# ConfigManager - Handles all configuration loading
# =============================================================================

class ConfigManager:
    """Manages application configuration from config.ini."""

    def __init__(self, config_path: str = CONFIG_FILE):
        """
        Initialize ConfigManager.

        Args:
            config_path: Path to the configuration file
        """
        self.config_path = Path(config_path)
        self.colors = DEFAULT_COLORS.copy()
        self.directed_config: Dict[str, str] = {}
        self.filter_settings: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """Load configuration from file."""
        # Initialize filter settings (always reset on startup)
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        self.filter_settings = {
            'start': today,
            'end': ''
        }

        # Load toggle settings from config if it exists
        default_feed = list(DEFAULT_RSS_FEEDS.keys())[0]
        sound_defaults = self._default_sound_files()

        def _legacy_default(legacy_val: Optional[bool]) -> bool:
            # If the old single-toggle `notification_sounds` key is still in the
            # config file, use its value to seed all three per-event enable flags
            # once. Otherwise default to True.
            return True if legacy_val is None else legacy_val

        if not self.config_path.exists():
            self.directed_config = {
                'hide_heartbeat': False, 'show_all_groups': True, 'show_every_group': True,
                'hide_map': False, 'show_alerts': False, 'show_contacts': False,
                'save_all_alerts': False, 'save_all_messages': False,
                'selected_rss_feed': default_feed, 'apply_text_normalization': False,
                'unchecked_groups': '',
                'sound_alert_enabled':   True, 'sound_alert_file':   sound_defaults['alert'],
                'sound_message_enabled': True, 'sound_message_file': sound_defaults['message'],
                'sound_statrep_enabled': True, 'sound_statrep_file': sound_defaults['statrep'],
            }
            return

        config = ConfigParser()
        config.read(self.config_path)

        legacy_master = None
        if config.has_section("DIRECTEDCONFIG") and config.has_option("DIRECTEDCONFIG", "notification_sounds"):
            legacy_master = config.getboolean("DIRECTEDCONFIG", "notification_sounds")
        seed = _legacy_default(legacy_master)

        if config.has_section("DIRECTEDCONFIG"):
            self.directed_config = {
                'hide_heartbeat': config.getboolean("DIRECTEDCONFIG", "hide_heartbeat", fallback=False),
                'show_all_groups': config.getboolean("DIRECTEDCONFIG", "show_all_groups", fallback=False),
                'show_every_group': config.getboolean("DIRECTEDCONFIG", "show_every_group", fallback=False),
                'hide_map': config.getboolean("DIRECTEDCONFIG", "hide_map", fallback=False),
                'show_alerts': config.getboolean("DIRECTEDCONFIG", "show_alerts", fallback=False),
                'show_contacts': config.getboolean("DIRECTEDCONFIG", "show_contacts", fallback=False),
                'save_all_alerts': config.getboolean("DIRECTEDCONFIG", "save_all_alerts", fallback=False),
                'save_all_messages': config.getboolean("DIRECTEDCONFIG", "save_all_messages", fallback=False),
                'selected_rss_feed': config.get("DIRECTEDCONFIG", "selected_rss_feed", fallback=default_feed),
                'apply_text_normalization': config.getboolean("DIRECTEDCONFIG", "apply_text_normalization", fallback=True),
                'unchecked_groups': config.get("DIRECTEDCONFIG", "unchecked_groups", fallback=""),
                'sound_alert_enabled':   config.getboolean("DIRECTEDCONFIG", "sound_alert_enabled",   fallback=seed),
                'sound_alert_file':      config.get("DIRECTEDCONFIG", "sound_alert_file",      fallback=sound_defaults['alert']),
                'sound_message_enabled': config.getboolean("DIRECTEDCONFIG", "sound_message_enabled", fallback=seed),
                'sound_message_file':    config.get("DIRECTEDCONFIG", "sound_message_file",    fallback=sound_defaults['message']),
                'sound_statrep_enabled': config.getboolean("DIRECTEDCONFIG", "sound_statrep_enabled", fallback=seed),
                'sound_statrep_file':    config.get("DIRECTEDCONFIG", "sound_statrep_file",    fallback=sound_defaults['statrep']),
            }
        else:
            self.directed_config = {
                'hide_heartbeat': False, 'show_all_groups': True, 'show_every_group': True,
                'hide_map': False, 'show_alerts': False, 'show_contacts': False,
                'save_all_alerts': False, 'save_all_messages': False,
                'selected_rss_feed': default_feed, 'apply_text_normalization': False,
                'unchecked_groups': '',
                'sound_alert_enabled':   True, 'sound_alert_file':   sound_defaults['alert'],
                'sound_message_enabled': True, 'sound_message_file': sound_defaults['message'],
                'sound_statrep_enabled': True, 'sound_statrep_file': sound_defaults['statrep'],
            }

    @staticmethod
    def _default_sound_files() -> Dict[str, str]:
        # First-run defaults: scan SOUNDS_DIR and pick a file per event by
        # keyword. Falls back to the first .wav alphabetically, or "" if none.
        try:
            files = sorted(f for f in os.listdir(SOUNDS_DIR) if f.lower().endswith('.wav'))
        except OSError:
            files = []
        first = files[0] if files else ""

        def pick(*keywords: str) -> str:
            for kw in keywords:
                for f in files:
                    if kw in f.lower():
                        return f
            return first

        return {
            'alert':   pick('alarm', 'alert'),
            'message': pick('bell', 'ding'),
            'statrep': pick('ping', 'triple', 'dit'),
        }

    def get_color(self, key: str) -> str:
        """Get a color value by key."""
        return self.colors.get(key, '#FFFFFF')

    def _save_setting(self, key: str, value) -> None:
        """Save a setting to both memory and config file."""
        self.directed_config[key] = value
        config = ConfigParser()
        config.read(self.config_path)
        if not config.has_section("DIRECTEDCONFIG"):
            config.add_section("DIRECTEDCONFIG")
        config.set("DIRECTEDCONFIG", key, str(value))
        with open(self.config_path, 'w') as f:
            config.write(f)

    def get_hide_heartbeat(self) -> bool:
        return self.directed_config.get('hide_heartbeat', False)

    def set_hide_heartbeat(self, value: bool) -> None:
        self._save_setting('hide_heartbeat', value)

    def get_hide_map(self) -> bool:
        return self.directed_config.get('hide_map', False)

    def set_hide_map(self, value: bool) -> None:
        self._save_setting('hide_map', value)

    def get_show_every_group(self) -> bool:
        return self.directed_config.get('show_every_group', False)

    def set_show_every_group(self, value: bool) -> None:
        self._save_setting('show_every_group', value)

    def get_unchecked_groups(self) -> List[str]:
        raw = self.directed_config.get('unchecked_groups', '')
        return [g.strip() for g in raw.split(',') if g.strip()]

    def set_unchecked_groups(self, groups: List[str]) -> None:
        self._save_setting('unchecked_groups', ','.join(groups))

    def get_show_alerts(self) -> bool:
        return self.directed_config.get('show_alerts', False)

    def set_show_alerts(self, value: bool) -> None:
        self._save_setting('show_alerts', value)

    def get_save_all_alerts(self) -> bool:
        return self.directed_config.get('save_all_alerts', False)

    def set_save_all_alerts(self, value: bool) -> None:
        self._save_setting('save_all_alerts', value)

    def get_save_all_messages(self) -> bool:
        return self.directed_config.get('save_all_messages', False)

    def set_save_all_messages(self, value: bool) -> None:
        self._save_setting('save_all_messages', value)

    def get_sound_enabled(self, event: str) -> bool:
        return self.directed_config.get(f'sound_{event}_enabled', True)

    def set_sound_enabled(self, event: str, value: bool) -> None:
        self._save_setting(f'sound_{event}_enabled', value)

    def get_sound_file(self, event: str) -> str:
        return self.directed_config.get(f'sound_{event}_file', '')

    def set_sound_file(self, event: str, filename: str) -> None:
        self._save_setting(f'sound_{event}_file', filename)

    def get_show_contacts(self) -> bool:
        return self.directed_config.get('show_contacts', False)

    def set_show_contacts(self, value: bool) -> None:
        self._save_setting('show_contacts', value)

    def get_apply_text_normalization(self) -> bool:
        return self.directed_config.get('apply_text_normalization', True)

    def set_apply_text_normalization(self, value: bool) -> None:
        self._save_setting('apply_text_normalization', value)

    def get_selected_rss_feed(self) -> str:
        return self.directed_config.get('selected_rss_feed', list(DEFAULT_RSS_FEEDS.keys())[0])

    def set_selected_rss_feed(self, feed_name: str) -> None:
        self._save_setting('selected_rss_feed', feed_name)


# =============================================================================
# RSSFetcher - Fetches and parses RSS news feeds
# =============================================================================

class RSSFetcher:
    """Fetches and caches RSS news headlines."""

    def __init__(self):
        """Initialize the RSS fetcher with empty cache."""
        self._headlines: List[str] = []
        self._cache_time: Optional[datetime] = None
        self._cache_duration = timedelta(minutes=5)
        self._current_url: str = ""
        self._fetching = False

    def get_headlines(self, feed_url: str, force_refresh: bool = False) -> List[str]:
        """
        Get headlines from the specified RSS feed.

        Args:
            feed_url: URL of the RSS feed
            force_refresh: If True, bypass cache and fetch fresh data

        Returns:
            List of headline strings
        """
        # Check if we need to refresh
        now = datetime.now()
        cache_valid = (
            self._cache_time is not None
            and self._current_url == feed_url
            and (now - self._cache_time) < self._cache_duration
            and not force_refresh
        )

        if cache_valid and self._headlines:
            return self._headlines

        # Return cached data if currently fetching
        if self._fetching:
            return self._headlines

        # Fetch new data
        self._current_url = feed_url
        self._fetch_feed(feed_url)
        return self._headlines

    def _fetch_feed(self, feed_url: str) -> None:
        """Fetch and parse the RSS feed."""
        self._fetching = True
        try:
            request = urllib.request.Request(
                feed_url,
                headers={'User-Agent': 'CommStat/2.5'}
            )

            with urllib.request.urlopen(request, timeout=10, context=create_insecure_ssl_context()) as response:
                content = response.read().decode('utf-8', errors='replace')

            # Parse RSS XML
            root = ET.fromstring(content)
            articles = []  # List of (title, pubdate_datetime) tuples
            now = datetime.now(timezone.utc)
            cutoff_time = now - timedelta(hours=6)

            # Try RSS 2.0 format first (most common)
            for item in root.findall('.//item'):
                title_elem = item.find('title')
                pubdate_elem = item.find('pubDate')

                if title_elem is not None and title_elem.text:
                    title = title_elem.text.strip()
                    pub_date = None

                    # Parse publication date
                    if pubdate_elem is not None and pubdate_elem.text:
                        try:
                            pub_date = parsedate_to_datetime(pubdate_elem.text)
                            # Filter out articles older than 6 hours
                            if pub_date < cutoff_time:
                                continue
                        except Exception:
                            # If date parsing fails, include the article anyway
                            pub_date = None

                    articles.append((title, pub_date))

            # Try Atom format if no RSS items found
            if not articles:
                # Atom uses namespace
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                for entry in root.findall('.//atom:entry', ns):
                    title_elem = entry.find('atom:title', ns)
                    published_elem = entry.find('atom:published', ns) or entry.find('atom:updated', ns)

                    if title_elem is not None and title_elem.text:
                        title = title_elem.text.strip()
                        pub_date = None

                        # Parse publication date
                        if published_elem is not None and published_elem.text:
                            try:
                                # Atom uses ISO 8601 format
                                pub_date = datetime.fromisoformat(published_elem.text.replace('Z', '+00:00'))
                                # Filter out articles older than 6 hours
                                if pub_date < cutoff_time:
                                    continue
                            except Exception:
                                pub_date = None

                        articles.append((title, pub_date))

                # Also try without namespace
                if not articles:
                    for entry in root.findall('.//entry'):
                        title_elem = entry.find('title')
                        published_elem = entry.find('published') or entry.find('updated')

                        if title_elem is not None and title_elem.text:
                            title = title_elem.text.strip()
                            pub_date = None

                            if published_elem is not None and published_elem.text:
                                try:
                                    pub_date = datetime.fromisoformat(published_elem.text.replace('Z', '+00:00'))
                                    if pub_date < cutoff_time:
                                        continue
                                except Exception:
                                    pub_date = None

                            articles.append((title, pub_date))

            # Sort by date (newest first), articles without dates go to end
            articles.sort(key=lambda x: x[1] if x[1] else datetime.min.replace(tzinfo=timezone.utc), reverse=True)

            # Format headlines with timestamps
            headlines = []
            for title, pub_date in articles[:20]:  # Limit to 20 headlines
                if pub_date:
                    # Convert to UTC for display
                    utc_time = pub_date.astimezone(timezone.utc)
                    time_str = utc_time.strftime('%H:%M UTC')
                    headlines.append(f"{title} - {time_str}")
                else:
                    headlines.append(title)

            self._headlines = headlines
            self._cache_time = datetime.now()

        except Exception as e:
            print(f"Error fetching RSS feed: {e}")
            # Keep old headlines if fetch fails
            if not self._headlines:
                self._headlines = ["Unable to fetch news - check internet connection"]

        finally:
            self._fetching = False

    def fetch_async(self, feed_url: str, callback=None) -> None:
        """
        Fetch RSS feed in background thread.

        Args:
            feed_url: URL of the RSS feed
            callback: Optional callback function to call when done
        """
        def fetch_thread():
            self._fetch_feed(feed_url)
            if callback:
                callback()

        thread = threading.Thread(target=fetch_thread, daemon=True)
        thread.start()

    def clear_cache(self) -> None:
        """Clear the cached headlines."""
        self._headlines = []
        self._cache_time = None
        self._current_url = ""


# =============================================================================
# DatabaseManager - Handles all database operations
# =============================================================================

class DatabaseManager:
    """Manages SQLite database operations."""

    def __init__(self, db_path: str = DATABASE_FILE):
        """
        Initialize DatabaseManager.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path

    def _execute(self, operation, default=None):
        """Execute a database operation with error handling.

        Args:
            operation: Callable that takes (cursor, connection) and returns result
            default: Value to return on error

        Returns:
            Result of operation, or default on error
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                return operation(cursor, connection)
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return default

    def get_statrep_data(
        self,
        groups: List[str],
        start: str,
        end: str = '',
        show_all: bool = False,
        exclude_groups: List[str] = None,
        user_callsign: str = ""
    ) -> List[Tuple]:
        """
        Fetch StatRep data from database.

        Args:
            groups: List of active group names (empty list returns no data unless show_all)
            start: Start date filter (required)
            end: End date filter (optional, empty string means no upper limit)
            show_all: If True, return all statreps (subject to exclude_groups)
            exclude_groups: When show_all=True, exclude records from these groups
            user_callsign: User's own callsign; also returns statreps addressed directly to it

        Returns:
            List of tuples containing StatRep records
        """
        # If no active groups and not showing all, return empty list
        if not groups and not show_all and not user_callsign:
            return []

        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()

                # Build date condition based on whether end date is provided
                if end:
                    date_condition = "datetime BETWEEN ? AND ?"
                    date_params = [start, end]
                else:
                    date_condition = "datetime >= ?"
                    date_params = [start]

                # Build query based on whether we're showing all or filtering by groups
                if show_all:
                    if exclude_groups:
                        excl_with_at = ["@" + g for g in exclude_groups]
                        placeholders = ",".join("?" * len(excl_with_at))
                        query = f"""
                            SELECT db, datetime, freq, from_callsign, target, sr_id, grid, scope, map,
                                   power, water, med, telecom, travel, internet,
                                   fuel, food, crime, civil, political, comments, source, id
                            FROM statrep
                            WHERE target NOT IN ({placeholders})
                              AND ({date_condition} OR pinned = 1)
                            ORDER BY datetime DESC
                        """
                        params = excl_with_at + date_params
                    else:
                        query = f"""
                            SELECT db, datetime, freq, from_callsign, target, sr_id, grid, scope, map,
                                   power, water, med, telecom, travel, internet,
                                   fuel, food, crime, civil, political, comments, source, id
                            FROM statrep
                            WHERE {date_condition} OR pinned = 1
                            ORDER BY datetime DESC
                        """
                        params = date_params
                else:
                    # Build group filter; also include statreps addressed to the user's callsign
                    groups_with_at = ["@" + g for g in groups]
                    target_list = groups_with_at[:]
                    if user_callsign:
                        target_list.append(user_callsign.upper())
                    if not target_list:
                        return []
                    placeholders = ",".join("?" * len(target_list))
                    query = f"""
                        SELECT db, datetime, freq, from_callsign, target, sr_id, grid, scope, map,
                               power, water, med, telecom, travel, internet,
                               fuel, food, crime, civil, political, comments, source, id
                        FROM statrep
                        WHERE target IN ({placeholders}) AND ({date_condition} OR pinned = 1)
                        ORDER BY datetime DESC
                    """
                    params = target_list + date_params

                cursor.execute(query, params)
                return cursor.fetchall()
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return []

    def get_message_data(
        self,
        groups: List[str],
        start: str,
        end: str = '',
        show_all: bool = False
    ) -> List[Tuple]:
        """
        Fetch message data from database.

        Args:
            groups: List of active group names
            start: Start date filter (required)
            end: End date filter (optional, empty string means no upper limit)
            show_all: If True, return all messages regardless of group

        Returns:
            List of tuples containing message records
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()

                # Build date condition based on whether end date is provided
                if end:
                    date_condition = "datetime BETWEEN ? AND ?"
                    date_params = [start, end]
                else:
                    date_condition = "datetime >= ?"
                    date_params = [start]

                if show_all:
                    # Show all messages regardless of group
                    query = f"""SELECT db, datetime, freq, from_callsign, target, msg_id, message, source, delivered
                               FROM messages
                               WHERE {date_condition}
                               ORDER BY datetime DESC"""
                    params = date_params
                elif groups:
                    # Filter by active groups (add @ prefix for matching)
                    groups_with_at = ["@" + g for g in groups]
                    placeholders = ",".join("?" * len(groups_with_at))
                    query = f"""SELECT db, datetime, freq, from_callsign, target, msg_id, message, source, delivered
                               FROM messages
                               WHERE target IN ({placeholders}) AND {date_condition}
                               ORDER BY datetime DESC"""
                    params = groups_with_at + date_params
                else:
                    # No groups and not show_all - return empty
                    return []

                cursor.execute(query, params)
                return cursor.fetchall()
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return []

    def get_all_groups(self) -> List[str]:
        """Get all group names."""
        def op(cursor, conn):
            cursor.execute("SELECT name FROM groups ORDER BY name")
            return [row[0] for row in cursor.fetchall()]
        return self._execute(op, [])

    def add_group(self, group_name: str, comment: str = "", url1: str = "", url2: str = "") -> bool:
        """Add a new group with optional fields. Returns True if successful."""
        name = group_name.strip().upper()[:MAX_GROUP_NAME_LENGTH]
        if not name:
            return False
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                today = datetime.now().strftime("%Y-%m-%d")
                cursor.execute(
                    "INSERT INTO groups (name, comment, url1, url2, date_added) VALUES (?, ?, ?, ?, ?)",
                    (name, comment.strip(), url1.strip(), url2.strip(), today)
                )
                connection.commit()
                return True
        except sqlite3.IntegrityError:
            # Duplicate name
            return False
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return False

    def update_group(self, group_name: str, comment: str = "", url1: str = "", url2: str = "") -> bool:
        """Update an existing group's fields. Returns True if successful."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE groups SET comment = ?, url1 = ?, url2 = ? WHERE name = ?",
                (comment.strip(), url1.strip(), url2.strip(), group_name.upper())
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def update_group_full(self, old_name: str, new_name: str, comment: str = "") -> bool:
        """Rename a group and update its comment. Returns True if successful."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE groups SET name = ?, comment = ? WHERE name = ?",
                (new_name.strip().upper(), comment.strip(), old_name.strip().upper())
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def upsert_contacts_pair(
        self,
        relay_cs: str,
        relay_snr: int,
        target_cs: str,
        target_snr: int,
        freq_mhz: float,
    ) -> bool:
        """
        Upsert two contacts rows from a single RX.DIRECTED SNR observation.

        relay_cs  = the station we directly heard (sender of the message)
        relay_snr = our local SNR reading of that station
        target_cs = the station the relay reported hearing (body callsign)
        target_snr = the SNR the relay reported for that station

        Entry 1 (relay observation):  pair = (target_cs, relay_cs)
        Entry 2 (relay self-presence): pair = (relay_cs, relay_cs), snr = relay_snr

        Both rows use UNIQUE(target_cs, relay_cs) to refresh freq, SNR, and
        insert_date on every new observation instead of growing the table.
        """
        if not target_cs or not relay_cs:
            return False
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sql = (
            "INSERT INTO contacts "
            "(freq, relay_snr, relay_cs, target_cs, target_snr, insert_date) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_cs, relay_cs) DO UPDATE SET "
            "freq        = excluded.freq, "
            "relay_snr   = excluded.relay_snr, "
            "target_snr  = excluded.target_snr, "
            "insert_date = excluded.insert_date"
        )
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    sql,
                    (freq_mhz, relay_snr, relay_cs, target_cs, target_snr, now_utc),
                )
                cursor.execute(
                    sql,
                    (freq_mhz, relay_snr, relay_cs, relay_cs, relay_snr, now_utc),
                )
                connection.commit()
                return True
        except sqlite3.OperationalError as error:
            # Schema not yet applied — fail soft so the live feed keeps running.
            print(f"contacts upsert skipped (schema not ready?): {error}")
            return False
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return False

    def upsert_contacts_hearing(
        self,
        relay_cs: str,
        heard_list: List[str],
        freq_mhz: float,
        snr: int = _CONTACTS_HEARING_DEFAULT_SNR,
    ) -> bool:
        """
        Upsert contacts rows from a HEARING report.

        relay_cs   = the station that issued the HEARING report (from_call)
        heard_list = ordered base callsigns the relay reports hearing
        freq_mhz   = current dial frequency (MHz, 3-decimal)
        snr        = placeholder written to both SNR columns of every row
                     (defaults to _CONTACTS_HEARING_DEFAULT_SNR since the
                     report carries no SNR data for either end of the link)

        One row per heard callsign as (relay_cs, target_cs=heard) plus one
        self-presence row (relay_cs, target_cs=relay_cs). UNIQUE(target_cs,
        relay_cs) collapses repeat observations in place.
        """
        if not relay_cs or not heard_list:
            return False
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sql = (
            "INSERT INTO contacts "
            "(freq, relay_snr, relay_cs, target_cs, target_snr, insert_date) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_cs, relay_cs) DO UPDATE SET "
            "freq        = excluded.freq, "
            "relay_snr   = excluded.relay_snr, "
            "target_snr  = excluded.target_snr, "
            "insert_date = excluded.insert_date"
        )
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                for heard_cs in heard_list:
                    cursor.execute(sql, (freq_mhz, snr, relay_cs, heard_cs, snr, now_utc))
                cursor.execute(sql, (freq_mhz, snr, relay_cs, relay_cs, snr, now_utc))
                connection.commit()
                return True
        except sqlite3.OperationalError as error:
            print(f"contacts upsert skipped (schema not ready?): {error}")
            return False
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return False

    def purge_old_contacts(self, hours: int) -> int:
        """
        Delete contacts rows whose insert_date is older than `hours`.

        insert_date is stored as 'YYYY-MM-DD HH:MM:SS UTC'; lexicographic
        comparison against a same-format threshold sorts correctly so no
        SQL date parsing is needed.

        Returns the number of rows deleted. Returns 0 if hours <= 0 (which
        disables the purge — useful for letting an operator opt out via a
        constant of 0) or on DB error.
        """
        if hours <= 0:
            return 0
        threshold = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

        def op(cursor, conn):
            cursor.execute("DELETE FROM contacts WHERE insert_date < ?", (threshold,))
            conn.commit()
            return cursor.rowcount

        return self._execute(op, 0)

    def upsert_contact_self(
        self,
        cs: str,
        snr: int,
        freq_mhz: float,
    ) -> bool:
        """
        Upsert a single self-presence row for an RX.DIRECTED whose body did
        not parse as an SNR observation (e.g., a group post, a relayed
        message, free-form text). Records that `cs` was on the air at `snr`.

        Both relay_cs and target_cs are set to `cs`; both SNR columns are set
        to `snr`. UNIQUE(target_cs, relay_cs) keeps this collapsed to one
        roster row per callsign.
        """
        if not cs:
            return False
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sql = (
            "INSERT INTO contacts "
            "(freq, relay_snr, relay_cs, target_cs, target_snr, insert_date) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_cs, relay_cs) DO UPDATE SET "
            "freq        = excluded.freq, "
            "relay_snr   = excluded.relay_snr, "
            "target_snr  = excluded.target_snr, "
            "insert_date = excluded.insert_date"
        )
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                cursor.execute(sql, (freq_mhz, snr, cs, cs, snr, now_utc))
                connection.commit()
                return True
        except sqlite3.OperationalError as error:
            print(f"contacts upsert skipped (schema not ready?): {error}")
            return False
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return False

    def get_group_details(self, group_name: str) -> Optional[Dict]:
        """Get full details of a group."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT name, comment, url1, url2, date_added FROM groups WHERE name = ?",
                (group_name.upper(),)
            )
            row = cursor.fetchone()
            if row:
                return {
                    "name": row[0],
                    "comment": row[1] or "",
                    "url1": row[2] or "",
                    "url2": row[3] or "",
                    "date_added": row[4] or "",
                }
            return None
        return self._execute(op, None)

    def get_all_groups_details(self) -> List[Dict]:
        """Get full details of all groups, sorted by name."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT name, comment, url1, url2, date_added FROM groups ORDER BY name"
            )
            return [
                {
                    "name": row[0],
                    "comment": row[1] or "",
                    "url1": row[2] or "",
                    "url2": row[3] or "",
                    "date_added": row[4] or ""
                }
                for row in cursor.fetchall()
            ]
        return self._execute(op, [])

    def remove_group(self, group_name: str) -> bool:
        """Remove a group. Returns True if successful."""
        def op(cursor, conn):
            cursor.execute("DELETE FROM groups WHERE name = ?", (group_name.upper(),))
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_group_count(self) -> int:
        """Get the number of groups."""
        def op(cursor, conn):
            cursor.execute("SELECT COUNT(*) FROM groups")
            return cursor.fetchone()[0]
        return self._execute(op, 0)

    def get_abbreviations(self) -> Dict[str, str]:
        """Get all abbreviations from database as a dictionary."""
        def op(cursor, conn):
            cursor.execute("SELECT abbrev, expansion FROM abbreviations ORDER BY abbrev")
            return {row[0]: row[1] for row in cursor.fetchall()}
        return self._execute(op, {})

    def add_abbreviation(self, abbrev: str, expansion: str) -> bool:
        """Add or update an abbreviation. Returns True if successful."""
        abbrev = abbrev.strip().upper()
        expansion = expansion.strip()
        if not abbrev or not expansion:
            return False
        def op(cursor, conn):
            cursor.execute(
                "INSERT OR REPLACE INTO abbreviations (abbrev, expansion) VALUES (?, ?)",
                (abbrev, expansion)
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def remove_abbreviation(self, abbrev: str) -> bool:
        """Remove an abbreviation. Returns True if successful."""
        def op(cursor, conn):
            cursor.execute("DELETE FROM abbreviations WHERE abbrev = ?", (abbrev.upper(),))
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_qrz_settings(self) -> Tuple[str, str, bool]:
        """Get QRZ settings from database. Returns (username, password, is_active)."""
        def op(cursor, conn):
            cursor.execute("SELECT username, password, is_active FROM qrz_settings WHERE id = 1")
            result = cursor.fetchone()
            if result:
                return (result[0] or "", result[1] or "", bool(result[2]))
            return ("", "", False)
        return self._execute(op, ("", "", False))

    def set_qrz_settings(self, username: str, password: str, is_active: bool) -> bool:
        """Save QRZ settings to database."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE qrz_settings SET username = ?, password = ?, is_active = ? WHERE id = 1",
                (username, password, 1 if is_active else 0)
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "INSERT INTO qrz_settings (id, username, password, is_active) VALUES (1, ?, ?, ?)",
                    (username, password, 1 if is_active else 0)
                )
            conn.commit()
            return True
        return self._execute(op, False)

    def get_user_settings(self) -> Tuple[str, str, str]:
        """Get user callsign, grid square, and state from controls table."""
        def op(cursor, conn):
            cursor.execute("SELECT callsign, gridsquare, state FROM controls WHERE id = 1")
            row = cursor.fetchone()
            return (row[0] or "", row[1] or "", row[2] or "") if row else ("", "", "")
        return self._execute(op, ("", "", ""))

    def get_qrz_callsigns(self) -> set:
        """Return set of all callsigns cached in the qrz table."""
        def op(cursor, conn):
            cursor.execute("SELECT callsign FROM qrz")
            return {row[0].upper() for row in cursor.fetchall()}
        return self._execute(op, set())

    def get_qrz_contacts(self) -> list:
        """Return all QRZ cached contacts ordered by most recent first."""
        def op(cursor, conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT callsign, name, address, city, state, zip, country, grid, "
                "class, email, image, insert_date FROM qrz ORDER BY insert_date DESC"
            )
            return cursor.fetchall()
        return self._execute(op, [])

    def set_user_settings(self, callsign: str, grid: str, state: str) -> bool:
        """Save user callsign, grid square, and state to controls table."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE controls SET callsign = ?, gridsquare = ?, state = ? WHERE id = 1",
                (callsign, grid, state)
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_default_map(self) -> str:
        """Get user's preferred startup map region; falls back to 'us'."""
        def op(cursor, conn):
            try:
                cursor.execute("SELECT default_map FROM controls WHERE id = 1")
                row = cursor.fetchone()
                return (row[0] or "us") if row else "us"
            except sqlite3.OperationalError:
                return "us"
        return self._execute(op, "us")

    def set_default_map(self, region: str) -> bool:
        """Save user's preferred startup map region."""
        def op(cursor, conn):
            try:
                cursor.execute(
                    "UPDATE controls SET default_map = ? WHERE id = 1",
                    (region,)
                )
                conn.commit()
                return cursor.rowcount > 0
            except sqlite3.OperationalError:
                return False
        return self._execute(op, False)

    def set_qrz_active(self, is_active: bool) -> bool:
        """Toggle QRZ active status."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE qrz_settings SET is_active = ? WHERE id = 1",
                (1 if is_active else 0,)
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_alert_count(self) -> int:
        """Get the total number of alerts in the database."""
        def op(cursor, conn):
            cursor.execute("SELECT COUNT(*) FROM alerts")
            result = cursor.fetchone()
            return result[0] if result else 0
        return self._execute(op, 0)

    def delete_alert_at_offset(self, offset: int) -> bool:
        """Delete the alert at the specified offset from most recent."""
        def op(cursor, conn):
            cursor.execute(
                "DELETE FROM alerts WHERE id = ("
                "SELECT id FROM alerts ORDER BY datetime DESC LIMIT 1 OFFSET ?)",
                (offset,)
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_alert_at_offset(self, offset: int) -> Optional[Tuple[str, str, int, str, str, str]]:
        """Get an alert at the specified offset from most recent.

        Args:
            offset: 0 for most recent, 1 for second most recent, etc.

        Returns:
            Tuple of (title, message, color, datetime, from_callsign, group) or None.
        """
        def op(cursor, conn):
            cursor.execute(
                "SELECT title, message, color, datetime, from_callsign, target "
                "FROM alerts ORDER BY datetime DESC LIMIT 1 OFFSET ?",
                (offset,)
            )
            result = cursor.fetchone()
            if result:
                return (result[0], result[1], result[2], result[3], result[4] or "", result[5] or "")
            return None
        return self._execute(op, None)


# =============================================================================
# MainWindow - Main application window
class _MenuBarMenu(QtWidgets.QMenu):
    """QMenu anchored to the bottom edge of its parent QMenuBar.

    On macOS/Windows Qt already places the popup just below the menu bar,
    so this is a no-op. On some Linux setups Qt places the popup overlapping
    the menu bar — only then do we shift it down to the menu bar's bottom.
    """

    def showEvent(self, event):
        parent = self.parent()
        if isinstance(parent, QtWidgets.QMenuBar):
            target_y = parent.mapToGlobal(QtCore.QPoint(0, parent.height())).y()
            if self.y() < target_y:
                self.move(self.x(), target_y)
        super().showEvent(event)


# =============================================================================

class SoundPlayer:
    """
    Plays short notification sounds for inbound StatRep / Message / Alert events.

    Per-event filename and enable flag live in ConfigManager; the dialog can
    call reload(event) to swap files at runtime without restarting the app.
    QSoundEffect instances are held on the instance so Qt's async playback
    doesn't get GC'd mid-play. Missing files are tolerated silently.
    """

    EVENTS = ("statrep", "message", "alert")
    VOLUME = 0.8

    def __init__(self, config: "ConfigManager", sounds_dir: str = SOUNDS_DIR):
        self.config = config
        self.sounds_dir = Path(sounds_dir)
        self._effects: Dict[str, QSoundEffect] = {}
        self._preview: Optional[QSoundEffect] = None
        self._preview_proc: Optional[subprocess.Popen] = None
        self.reload_all()

    def reload(self, event: str) -> None:
        if not self.config.get_sound_enabled(event):
            self._effects.pop(event, None)
            return
        filename = self.config.get_sound_file(event)
        if not filename:
            self._effects.pop(event, None)
            return
        path = self.sounds_dir / filename
        if not path.exists():
            self._effects.pop(event, None)
            return
        effect = QSoundEffect()
        effect.setSource(QUrl.fromLocalFile(str(path.resolve())))
        effect.setVolume(self.VOLUME)
        self._effects[event] = effect

    def reload_all(self) -> None:
        for event in self.EVENTS:
            self.reload(event)

    def play(self, msg_type: str) -> None:
        if not self.config.get_sound_enabled(msg_type):
            return
        effect = self._effects.get(msg_type)
        if effect is not None:
            effect.play()

    def preview(self, filename: str) -> None:
        # Ignores the per-event enabled flag — used by the Sound Settings dialog
        # Play button so the user can audition a clip before turning it on.
        if not filename:
            return
        path = self.sounds_dir / filename
        if not path.exists():
            return
        abs_path = str(path.resolve())
        if sys.platform == "darwin":
            # QSoundEffect's CoreAudioOutput backend silently drops play()
            # calls made from a statusChanged handler (re-entrancy issue on
            # macOS). afplay is simpler and confirmed reliable.
            if self._preview_proc is not None:
                self._preview_proc.terminate()
                self._preview_proc = None
            self._preview_proc = subprocess.Popen(
                ["/usr/bin/afplay", abs_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            effect = QSoundEffect()
            effect.setVolume(self.VOLUME)
            self._preview = effect
            effect.statusChanged.connect(
                lambda: effect.play() if effect.status() == QSoundEffect.Ready else None
            )
            effect.setSource(QUrl.fromLocalFile(abs_path))


class MainWindow(QtWidgets.QMainWindow):
    """Main application window for CommStat."""

    # Collapse a burst of inbound events into a single notification sound.
    _SOUND_DEBOUNCE_MS = 300

    def __init__(self, config: ConfigManager, db: DatabaseManager):
        """
        Initialize the main window.

        Args:
            config: ConfigManager instance with loaded settings
            db: DatabaseManager instance for database operations
        """
        super().__init__()
        self.config = config
        self.db = db

        self._sound_player = SoundPlayer(config)
        self._pending_sound_types: set = set()
        self._sound_debounce_timer = QTimer(self)
        self._sound_debounce_timer.setSingleShot(True)
        self._sound_debounce_timer.timeout.connect(self._play_pending_sound)

        # Internet connectivity state
        self._internet_available = False
        self._check_internet_on_startup()

        # Initialize JS8Call connector manager and TCP connection pool
        self.connector_manager = ConnectorManager()
        self.connector_manager.init_connectors_table()
        self.tcp_pool = TCPConnectionPool(self.connector_manager, self)
        self.tcp_pool.any_message_received.connect(self._handle_tcp_message)
        self.tcp_pool.any_connection_changed.connect(self._handle_connection_changed)
        self.tcp_pool.any_status_message.connect(self._handle_status_message)
        self.tcp_pool.any_callsign_received.connect(self._handle_callsign_received)
        self.tcp_pool.any_grid_received.connect(self._handle_grid_received)

        # Live-bold StatRep/Message rows when a QRZ API lookup writes to the qrz table
        from qrz_lookup import qrz_cache_notifier
        qrz_cache_notifier.record_written.connect(self._on_qrz_record_written)

        # Store station info by rig name (persists even if connection is lost)
        self.rig_callsigns: Dict[str, str] = {}
        self.rig_grids: Dict[str, str] = {}
        self.rig_states: Dict[str, str] = {}
        self.rig_status_logged: Set[str] = set()  # Track which rigs have logged initial status

        # Live feed message buffer (stores messages from all TCP connections)
        self.feed_messages: List[str] = []
        self.max_feed_messages = 500  # Limit buffer size
        self._hide_live_feed: bool = False          # Session-only; resets on restart
        self._hide_internet_statrep: bool = False   # Session-only; resets on restart
        self._hide_green_pins: bool = False         # Session-only; resets on restart

        # Run startup status checks and initiate TCP connections.
        # Order: User Settings -> Groups -> JS8 Connectors -> QRZ Settings.
        self._log_startup_status()

        # Map state
        self.map_loaded = False
        self._last_map_region = self.db.get_default_map()
        self._current_view_mode: str = ""
        self._region_pin_counts: Dict[str, int] = {"us": 0, "eu": 0, "mideast": 0, "seasia": 0, "world": 0}

        self._setup_window()
        self._setup_ui()

    def _setup_window(self) -> None:
        """Configure window properties (size, title, icon)."""
        self.setObjectName("MainWindow")
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_SIZE)

        # Restore window geometry from config
        self._restore_window_position()

        # Cmd+Q on macOS may bypass closeEvent — save on aboutToQuit too.
        qApp.aboutToQuit.connect(self._save_window_position)

        # Set window icon
        icon_path = Path(ICON_FILE)
        if icon_path.exists():
            icon = QtGui.QIcon()
            icon.addPixmap(QtGui.QPixmap(str(icon_path)), QtGui.QIcon.Normal, QtGui.QIcon.Off)
            self.setWindowIcon(icon)

    def _restore_window_position(self) -> None:
        """Restore window geometry from config.ini."""
        config = ConfigParser()
        if not os.path.exists(CONFIG_FILE):
            return

        config.read(CONFIG_FILE)
        if not config.has_section("WINDOW"):
            return

        try:
            x = config.getint("WINDOW", "x", fallback=None)
            y = config.getint("WINDOW", "y", fallback=None)
            width = config.getint("WINDOW", "width", fallback=None)
            height = config.getint("WINDOW", "height", fallback=None)
        except (ValueError, TypeError):
            return

        if width is not None and height is not None:
            self.resize(width, height)
        if x is None or y is None:
            return

        # Pre-show move so the window paints roughly in the right place.
        self.move(x, y)

        # Qt's move(x, y) on Windows DWM leaves pos() reporting (x + margin_left,
        # y + margin_top) after WM_NCCALCSIZE fires — and the visible window has
        # actually drifted by that offset. Without compensation, every save→
        # restore cycle adds one frame margin and the window walks down/right
        # across launches. Once windowHandle().frameMargins() is populated,
        # re-move with the margins subtracted so the saved/restored position is
        # a stable fixed point.
        target_x, target_y = x, y
        state = {"attempts": 0, "applied": False}

        def _compensate() -> None:
            state["attempts"] += 1
            win = self.windowHandle()
            margins = win.frameMargins() if win is not None else None

            if margins is None or not (margins.left() or margins.top()):
                if state["attempts"] < 30:
                    QTimer.singleShot(100, _compensate)
                return

            if state["applied"]:
                return
            state["applied"] = True
            self.move(target_x - margins.left(), target_y - margins.top())

        QTimer.singleShot(150, _compensate)

    def closeEvent(self, event) -> None:
        """Clean up resources and save window position before closing."""
        # Stop all timers
        if hasattr(self, 'clock_timer'):
            self.clock_timer.stop()
        if hasattr(self, 'slideshow_timer'):
            self.slideshow_timer.stop()
        if hasattr(self, 'internet_timer'):
            self.internet_timer.stop()
        if hasattr(self, 'commsrvr_timer'):
            self.commsrvr_timer.stop()

        # Disconnect all TCP connections gracefully
        if hasattr(self, 'tcp_pool'):
            print("Closing TCP connections...")
            self.tcp_pool.disconnect_all()

        # Save window position
        self._save_window_position()
        event.accept()

    def _save_window_position(self) -> None:
        """Save window geometry to config.ini."""
        config = ConfigParser()
        if os.path.exists(CONFIG_FILE):
            config.read(CONFIG_FILE)

        if not config.has_section("WINDOW"):
            config.add_section("WINDOW")

        # Drop any stale geometry blob from older versions.
        config.remove_option("WINDOW", "geometry")

        # Save pos(). _restore_window_position re-moves the window with the
        # frame-margin offset subtracted, so this value is a stable fixed
        # point across restarts.
        pos = self.pos()
        size = self.size()
        config.set("WINDOW", "x", str(pos.x()))
        config.set("WINDOW", "y", str(pos.y()))
        config.set("WINDOW", "width", str(size.width()))
        config.set("WINDOW", "height", str(size.height()))

        try:
            with open(CONFIG_FILE, 'w') as f:
                config.write(f)
        except IOError as e:
            print(f"Warning: Could not save window position: {e}")

    def _setup_ui(self) -> None:
        """Build the user interface."""
        # Create central widget with background color
        self.central_widget = QtWidgets.QWidget(self)
        self.central_widget.setStyleSheet(
            f"background-color: {self.config.get_color('program_background')};"
        )
        self.setCentralWidget(self.central_widget)

        # Main layout
        self.main_layout = QtWidgets.QGridLayout(self.central_widget)
        self.main_layout.setObjectName("mainLayout")

        # Row stretches (menu bar handled by QMainWindow.setMenuBar)
        self.main_layout.setRowStretch(0, 0)  # Unused (was menu bar)
        self.main_layout.setRowStretch(1, 0)  # Header
        self.main_layout.setRowStretch(2, 1)  # StatRep table (50%)
        self.main_layout.setRowStretch(3, 1)  # Feed text (50%)
        self.main_layout.setRowStretch(4, 0)  # Map row 1 / Filter (fixed)
        self.main_layout.setRowStretch(5, 0)  # Map row 2 / Messages (fixed)

        # Setup components
        self._setup_menu()
        self._setup_header()
        self._setup_statrep_table()
        self._setup_map_widget()
        self._setup_live_feed()
        self._setup_message_table()
        self._setup_contacts_widget()
        self._setup_timers()

        # Single Ctrl+C shortcut for all tables — dispatches by focused viewport
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+C"), self).activated.connect(
            self._handle_copy_shortcut
        )

        # Populate the Groups menu and filter menu group checkboxes
        self._populate_groups_menu()
        self._populate_filter_groups_menu()

        # Load initial data
        self._load_statrep_data()
        self._load_map()
        self._load_live_feed()
        self._load_message_data()

        # Commsrvr check will start automatically after 30 seconds via timer

    def _check_internet_on_startup(self) -> None:
        """Check internet connectivity at startup."""
        self._internet_available = check_internet()
        if self._internet_available:
            print("Internet connectivity: Available")
        else:
            print("Internet connectivity: Not available (will retry in 30 minutes)")

    def _log_startup(self, line: str) -> None:
        """Log a startup status line to console (no timestamp) and live feed (timestamped)."""
        print(line)
        utc_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._add_to_feed(f"{utc_str}\t{line}", "")

    def _log_startup_status(self) -> None:
        """Emit startup status messages and initiate TCP connections.

        Order: User Settings, Groups, JS8 Connectors (and connection attempts),
        QRZ Settings.
        """
        # User settings (controls table)
        callsign, grid, state = self.db.get_user_settings()
        if callsign:
            self._log_startup(f"[User Settings Found] {callsign}, {grid}, {state}")
        else:
            self._log_startup("[User Settings NOT Found] Go to Menu -> Config -> User Settings")

        # Groups (stored with @ prefix in DB; displayed without)
        group_names = [g.lstrip('@') for g in self.db.get_all_groups()]
        if not group_names:
            self._log_startup("[Groups NOT Found] Go to Menu -> Config -> Manage Groups")
        elif len(group_names) == 1:
            self._log_startup(f"[Group Found] {group_names[0]}")
        else:
            self._log_startup(f"[Groups Found] {', '.join(group_names)}")

        # QRZ settings
        qrz_username, _, _ = self.db.get_qrz_settings()
        if qrz_username:
            self._log_startup("[QRZ Settings Found]")
        else:
            self._log_startup("[QRZ Settings NOT Found] Go to Menu -> Config -> QRZ Settings")

        # JS8 connectors — only log NOT Found if none configured.
        # Existing TCP "Attempting to connect" / "Connected" messages cover the rest.
        if not self.connector_manager.get_all_connectors(enabled_only=False):
            self._log_startup("[JS8 Connectors NOT Found] Go to Menu -> Config -> JS8 Connectors")
        # Initiate TCP connections (status messages emitted via status_message signal).
        # Each auto_connect=1 row is re-enabled and reconnected; auto_connect=0 rows
        # stay quiescent until the user clicks Reconnect.
        self.tcp_pool.connect_all()

    def _retry_internet_check(self) -> None:
        """Retry internet connectivity check (called by timer)."""
        was_available = self._internet_available
        self._internet_available = check_internet()

        if self._internet_available and not was_available:
            # Internet just became available
            print("Internet connectivity: Now available")
            self.internet_timer.stop()
            # Send first heartbeat after the HEARTBEAT_DELAY_MS delay, then start timer
            def start_commsrvr_heartbeat():
                self._check_commsrvr()  # Send first heartbeat immediately
                self.commsrvr_timer.start(180000)  # Then start 3 minute interval timer
            QTimer.singleShot(HEARTBEAT_DELAY_MS, start_commsrvr_heartbeat)
        elif not self._internet_available:
            print("Internet connectivity: Still not available (will retry in 30 minutes)")

    def _setup_menu(self) -> None:
        """Create the menu bar with all actions."""
        self.menubar = QtWidgets.QMenuBar(self)
        self.menubar.setNativeMenuBar(False)  # Use Qt menu bar, not native (fixes Linux)
        self.setMenuBar(self.menubar)  # Explicitly set as main window's menu bar
        self.menubar.setVisible(True)
        self.menubar.setFixedHeight(30)
        # Clear corner widgets that may interfere with menu layout on Linux
        self.menubar.setCornerWidget(None, Qt.TopLeftCorner)
        self.menubar.setCornerWidget(None, Qt.TopRightCorner)
        menu_bg = self.config.get_color('menu_background')
        menu_fg = self.config.get_color('menu_foreground')
        panel_bg = self.config.get_color('module_background')
        panel_fg = self.config.get_color('module_foreground')
        self.menubar.setStyleSheet(f"""
            QMenuBar {{
                background-color: {menu_bg};
                color: {menu_fg};
                font-family: Roboto;
                font-size: 13px;
                font-weight: bold;
            }}
            QMenuBar::item {{
                padding: 6px 8px;
            }}
            QMenuBar::item:selected {{
                background-color: {menu_bg};
            }}
            QMenu {{
                background-color: {panel_bg};
                color: {panel_fg};
                font-family: Roboto;
                font-size: 13px;
            }}
            QMenu::item {{
                font-family: Roboto;
                font-size: 13px;
                padding: 3px 12px;
            }}
            QMenu::item:selected {{
                background-color: {menu_bg};
                color: {menu_fg};
            }}
        """)

        # Create the main menu
        self.menu = _MenuBarMenu("Config", self.menubar)
        self.menubar.addMenu(self.menu)

        # Define menu actions: (name, text, handler)
        menu_items = [
            ("user_settings",  "User Settings",  self._on_user_settings),
            ("manage_groups",  "Manage Groups",   self._on_manage_groups),
            ("js8_connectors", "JS8 Connectors",  self._on_js8_connectors),
            ("qrz_enable",     "QRZ Settings",    self._on_qrz_enable),
            ("sound_settings", "Sound Settings",  self._on_sound_settings),
        ]

        # Create actions for dropdown menu
        self.actions: Dict[str, QtWidgets.QAction] = {}
        for item in menu_items:
            name, text, handler = item
            action = QtWidgets.QAction(text, self)
            action.triggered.connect(handler)
            self.menu.addAction(action)
            self.actions[name] = action

        self.groups_menu = self.menu
        self.groups_menu.addSeparator()

        # Populate group checkboxes (will be called after menu setup)
        # Deferred to after db initialization in __init__

        # Create the Transmit menu
        self.transmit_menu = _MenuBarMenu("Transmit", self.menubar)
        self.menubar.addMenu(self.transmit_menu)
        # Section headers throughout the menus are disabled QActions; style them
        # bold with the menu background/foreground colors. In each of these menus
        # the disabled items are exclusively section titles.
        section_header_qss = (
            f"QMenu::item:disabled {{ background-color: {menu_bg}; color: {menu_fg}; font-weight: bold; }}"
        )
        self.transmit_menu.setStyleSheet(section_header_qss)

        hybrid_lbl = QtWidgets.QAction("HYBRID TOOLS", self)
        hybrid_lbl.setEnabled(False)
        self.transmit_menu.addAction(hybrid_lbl)

        for name, text, handler in [
            ("statrep",      "Status Report",        self._on_statrep),
            ("send_message", "Group Message",         self._on_send_message),
            ("group_alert",  "Alert",                 self._on_group_alert),
        ]:
            action = QtWidgets.QAction(text, self)
            action.triggered.connect(handler)
            self.transmit_menu.addAction(action)
            self.actions[name] = action

        self.transmit_menu.addSeparator()
        internet_lbl = QtWidgets.QAction("INTERNET TOOLS", self)
        internet_lbl.setEnabled(False)
        self.transmit_menu.addAction(internet_lbl)

        inet_msg_action = QtWidgets.QAction("Direct Message", self)
        inet_msg_action.triggered.connect(self._on_qrz_lookup)
        self.transmit_menu.addAction(inet_msg_action)
        self.actions["internet_message"] = inet_msg_action

        self.transmit_menu.addSeparator()
        section_lbl = QtWidgets.QAction("GRID DOWN TOOLS", self)
        section_lbl.setEnabled(False)
        self.transmit_menu.addAction(section_lbl)

        for name, text, handler in [
            ("js8_direct_message", "JS8 Direct Message", self._on_js8_direct_message),
            ("js8email", "JS8 Email", self._on_js8email),
            ("js8sms",   "JS8 SMS",   self._on_js8sms),
        ]:
            action = QtWidgets.QAction(text, self)
            action.triggered.connect(handler)
            self.transmit_menu.addAction(action)
            self.actions[name] = action

        # Create the Filter menu
        self.filter_menu = _MenuBarMenu("Filter", self.menubar)
        self.menubar.addMenu(self.filter_menu)
        self.filter_menu.setStyleSheet(section_header_qss)

        # Helper to create styled menu checkboxes

        # DATE FILTERING section
        date_filter_label = QtWidgets.QAction("DATE FILTERING", self)
        date_filter_label.setEnabled(False)  # Disabled as a section title
        self.filter_menu.addAction(date_filter_label)

        for label, days in [
            ("Reset to Midnight", 0), ("Reset to 1 day ago", 1),
            ("Reset to 2 days ago", 2), ("Reset to 3 days ago", 3),
            ("Reset to 1 week ago", 7),
        ]:
            action = QtWidgets.QAction(label, self)
            action.triggered.connect(lambda checked, d=days: self._reset_filter_date(d))
            self.filter_menu.addAction(action)


        # LIVE FEED section
        self.filter_menu.addSeparator()
        live_feed_label = QtWidgets.QAction("LIVE FEED", self)
        live_feed_label.setEnabled(False)  # Disabled as a section title
        self.filter_menu.addAction(live_feed_label)

        self.hide_heartbeat_checkbox = self._create_menu_checkbox(
            self.filter_menu, "Hide CQ & Heartbeat",
            self.config.get_hide_heartbeat(), self._on_toggle_heartbeat)

        self.hide_live_feed_checkbox = self._create_menu_checkbox(
            self.filter_menu, "Hide Live Feed",
            False, self._on_toggle_hide_live_feed)

        # ALERTS & MESSAGES section
        self.filter_menu.addSeparator()
        alerts_messages_label = QtWidgets.QAction("ALERTS & MESSAGES", self)
        alerts_messages_label.setEnabled(False)  # Disabled as a section title
        self.filter_menu.addAction(alerts_messages_label)

        self.save_all_alerts_checkbox = self._create_menu_checkbox(
            self.filter_menu, "Save all Alerts",
            self.config.get_save_all_alerts(), self._on_toggle_save_all_alerts)

        self.save_all_messages_checkbox = self._create_menu_checkbox(
            self.filter_menu, "Save all Messages",
            self.config.get_save_all_messages(), self._on_toggle_save_all_messages)

        # STATREP & MESSAGES section
        self.filter_menu.addSeparator()
        statrep_messages_label = QtWidgets.QAction("STATUS REPORTS", self)
        statrep_messages_label.setEnabled(False)  # Disabled as a section title
        self.filter_menu.addAction(statrep_messages_label)

        self.hide_internet_statrep_checkbox = self._create_menu_checkbox(
            self.filter_menu, "Hide Internet Feed",
            False, self._on_toggle_hide_internet_statrep)

        self.hide_green_pins_checkbox = self._create_menu_checkbox(
            self.filter_menu, "Hide Green Pins",
            False, self._on_toggle_hide_green_pins)

        # Per-group checkboxes are inserted here dynamically after DB is ready
        self.filter_group_actions: Dict[str, QtWidgets.QAction] = {}

        self.show_every_group_checkbox = self._create_menu_checkbox(
            self.filter_menu, "Show Other Groups",
            self.config.get_show_every_group(), self._on_toggle_show_every_group)
        # Keep a reference to the QWidgetAction so insertAction() can use it as anchor
        self.show_every_group_action = self.filter_menu.actions()[-1]

        # Create Tools dropdown menu
        self.tools_menu = _MenuBarMenu("Tools", self.menubar)
        self.menubar.addMenu(self.tools_menu)
        self.tools_menu.setStyleSheet(section_header_qss)

        # Helper to create menu actions
        def create_action(menu, label, key, handler):
            action = QtWidgets.QAction(label, self)
            action.triggered.connect(handler)
            menu.addAction(action)
            self.actions[key] = action

        # Helper to create a non-clickable bold section header in a menu.
        def add_section_header(menu, text):
            header = QtWidgets.QAction(text, self)
            header.setEnabled(False)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            menu.addAction(header)

        # WEATHER MAPS section - browser links
        add_section_header(self.tools_menu, "WEATHER MAPS")
        for label, url in WEATHER_MAP_LINKS:
            create_action(
                self.tools_menu, label, "weather_" + label.lower().replace(" ", "_").replace(".", "_"),
                lambda checked=False, u=url: QDesktopServices.openUrl(QUrl(u))
            )

        self.tools_menu.addSeparator()
        create_action(self.tools_menu, "Live Radiation Map", "live_radiation_map", self._on_live_radiation_map)
        for label, key, url in [
            ("Real-Time Lightning",    "lightning_map",     "https://www.lightningmaps.org/"),
            ("US Power Outages",       "power_outages",     "https://poweroutage.us/"),
            ("USGS Earthquakes",       "usgs_earthquakes",  "https://earthquake.usgs.gov/earthquakes/map/"),
            ("Wildfire Map",           "wildfire_map",      "https://wildfiretrackers.com/"),
            ("World Internet Outages", "internet_outages",  "https://radar.cloudflare.com/outage-center"),
        ]:
            create_action(
                self.tools_menu, label, key,
                lambda checked=False, u=url: QDesktopServices.openUrl(QUrl(u))
            )

        # HAMSQL Tools section - solar/radio image dialogs
        add_section_header(self.tools_menu, "HAMSQL Tools")
        for menu_label, url, link, load_text, err_prefix in SOLAR_IMAGE_DIALOGS:
            create_action(
                self.tools_menu, menu_label, menu_label.lower().replace(" ", "_"),
                lambda checked=False, t=menu_label, u=url, l=link, lt=load_text, ep=err_prefix:
                    self._show_image_dialog(title=t, image_url=u, link_html=l, loading_text=lt, error_prefix=ep)
            )

        self.tools_menu.addSeparator()
        create_action(self.tools_menu, "Brevity", "brevity", self._on_brevity_generator)
        create_action(self.tools_menu, "Grid Finder", "grid_finder", self._on_grid_finder)
        create_action(self.tools_menu, "Large Map...", "large_map", self._on_large_map)
        create_action(self.tools_menu, "QRZ Contacts", "qrz_contacts", self._on_qrz_contacts_menu)

        # Menubar items
        create_action(self.menubar, "QRZ", "qrz_lookup", self._on_qrz_lookup)
        create_action(self.menubar, "Help", "help", self._on_help)
        create_action(self.menubar, "Exit" + " " * 10, "exit", qApp.quit)
        create_action(self.menubar, "What's New" + " " * 10, "whats_new", self._on_whats_new)
        create_action(self.menubar, "Live Better", "live_better", self._on_live_better)

        # Add status bar
        self.statusbar = QtWidgets.QStatusBar(self)
        self.setStatusBar(self.statusbar)
        self.statusbar.setStyleSheet(
            "QStatusBar, QStatusBar QLabel, QStatusBar QPushButton {"
            " font-family: Roboto; font-size: 12px; font-weight: normal; }"
        )

        # Map view toggle buttons (left side of status bar)
        for label, mode in [
            ("US", "us"),
            ("EU", "eu"),
            ("Mid-East", "mideast"),
            ("SE Asia", "seasia"),
            ("World", "world"),
            ("Images", "images"),
            ("Alerts", "alerts"),
            ("Contacts", "contacts"),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setFixedHeight(18)
            btn.setFixedWidth(70)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked, m=mode: self._set_map_view_mode(m))
            self.statusbar.addWidget(btn)
            setattr(self, f"_btn_{mode}", btn)

        hint_label = QtWidgets.QLabel("  ")
        self.statusbar.addWidget(hint_label)

        # Quick-link buttons after the divider, styled with the menu colors.
        for label, url in [
            ("Weather", "https://www.ventusky.com/"),
            ("Radiation", "https://gmcmap.com/"),
            ("Power", "https://poweroutage.us/"),
            ("Internet", "https://radar.cloudflare.com/outage-center"),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setFixedHeight(18)
            btn.setFixedWidth(70)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"background-color: {menu_bg}; color: {menu_fg}; border: none; border-radius: 4px; padding: 2px 10px;")
            btn.clicked.connect(lambda checked, u=url: QDesktopServices.openUrl(QUrl(u)))
            self.statusbar.addWidget(btn)

        # Add "Rig Status:" label (no sunken effect, permanent on right)
        rig_status_header = QtWidgets.QLabel(" Rig Status: ")
        self.statusbar.addPermanentWidget(rig_status_header)

        # Dictionary to hold status widgets for each rig
        self.rig_status_widgets: Dict[str, Tuple[QtWidgets.QLabel, QtWidgets.QLabel]] = {}

    def _setup_header(self) -> None:
        """Create the header row with News Feed and Time."""
        # Header container widget with horizontal layout
        self.header_widget = QtWidgets.QWidget(self.central_widget)
        self.header_widget.setFixedHeight(38)
        self.header_layout = QtWidgets.QHBoxLayout(self.header_widget)
        self.header_layout.setContentsMargins(0, 0, 0, 0)

        fg_color = self.config.get_color('program_foreground')
        menu_bg = self.config.get_color('menu_background')
        menu_fg = self.config.get_color('menu_foreground')
        font = QtGui.QFont("Roboto", -1, QtGui.QFont.Bold)
        font.setPixelSize(15)

        # News label
        self.label_newsfeed = QtWidgets.QLabel(self.header_widget)
        self.label_newsfeed.setStyleSheet(f"color: {fg_color};")
        self.label_newsfeed.setText("News:")
        self.label_newsfeed.setFont(font)
        self.header_layout.addWidget(self.label_newsfeed)

        # RSS Feed selector dropdown
        self.feed_combo = QtWidgets.QComboBox(self.header_widget)
        self.feed_combo.setFixedSize(180, 28)
        self.feed_combo.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        _combo_font = QtGui.QFont("Roboto", -1)
        _combo_font.setPixelSize(15)
        self.feed_combo.setFont(_combo_font)
        self.feed_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {menu_bg};
                color: {menu_fg};
                border: 1px solid {menu_fg};
                padding: 2px 5px;
            }}
            QComboBox::drop-down {{
                border: none;
            }}
        """)
        # Style the dropdown list view directly
        combo_view = QtWidgets.QListView()
        _combo_view_font = QtGui.QFont("Roboto", -1)
        _combo_view_font.setPixelSize(15)
        combo_view.setFont(_combo_view_font)
        combo_view.setStyleSheet(f"""
            QListView {{
                background-color: {menu_bg};
                color: {menu_fg};
                outline: none;
            }}
            QListView::item {{
                background-color: {menu_bg};
                color: {menu_fg};
                padding: 4px;
            }}
        """)
        self.feed_combo.setView(combo_view)
        # Populate with feed names
        for feed_name in DEFAULT_RSS_FEEDS.keys():
            self.feed_combo.addItem(feed_name)
        self.feed_combo.addItem("Disable")
        # Set to saved selection
        saved_feed = self.config.get_selected_rss_feed()
        index = self.feed_combo.findText(saved_feed)
        if index >= 0:
            self.feed_combo.setCurrentIndex(index)
        # Connect signal
        self.feed_combo.currentTextChanged.connect(self._on_feed_changed)
        self.header_layout.addWidget(self.feed_combo)

        # News ticker (scrolling text)
        self.newsfeed_label = QtWidgets.QLabel(self.header_widget)
        self.newsfeed_label.setFixedHeight(32)
        self.newsfeed_label.setMinimumWidth(0)
        self.newsfeed_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        _ticker_font = QtGui.QFont("Kode Mono", -1)
        _ticker_font.setPixelSize(15)
        self.newsfeed_label.setFont(_ticker_font)
        self.newsfeed_label.setStyleSheet(
            f"background-color: {self.config.get_color('newsfeed_background')};"
            f"color: {self.config.get_color('newsfeed_foreground')};"
        )
        self.header_layout.addWidget(self.newsfeed_label, 7)
        # Recompute ticker length when the label is resized so the scrolling
        # text fills the new width. Debounced via single-shot QTimer to avoid
        # restarting on every pixel during a window drag.
        self._newsfeed_resize_timer = QTimer(self)
        self._newsfeed_resize_timer.setSingleShot(True)
        self._newsfeed_resize_timer.timeout.connect(self._refresh_newsfeed_for_resize)
        self.newsfeed_label.installEventFilter(self)

        # Last 20 button - shows last 20 news headlines
        self.last20_button = QtWidgets.QPushButton("Last 20", self.header_widget)
        self.last20_button.setFixedSize(80, 28)
        _btn_font = QtGui.QFont("Roboto", -1)
        _btn_font.setPixelSize(15)
        self.last20_button.setFont(_btn_font)
        self.last20_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {menu_bg};
                color: {menu_fg};
                border: 1px solid {menu_fg};
                padding: 2px 5px;
            }}
            QPushButton:hover {{
                background-color: {menu_fg};
                color: {menu_bg};
            }}
        """)
        self.last20_button.clicked.connect(self._on_last20_clicked)
        self.header_layout.addWidget(self.last20_button)

        self.header_layout.addSpacing(22)

        # Time label
        self.label_time_prefix = QtWidgets.QLabel(self.header_widget)
        self.label_time_prefix.setStyleSheet(f"color: {fg_color};")
        self.label_time_prefix.setText("Time:")
        self.label_time_prefix.setFont(font)
        self.header_layout.addWidget(self.label_time_prefix)

        # Time display
        self.time_label = QtWidgets.QLabel(self.header_widget)
        self.time_label.setFixedSize(120, 32)
        _time_font = QtGui.QFont("Kode Mono", -1)
        _time_font.setPixelSize(16)
        self.time_label.setFont(_time_font)
        self.time_label.setStyleSheet(
            f"background-color: {self.config.get_color('time_background')};"
            f"color: {self.config.get_color('time_foreground')};"
        )
        self.time_label.setAlignment(QtCore.Qt.AlignCenter)
        self.header_layout.addWidget(self.time_label)

        # Add header to main layout (row 1, spans all columns)
        self.main_layout.addWidget(self.header_widget, 1, 0, 1, 2)

    def _setup_table_widget(self, table: QtWidgets.QTableWidget, headers: list) -> None:
        """Apply common styling and header configuration to a table widget."""
        title_bg = self.config.get_color('title_bar_background')
        title_fg = self.config.get_color('title_bar_foreground')
        data_bg = self.config.get_color('data_background')
        data_fg = self.config.get_color('data_foreground')

        table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {data_bg};
                color: {data_fg};
                font-family: Roboto;
                font-size: 13px;
                gridline-color: #D2D0CF;
                border: 1px solid #D2D0CF;
            }}
            QTableWidget QHeaderView::section {{
                background-color: {title_bg};
                color: {title_fg};
                font-family: Roboto;
                font-weight: bold;
                padding: 4px;
                border: 1px solid {title_bg};
            }}
            QTableWidget::item:selected {{
                background-color: #cce5ff;
                color: #000000;
            }}
            QToolTip {{
                background-color: #FFFFE1;
                color: black;
                border: 1px solid black;
            }}
        """)
        table.setShowGrid(True)

        table.horizontalHeader().setStyleSheet(f"""
            QHeaderView::section {{
                background-color: {title_bg};
                color: {title_fg};
                font-family: Roboto;
                font-weight: bold;
                font-size: 13px;
                padding: 4px;
            }}
        """)

        table.setHorizontalHeaderLabels(headers)

        header = table.horizontalHeader()
        header.setMinimumSectionSize(10)
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Fixed)
        header.resizeSection(0, 10)
        header.setStretchLastSection(True)
        table.verticalHeader().setVisible(False)

    def _setup_statrep_table(self) -> None:
        """Create the StatRep data table."""
        self.statrep_table = QtWidgets.QTableWidget(self.central_widget)
        self.statrep_table.setObjectName("statrepTable")
        self.statrep_table.setColumnCount(21)
        self.statrep_table.setRowCount(0)

        self._setup_table_widget(self.statrep_table, STATREP_HEADERS)

        self.statrep_table.itemClicked.connect(self._on_statrep_click)
        self.statrep_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.statrep_table.customContextMenuRequested.connect(
            lambda pos: self._show_table_copy_menu(self.statrep_table, pos)
        )

        self.main_layout.addWidget(self.statrep_table, 2, 0, 1, 2)

    def _setup_map_widget(self) -> None:
        """Create the map widget using QWebEngineView."""
        self.map_widget = QWebEngineView(self.central_widget)
        self.map_widget.setObjectName("mapWidget")
        self.map_widget.setFixedSize(MAP_WIDTH, MAP_HEIGHT)

        # Set custom page to handle statrep links
        custom_page = CustomWebEnginePage(self)
        self.map_widget.setPage(custom_page)

        # Add to layout (row 4, column 0)
        self.main_layout.addWidget(self.map_widget, 4, 0, 1, 1, Qt.AlignLeft | Qt.AlignTop)

        # Set column stretches: map column fixed, message column stretches
        self.main_layout.setColumnStretch(0, 0)  # Map (fixed)

        # Setup map disabled label (hidden by default)
        self._setup_map_disabled_label()

        # Setup view-toggle buttons below map
        self._setup_map_view_buttons()

    def _setup_map_disabled_label(self) -> None:
        """Create the label/image display shown when map is hidden."""
        self.map_disabled_label = ClickableLabel(self.central_widget)
        self.map_disabled_label.setFixedSize(MAP_WIDTH, MAP_HEIGHT)
        self.map_disabled_label.setAlignment(Qt.AlignCenter)
        self.map_disabled_label.setCursor(QtGui.QCursor(Qt.PointingHandCursor))

        # Use feed colors for background
        bg_color = self.config.get_color('feed_background')
        fg_color = self.config.get_color('feed_foreground')
        self.map_disabled_label.setStyleSheet(
            f"background-color: {bg_color}; color: {fg_color}; font-size: 18px; font-weight: bold;"
        )

        # Add to same layout position as map
        self.main_layout.addWidget(self.map_disabled_label, 4, 0, 2, 1, Qt.AlignLeft | Qt.AlignTop)

        # Image slideshow state
        self.slideshow_items: List[str] = []
        self.slideshow_index: int = 0

        # Timer for slideshow
        self.slideshow_timer = QtCore.QTimer(self)
        self.slideshow_timer.timeout.connect(self._show_next_image)
        self.slideshow_timer.setInterval(SLIDESHOW_INTERVAL * 60000)  # Convert minutes to ms

        # Setup alert display widget
        self._setup_alert_display()

    def _setup_alert_display(self) -> None:
        """Create the alert display widget shown when Show Alerts is enabled."""
        self.alert_display = QtWidgets.QWidget(self.central_widget)
        self.alert_display.setFixedSize(MAP_WIDTH, MAP_HEIGHT)

        # Track current alert index (0 = most recent)
        self.alert_index = 0

        # Use vertical layout
        alert_layout = QtWidgets.QVBoxLayout(self.alert_display)
        alert_layout.setAlignment(Qt.AlignTop)

        # Add small spacing at top
        alert_layout.addSpacing(20)

        # Title label (first line)
        self.alert_title_label = QtWidgets.QLabel()
        self.alert_title_label.setAlignment(Qt.AlignCenter)
        self.alert_title_label.setTextFormat(Qt.RichText)  # Enable HTML formatting
        # Title uses Roboto Slab with Black weight (heaviest/900)
        title_font = QtGui.QFont("Roboto Slab", 24, QtGui.QFont.Black)
        self.alert_title_label.setFont(title_font)
        alert_layout.addWidget(self.alert_title_label)

        # Message label (second line)
        self.alert_message_label = QtWidgets.QLabel()
        self.alert_message_label.setAlignment(Qt.AlignCenter)
        self.alert_message_label.setWordWrap(True)
        self.alert_message_label.setTextFormat(Qt.RichText)
        self.alert_message_label.setOpenExternalLinks(True)
        # Message uses Roboto (clean sans-serif for readability)
        message_font = QtGui.QFont("Roboto", 18)
        self.alert_message_label.setFont(message_font)
        alert_layout.addWidget(self.alert_message_label)

        # Spacer between message and date
        alert_layout.addStretch(1)

        # Date received label (at bottom)
        self.alert_date_label = QtWidgets.QLabel()
        self.alert_date_label.setAlignment(Qt.AlignCenter)
        self.alert_date_label.setTextFormat(Qt.RichText)  # Enable HTML formatting
        # Date uses Roboto (clean sans-serif for readability)
        date_font = QtGui.QFont("Roboto", -1)
        date_font.setPixelSize(19)
        self.alert_date_label.setFont(date_font)
        alert_layout.addWidget(self.alert_date_label)
        alert_layout.addSpacing(10)

        # Bottom row: nav buttons centered, delete button at right
        bottom_layout = QtWidgets.QHBoxLayout()

        self.alert_prev_btn = QtWidgets.QPushButton("<")
        self.alert_prev_btn.setFixedSize(40, 30)
        self.alert_prev_btn.setStyleSheet("QPushButton { font-size: 16px; font-weight: bold; background-color: white; color: #000000; }")
        self.alert_prev_btn.clicked.connect(lambda: self._alert_navigate(-1))

        self.alert_next_btn = QtWidgets.QPushButton(">")
        self.alert_next_btn.setFixedSize(40, 30)
        self.alert_next_btn.setStyleSheet("QPushButton { font-size: 16px; font-weight: bold; background-color: white; color: #000000; }")
        self.alert_next_btn.clicked.connect(lambda: self._alert_navigate(1))

        self.alert_delete_btn = QtWidgets.QPushButton("Delete")
        self.alert_delete_btn.setStyleSheet(
            "QPushButton { background-color: #dc3545; color: white; font-family: Roboto; font-size: 13px; font-weight: bold; border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background-color: #c82333; }"
            "QPushButton:pressed { background-color: #bd2130; }"
        )
        self.alert_delete_btn.clicked.connect(self._alert_delete)

        bottom_layout.addStretch(1)
        bottom_layout.addWidget(self.alert_prev_btn)
        bottom_layout.addSpacing(20)
        bottom_layout.addWidget(self.alert_next_btn)
        bottom_layout.addStretch(1)
        bottom_layout.addWidget(self.alert_delete_btn)

        alert_layout.addLayout(bottom_layout)

        # Default styling (will be updated when alert is displayed)
        self.alert_display.setStyleSheet("background-color: #333333;")
        self.alert_title_label.setStyleSheet("color: #ffffff;")
        self.alert_message_label.setStyleSheet("color: #ffffff; font-family: Roboto;")
        self.alert_date_label.setStyleSheet("color: #ffffff; font-family: Roboto;")

        # Add to same layout position as map
        self.main_layout.addWidget(self.alert_display, 4, 0, 2, 1, Qt.AlignLeft | Qt.AlignTop)

        # Hidden by default
        self.alert_display.hide()

    def _setup_map_view_buttons(self) -> None:
        """Apply initial map view state (buttons live in the status bar)."""
        if self.config.get_show_contacts():
            self._set_map_view_mode("contacts")
        elif self.config.get_show_alerts():
            self._set_map_view_mode("alerts")
        elif self.config.get_hide_map():
            self._set_map_view_mode("images")
        else:
            default = self.db.get_default_map()
            if default not in ("us", "eu", "mideast", "seasia", "world"):
                default = "us"
            self._set_map_view_mode(default)

    def _set_map_view_mode(self, mode: str) -> None:
        """Switch the map panel between region maps, Images, Alerts, and Contacts views."""
        INACTIVE = "background-color: #DDDDDD; color: #000000; border: none; border-radius: 4px; padding: 2px 10px;"
        ACTIVE   = "background-color: #28a745; color: white; border: none; border-radius: 4px; padding: 2px 10px;"

        # Region presets: (center_lat, center_lng), zoom
        REGION_VIEWS = {
            "us":      ((38.8199286, -96.7782551), 4),
            "eu":      ((45.8150, 15.9819),        4),
            "mideast": ((31.7683, 35.2137),        4),
            "seasia":  ((22.4778, 101.1718),       4),
            "world":   ((33.7948, -83.7132),       1),
        }

        self._current_view_mode = mode

        # Non-region buttons are styled directly; region buttons go through
        # _update_region_button_pin_indicators so they can show orange when
        # an inactive region has pins on it.
        for m in ("images", "alerts", "contacts"):
            btn = getattr(self, f"_btn_{m}", None)
            if btn:
                btn.setStyleSheet(ACTIVE if m == mode else INACTIVE)
        self._update_region_button_pin_indicators()

        if mode in REGION_VIEWS:
            center, zoom = REGION_VIEWS[mode]
            self.map_center = center
            self.map_zoom = zoom
            self._last_map_region = mode
            self._stop_slideshow()
            self.map_disabled_label.hide()
            self.alert_display.hide()
            if hasattr(self, 'contacts_widget'):
                self.contacts_widget.hide()
            self.map_widget.show()
            if hasattr(self, 'message_table'):
                self.message_table.show()
            self.config.set_hide_map(False)
            self.config.set_show_alerts(False)
            self.config.set_show_contacts(False)
            self._load_map()
        elif mode == "images":
            self.map_widget.hide()
            self.alert_display.hide()
            if hasattr(self, 'contacts_widget'):
                self.contacts_widget.hide()
            self.map_disabled_label.show()
            if hasattr(self, 'message_table'):
                self.message_table.show()
            self._start_slideshow()
            self.config.set_hide_map(True)
            self.config.set_show_alerts(False)
            self.config.set_show_contacts(False)
        elif mode == "alerts":
            self._stop_slideshow()
            self.map_widget.hide()
            self.map_disabled_label.hide()
            if hasattr(self, 'contacts_widget'):
                self.contacts_widget.hide()
            if hasattr(self, 'message_table'):
                self.message_table.show()
            self.config.set_hide_map(True)
            self.config.set_show_alerts(True)
            self.config.set_show_contacts(False)
            self.alert_index = 0
            self._show_alert_display()
        elif mode == "contacts":
            self._stop_slideshow()
            self.map_widget.hide()
            self.map_disabled_label.hide()
            self.alert_display.hide()
            self.config.set_hide_map(True)
            self.config.set_show_alerts(False)
            self.config.set_show_contacts(True)
            if hasattr(self, 'contacts_widget'):
                if hasattr(self, 'message_table'):
                    self.message_table.hide()
                self.contacts_widget.show()
                self._load_contacts_data()
                self.contacts_table.viewport().setFocus()
            else:
                QTimer.singleShot(0, lambda: self._set_map_view_mode("contacts"))

    def _update_region_button_pin_indicators(self) -> None:
        """
        Style the four region buttons from current state:
          - selected region  → green
          - has pins on it   → orange (#F07800)
          - otherwise        → gray
        Pin counts come from self._region_pin_counts (filled by _load_map);
        selected region comes from self._current_view_mode.
        """
        INACTIVE = "background-color: #DDDDDD; color: #000000; border: none; border-radius: 4px; padding: 2px 10px;"
        ACTIVE   = "background-color: #28a745; color: white;   border: none; border-radius: 4px; padding: 2px 10px;"
        HASPINS  = "background-color: #F07800; color: white;   border: none; border-radius: 4px; padding: 2px 10px;"

        counts  = getattr(self, "_region_pin_counts", {}) or {}
        current = getattr(self, "_current_view_mode", "")

        for region in ("us", "eu", "mideast", "seasia", "world"):
            btn = getattr(self, f"_btn_{region}", None)
            if not btn:
                continue
            if region == current:
                btn.setStyleSheet(ACTIVE)
            elif counts.get(region, 0) > 0:
                btn.setStyleSheet(HASPINS)
            else:
                btn.setStyleSheet(INACTIVE)

    def _show_alert_display(self) -> None:
        """Show the alert display with the current alert from database."""
        # Get total alert count and fetch alert at current index
        alert_count = self.db.get_alert_count()
        alert = self.db.get_alert_at_offset(self.alert_index)

        # Update navigation button states
        self.alert_prev_btn.setEnabled(self.alert_index > 0)
        self.alert_next_btn.setEnabled(self.alert_index < alert_count - 1)

        if alert:
            title, message, color, date_received, from_callsign, group = alert

            # Set colors based on alert color - all alerts use red
            color_map = {
                1: ("#333333", "#ffffff"),  # was #dc3545 (formerly Yellow)
                2: ("#333333", "#ffffff"),  # was #dc3545 (formerly Orange)
                3: ("#333333", "#ffffff"),  # was #dc3545
                4: ("#333333", "#ffffff"),  # was #dc3545 (formerly Black)
            }
            bg_color, text_color = color_map.get(color, ("#dc3545", "#ffffff"))

            # Format date to remove seconds (e.g., "2026-01-15 11:00:00" -> "2026-01-15 11:00")
            date_formatted = date_received[:16] if len(date_received) > 16 else date_received

            # Build date/callsign line with bold labels (use Roboto font)
            date_line = f'<span style="font-family: Roboto;"><b>Date Received:</b> {date_formatted}'
            if from_callsign:
                date_line += f"&nbsp;&nbsp;&nbsp;<b>Sent By:</b> {from_callsign}"
            date_line += "</span>"

            # Format alert display:
            # Top: group - ALERT (smaller font)
            # Middle: title (bold, bigger than message)
            # Bottom: message (normal)
            if group:
                # Show group + ALERT at top, then title in bold below (strip @ symbol)
                group_display = group.lstrip('@')
                formatted_title = f'<div style="font-family: \'Kode Mono\'; font-size: 22px; font-weight: bold; margin-top: -18px;">@{group_display} - ALERT</div>'
                if title:
                    formatted_title += f'<div style="font-family: \'Roboto Slab\'; font-size: 30px; font-weight: 900; margin-top: 24px;">{title}</div>'
            else:
                # No group, just show title in bold
                formatted_title = f'<div style="font-family: \'Roboto Slab\'; font-size: 26px; font-weight: 900;">{title if title else ""}</div>'

            self.alert_display.setStyleSheet(f"background-color: {bg_color};")
            self.alert_title_label.setStyleSheet(f"color: {text_color};")
            self.alert_message_label.setStyleSheet(f"color: {text_color}; font-family: Roboto;")
            self.alert_date_label.setStyleSheet(f"color: {text_color}; font-family: Roboto;")
            self.alert_title_label.setText(formatted_title)
            _parts = re.split(r'(https?://\S+)', message)
            _msg_html = "".join(
                f'<a href="{p.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")}"'
                f' style="color:#00FF00;">'
                f'{p.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")}</a>'
                if i % 2 else
                p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                for i, p in enumerate(_parts)
            )
            self.alert_message_label.setText(_msg_html)
            self.alert_date_label.setText(date_line)
        else:
            # No alerts - show placeholder
            self.alert_display.setStyleSheet("background-color: #333333;")
            self.alert_title_label.setStyleSheet("color: #ffffff;")
            self.alert_message_label.setStyleSheet("color: #ffffff; font-family: Roboto;")
            self.alert_date_label.setStyleSheet("color: #ffffff; font-family: Roboto;")
            self.alert_title_label.setText("No Alerts")
            self.alert_message_label.setText("")
            self.alert_date_label.setText("")

        self.alert_delete_btn.setVisible(alert is not None)
        self.alert_display.show()

    def _alert_navigate(self, direction: int) -> None:
        """Navigate alerts by direction (-1 for newer, +1 for older)."""
        new_index = self.alert_index + direction
        if direction < 0 and new_index >= 0:
            self.alert_index = new_index
            self._show_alert_display()
        elif direction > 0 and new_index < self.db.get_alert_count():
            self.alert_index = new_index
            self._show_alert_display()

    def _alert_delete(self) -> None:
        """Delete the currently displayed alert and refresh the view."""
        self.db.delete_alert_at_offset(self.alert_index)
        count = self.db.get_alert_count()
        if self.alert_index >= count:
            self.alert_index = max(0, count - 1)
        self._show_alert_display()

    def _fetch_commsrvr_content(self) -> Optional[str]:
        """Fetch and extract content from commsrvr server.

        Returns:
            Extracted content string, or None on error.
        """
        try:
            # Get callsign: prefer first active JS8 connector callsign, fall back to user settings
            callsign = next((cs for cs in self.rig_callsigns.values() if cs), None)
            if not callsign:
                callsign, _, __ = self.db.get_user_settings()
            if not callsign:
                callsign = "UNKNOWN"

            # Get db_version, build_number, data_id, and qrz_id from controls table
            db_version = 0
            build_number = 500  # Default fallback
            data_id = 0  # Default fallback
            qrz_id = 0  # Default fallback
            try:
                with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT db_version, build_number, data_id, qrz_id FROM controls WHERE id = 1")
                    result = cursor.fetchone()
                    if result:
                        db_version = result[0]
                        build_number = result[1] if len(result) > 1 else 500
                        data_id = result[2] if len(result) > 2 else 0
                        qrz_id = result[3] if len(result) > 3 and result[3] is not None else 0
            except sqlite3.Error:
                pass  # Use default values if query fails

            # Only report qrz_id when at least one JS8 connector is in "Connected" status
            qrz_value = qrz_id if self.tcp_pool.get_connected_rig_names() else 0

            # Build heartbeat URL with callsign, data_id, qrz_id, db_version, and build_number parameters
            heartbeat_url = f"{_PING}?cs={callsign}&id={data_id}&qrz={qrz_value}&db={db_version}&build={build_number}"

            with urllib.request.urlopen(heartbeat_url, timeout=10) as response:
                content = response.read().decode('utf-8')

            return content.strip() or None
        except Exception:
            return None

    def _handle_db_update(self, content: str) -> bool:
        """Handle database update from commsrvr server.

        Expected format:
        db_update
        db: 3
        sql:
        CREATE TABLE ... );
        INSERT INTO ... );

        Each SQL statement ends with };

        Args:
            content: The db_update response content

        Returns:
            True if update was successful, False otherwise
        """
        try:
            lines = content.split('\n')

            if not lines or lines[0].strip() != 'db_update':
                return False

            new_db_version = None
            sql_section = None

            # Find db version and sql section
            for i, line in enumerate(lines):
                if line.strip().startswith('db:'):
                    try:
                        new_db_version = int(line.split(':', 1)[1].strip())
                    except (ValueError, IndexError):
                        return False
                elif line.strip().startswith('sql:'):
                    # SQL may start on this line or the next
                    sql_start = line.split(':', 1)[1].strip()
                    if sql_start:
                        # SQL starts on same line
                        sql_section = sql_start + '\n' + '\n'.join(lines[i+1:])
                    else:
                        # SQL starts on next line
                        sql_section = '\n'.join(lines[i+1:])
                    break

            if new_db_version is None or sql_section is None:
                return False

            # Split SQL statements by semicolon
            sql_statements = []
            raw_statements = sql_section.split(';')

            for stmt in raw_statements:
                stmt = stmt.strip()
                if stmt:  # Skip empty statements
                    sql_statements.append(stmt)

            if not sql_statements:
                return False

            # Execute SQL statements
            try:
                with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                    cursor = conn.cursor()
                    for sql in sql_statements:
                        cursor.execute(sql)
                    cursor.execute("UPDATE controls SET db_version = ? WHERE id = 1", (new_db_version,))
                    conn.commit()
                print(f"Database updated successfully to version {new_db_version}")
                return True
            except sqlite3.Error as e:
                print(f"Database update failed: {e}")
                return False

        except Exception as e:
            print(f"Error handling db_update: {e}")
            return False

    def _handle_program_update(self, content: str) -> bool:
        """Handle program update from commsrvr server.

        Expected format:
        program_update
        build: 501
        url: https://commstat.com/downloads/update.zip

        Args:
            content: The program_update response content

        Returns:
            True if download was successful, False otherwise
        """
        try:
            lines = [line.strip() for line in content.split('\n') if line.strip()]
            if not lines or lines[0] != 'program_update':
                return False

            new_build = None
            download_url = None

            # Parse the update content
            for line in lines[1:]:
                if line.startswith('build:'):
                    try:
                        new_build = int(line.split(':', 1)[1].strip())
                    except (ValueError, IndexError):
                        print(f"Invalid build number format: {line}")
                        return False
                elif line.startswith('url:') or line.startswith('URL:'):
                    download_url = line.split(':', 1)[1].strip()
                    # Handle URLs that might have multiple colons (https://)
                    if '://' in line:
                        download_url = line.split(None, 1)[1].strip()

            if new_build is None or not download_url:
                print("Missing build number or URL in program_update")
                return False

            # Create updates directory if it doesn't exist
            import os
            updates_dir = os.path.join(os.path.dirname(__file__), 'updates')
            os.makedirs(updates_dir, exist_ok=True)

            update_file = os.path.join(updates_dir, 'update.zip')

            # Download the update
            print(f"Downloading update build {new_build} from {download_url}")

            try:
                with urllib.request.urlopen(download_url, timeout=30) as response:
                    with open(update_file, 'wb') as f:
                        f.write(response.read())

                print(f"Update downloaded successfully to {update_file}")

                # Update build_number in database
                try:
                    with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                        conn.execute("UPDATE controls SET build_number = ? WHERE id = 1", (new_build,))
                        conn.commit()
                    print(f"Build number updated to {new_build} in database")
                except sqlite3.Error as e:
                    print(f"Warning: Failed to update build number in database: {e}")
                    # Continue anyway - the update file is downloaded

                # Show restart prompt to user
                QtCore.QMetaObject.invokeMethod(
                    self, "_show_program_update_notification",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(int, new_build)
                )

                return True

            except Exception as e:
                print(f"Failed to download update: {e}")
                # Clean up partial download
                if os.path.exists(update_file):
                    os.remove(update_file)
                return False

        except Exception as e:
            print(f"Error handling program_update: {e}")
            return False

    def _is_valid_grid(self, grid: str) -> bool:
        """Check if a string looks like a valid Maidenhead grid square.

        Args:
            grid: String to validate

        Returns:
            True if it looks like a valid grid square (e.g., EM83CV, FN20, etc.)
        """
        if not grid:
            return False
        grid = grid.strip().upper()
        # Grid squares are 2 letters + 2 digits + optional 2 letters/digits
        # Examples: EM83, EM83CV, FN20XS
        if len(grid) < 4 or len(grid) > 8:
            return False
        # First 2 chars must be letters A-R
        if not (grid[0].isalpha() and grid[1].isalpha()):
            return False
        if not (grid[0] in 'ABCDEFGHIJKLMNOPQR' and grid[1] in 'ABCDEFGHIJKLMNOPQR'):
            return False
        # Next 2 must be digits
        if not (grid[2].isdigit() and grid[3].isdigit()):
            return False
        return True

    def _lookup_grid_for_callsign(self, callsign: str) -> Optional[str]:
        """Look up grid square for a callsign using QRZ cache/API.

        Args:
            callsign: Callsign to lookup

        Returns:
            Grid square or None if not found
        """
        try:
            from qrz_client import QRZClient, load_qrz_config

            # Check if QRZ is active
            active, username, password = load_qrz_config()
            if not active:
                return None

            # Create client and do lookup (uses cache first)
            client = QRZClient(username, password)
            result = client.lookup(callsign, use_cache=True)

            if result and result.get('grid'):
                grid = result['grid']
                print(f"[QRZ] Found grid {grid} for {callsign}")
                return grid

            return None
        except Exception as e:
            print(f"[QRZ] Error looking up {callsign}: {e}")
            return None

    def _resolve_grid(
        self,
        rig_name: str,
        grid: str,
        callsign: str,
        fallback_grid: str = "",
        msg_format: str = ""
    ) -> str:
        """
        Resolve grid square with QRZ fallback if needed.

        Args:
            rig_name: Rig identifier for logging
            grid: Primary grid square (may be empty/invalid)
            callsign: Callsign to lookup if grid is missing
            fallback_grid: Grid to use if QRZ lookup fails
            msg_format: Message format for logging (e.g., "STATREP", "F!304")

        Returns:
            Valid grid square or fallback
        """
        prefix = f"[{rig_name}] {msg_format}: " if msg_format else f"[{rig_name}] "

        # Case 1: Already have a precise grid (5+ chars) - use directly
        if grid and len(grid) > 4:
            return grid

        # Case 2: Have a 4-char grid - try to upgrade via QRZ
        if grid and len(grid) == 4:
            qrz_grid = self._lookup_grid_for_callsign(callsign)
            if qrz_grid and len(qrz_grid) > 4 and qrz_grid[:4].upper() == grid.upper():
                # Format as mixed case: first 4 upper + rest lower (e.g., EM83cv)
                # This makes QRZ-upgraded grids visually distinguishable
                qrz_grid = qrz_grid[:4].upper() + qrz_grid[4:].lower()
                print(f"{prefix}Upgraded grid {grid} -> {qrz_grid} via QRZ for {callsign}")
                return qrz_grid
            return grid

        # Try QRZ lookup for missing/invalid grid
        print(f"{prefix}Missing/invalid grid, attempting QRZ lookup for {callsign}")

        qrz_grid = self._lookup_grid_for_callsign(callsign)
        if qrz_grid:
            print(f"{prefix}Found grid {qrz_grid} via QRZ for {callsign}")
            return qrz_grid

        print(f"{prefix}QRZ lookup failed, using fallback grid")
        return fallback_grid if fallback_grid else ""

    def _insert_message_data(
        self,
        rig_name: str,
        table: str,
        data: dict,
        id_field: str,
        msg_type: str,
        from_callsign: str,
        extra_info: str = ""
    ) -> str:
        """
        Generic database insert with standardized error handling.

        Args:
            rig_name: Rig identifier for logging
            table: Database table name
            data: Dict of column_name: value pairs
            id_field: Name of the ID field for duplicate detection
            msg_type: Return value on success (e.g., "statrep", "message")
            from_callsign: Sender callsign for logging
            extra_info: Optional extra info for success message (e.g., " (FORWARDED)")

        Returns:
            msg_type on success, empty string on failure
        """
        columns = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        query = f"INSERT INTO {table} ({columns}) VALUES({placeholders})"
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                conn.execute(query, tuple(data.values()))
                conn.commit()
            print(f"{ConsoleColors.SUCCESS}[{rig_name}] Added {msg_type.upper()} {data.get(id_field, '')}{extra_info} from: {from_callsign}{ConsoleColors.RESET}")
            QtCore.QMetaObject.invokeMethod(
                self, "_queue_notification_sound",
                QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, msg_type),
            )
            return msg_type
        except sqlite3.IntegrityError as e:
            if id_field in str(e) or "UNIQUE" in str(e):
                id_val = data.get(id_field, "unknown")
                incoming_global_id = data.get('global_id', 0)
                print(f"[{rig_name}] Skipping duplicate {msg_type.upper()} {data.get(id_field, '')} from {from_callsign} — already received (Global ID: {incoming_global_id})")
                if incoming_global_id:
                    try:
                        with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                            conn.execute(
                                f"UPDATE {table} SET global_id = ? WHERE {id_field} = ? AND (global_id IS NULL OR global_id = 0)",
                                (incoming_global_id, id_val)
                            )
                            conn.commit()
                    except sqlite3.Error:
                        pass
            else:
                print(f"{ConsoleColors.WARNING}[{rig_name}] WARNING: Database constraint violation: {e}{ConsoleColors.RESET}")
        except sqlite3.Error as e:
            print(f"{ConsoleColors.ERROR}[{rig_name}] ERROR: {msg_type.capitalize()} database insert failed for {from_callsign}: {e}{ConsoleColors.RESET}")
        return ""

    def _process_fcode_statrep(
        self,
        rig_name: str,
        value: str,
        from_callsign: str,
        target: str,
        grid: str,
        freq: int,
        snr: int,
        utc: str,
        format_code: str,  # "F!304" or "F!301"
        source: int = 1,  # 1=Radio (TCP), 2=Internet (commsrvr)
        global_id: int = 0
    ) -> str:
        """
        Process F!304 or F!301 STATREP format messages.

        Args:
            format_code: "F!304" (8 digits) or "F!301" (9 digits)

        Returns:
            "statrep" on success, empty string on failure
        """
        # Determine pattern based on format
        digit_count = 8 if format_code == "F!304" else 9
        pattern = rf'{format_code}\s+(\d{{{digit_count}}})\s*(.*?)(?:>])?$'

        match = re.search(pattern, value, re.IGNORECASE)
        if not match:
            return ""

        digits = match.group(1)
        remainder = match.group(2)

        # Map digits to fields
        if format_code == "F!304":
            field_map = map_f304_digits_to_fields(digits)
            scope = "My Location"
            status_digits = digits
        else:  # F!301
            field_map = map_f301_digits_to_fields(digits)
            scope = field_map['scope']
            status_digits = digits[1:]  # Skip scope digit

        # F!304/F!301 messages don't contain grid data — resolve via callsign lookup
        # _lookup_grid_for_callsign checks qrz table first, then QRZ API (caches result)
        fcode_grid = self._lookup_grid_for_callsign(from_callsign) or ""
        grid_found = bool(fcode_grid and len(fcode_grid) >= 4)

        # Build comments
        comment_parts = [format_code] + field_map['comment_parts']
        comments = ", ".join(comment_parts)
        if remainder.strip():
            comments += f" - {remainder.strip()}"
        comments = sanitize_ascii(comments)

        # Calculate status
        fcode_status = calculate_f304_status(status_digits, grid_found)

        # Generate ID and extract date
        date_only, srid = parse_message_datetime(utc)

        # Default group
        fcode_group = target if target else "@ALL"

        # Build data dict for insertion
        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': source,
            'sr_id': srid,
            'from_callsign': from_callsign,
            'target': fcode_group,
            'grid': fcode_grid,
            'scope': scope,
            'map': fcode_status,
            'power': field_map['power'],
            'water': field_map['water'],
            'med': "4",
            'telecom': field_map['telecom'],
            'travel': "4",
            'internet': field_map['internet'],
            'fuel': "4",
            'food': "4",
            'crime': "4",
            'civil': "4",
            'political': "4",
            'comments': comments,
            'global_id': global_id
        }

        return self._insert_message_data(
            rig_name, "statrep", data, "sr_id", "statrep", from_callsign
        )

    def _handle_commsrvr_data_messages(self, content: str) -> bool:
        """Handle commsrvr server data messages with ID prefixes.

        Expected format (one or more lines):
        113:  2026-02-06 18:32:32    14118000    0    30    N0DDK: @MAGNET ,EM83CV,3,T31,321311111331,GA,{&%}
        114:  2026-02-06 18:35:10    14118000    0    30    W1ABC: @ALL LRT ,1,Test Alert,This is a test,{%%}

        Format per line:
        ID: date time freq_hz unused(0) snr callsign: message_data

        Args:
            content: The commsrvr response content with ID-prefixed messages

        Returns:
            True if at least one message was processed, False otherwise
        """
        import re
        from datetime import datetime, timezone
        from typing import Optional

        try:
            lines = content.split('\n')
            processed_count = 0
            last_data_id = 0
            data_types_processed = set()  # Track which data types were added

            # Process each line that starts with an ID
            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # Catch ::DELIVERED:: / ::EXPIRED:: lines regardless of whether they carry an ID prefix
                _directive_slot = None
                if "::DELIVERED::" in line:
                    _directive_slot = ("::DELIVERED::", "_show_delivered_popup", "Delivered")
                elif "::EXPIRED::" in line:
                    _directive_slot = ("::EXPIRED::", "_show_expired_popup", "Expired")
                if _directive_slot is not None:
                    _tag, _slot, _label = _directive_slot
                    print(f"[COMMSRVR] {_tag} raw line: {line!r}")
                    # Strip optional leading ID prefix before the directive
                    raw_payload = re.sub(r'^\d+:\s*', '', line)
                    raw_payload = raw_payload[len(_tag):]
                    if "," in raw_payload:
                        callsign, msg_text = raw_payload.split(",", 1)
                        callsign = callsign.strip()

                        # ::DELIVERED:: now arrives as CALLSIGN,DATE,MSG_ID,message
                        # (older format was just CALLSIGN,message). When the date
                        # and msg_id are present, flag the matching sent message as
                        # delivered and strip them from the popup text.
                        if _tag == "::DELIVERED::":
                            _deliv = re.match(
                                r'^(\d{4}-\d{2}-\d{2}),([^,]+),(.*)$', msg_text, re.DOTALL
                            )
                            if _deliv:
                                _date, _msgid, msg_text = (
                                    _deliv.group(1).strip(),
                                    _deliv.group(2).strip(),
                                    _deliv.group(3),
                                )
                                QtCore.QMetaObject.invokeMethod(
                                    self, "_mark_message_delivered",
                                    QtCore.Qt.QueuedConnection,
                                    QtCore.Q_ARG(str, callsign),
                                    QtCore.Q_ARG(str, _date),
                                    QtCore.Q_ARG(str, _msgid),
                                )

                        print(f"[COMMSRVR] {_label} — callsign={callsign!r}  msg={msg_text.strip()!r}")
                        QtCore.QMetaObject.invokeMethod(
                            self, _slot,
                            QtCore.Qt.QueuedConnection,
                            QtCore.Q_ARG(str, callsign),
                            QtCore.Q_ARG(str, msg_text.strip())
                        )
                    else:
                        print(f"[COMMSRVR] {_tag} payload missing comma, skipping: {raw_payload!r}")
                    continue

                # Check if line starts with a number followed by colon
                id_match = re.match(r'^(\d+):\s*(.+)$', line)
                if not id_match:
                    continue

                data_id = int(id_match.group(1))
                data = id_match.group(2).strip()

                # Track the highest ID we've seen
                if data_id > last_data_id:
                    last_data_id = data_id

                # Handle delete directive
                delete_match = re.match(r'^::STATREP-DELETE::(\d+)$', data)
                if delete_match:
                    gid = int(delete_match.group(1))
                    self.db._execute(
                        lambda cursor, conn, g=gid: (
                            cursor.execute("DELETE FROM statrep WHERE global_id = ?", (g,)),
                            conn.commit()
                        ),
                        None
                    )
                    try:
                        with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                            conn.execute("UPDATE controls SET data_id = ? WHERE id = 1", (data_id,))
                            conn.commit()
                        print(f"Updated data_id to {data_id} in controls table (delete)")
                    except sqlite3.Error as e:
                        print(f"Warning: Failed to update data_id in controls table (delete): {e}")
                    continue

                # Parse the data line: date time freq_hz unused snr callsign: message
                # Example: 2026-02-06 18:32:32    14118000    0    30    N0DDK: @MAGNET ,EM83CV,3,T31,321311111331,GA,{&%}
                # Fields: date(0) time(1) freq_hz(2) unused/0(3) snr/db(4) callsign:message(5)
                if data.startswith("DM:"):
                    data = data[3:]
                parts = data.split(None, 5)  # Split on whitespace, max 6 parts
                if len(parts) < 6:
                    print(f"Skipping malformed data line (ID {data_id}): insufficient fields")
                    continue

                try:
                    utc_date = parts[0]  # YYYY-MM-DD
                    utc_time = parts[1]  # HH:MM:SS
                    utc = f"{utc_date} {utc_time}"
                    freq = int(parts[2])  # Frequency in Hz
                    # parts[3] is unknown/unused (always 0)
                    db = int(parts[4])  # SNR in dB
                    message_part = parts[5]  # callsign: message_data

                    # Split callsign from message
                    if ':' not in message_part:
                        print(f"Skipping malformed message (ID {data_id}): no callsign separator")
                        continue

                    callsign_and_msg = message_part.split(':', 1)
                    from_callsign = callsign_and_msg[0].strip()
                    message_value = message_part  # Keep full message with sender prefix for consistent parsing

                    # Extract target group from message if present
                    target = ""
                    target_match = re.search(r'(@[A-Z0-9]+)', message_value, re.IGNORECASE)
                    if target_match:
                        target = target_match.group(1).upper()
                    else:
                        # Check if statrep is addressed directly to the user's callsign
                        _my_call = next((cs for cs in self.rig_callsigns.values() if cs), None)
                        if not _my_call:
                            _my_call, _, __ = self.db.get_user_settings()
                        if _my_call:
                            _direct = re.match(r'^\w+:\s+(\w+)\s+,', message_value, re.IGNORECASE)
                            if _direct and _direct.group(1).upper() == _my_call.upper():
                                target = _my_call.upper()

                    # Preprocess message value
                    message_value = self._preprocess_message_value(message_value, from_callsign)

                    # Parse using unified parser (source=2 for Internet)
                    msg_type, _ = self._parse_commstat_message(
                        "COMMSRVR", from_callsign, message_value, target, "", freq, db, utc, source=2, global_id=data_id
                    )

                    if msg_type:
                        processed_count += 1
                        data_types_processed.add(msg_type)

                except Exception as e:
                    print(f"Error parsing data line (ID {data_id}): {e}")
                    import traceback
                    traceback.print_exc()
                    continue

            # Update data_id in controls table if we processed any messages
            if last_data_id > 0:
                try:
                    with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                        conn.execute("UPDATE controls SET data_id = ? WHERE id = 1", (last_data_id,))
                        conn.commit()
                    print(f"Updated data_id to {last_data_id} in controls table")
                except sqlite3.Error as e:
                    print(f"Warning: Failed to update data_id in controls table: {e}")

            # Trigger UI refresh for processed data types (on main thread)
            if data_types_processed:
                QtCore.QMetaObject.invokeMethod(
                    self, "_refresh_commsrvr_data",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(set, data_types_processed)
                )

            return processed_count > 0

        except Exception as e:
            print(f"Error handling commsrvr data messages: {e}")
            import traceback
            traceback.print_exc()
            return False

    @QtCore.pyqtSlot(str, str, str)
    def _mark_message_delivered(self, callsign: str, date: str, msg_id: str) -> None:
        """Flag a sent message as delivered and refresh the message table.

        Fired when the commsrvr returns ::DELIVERED::CALLSIGN,DATE,MSG_ID,...
        The row we sent has the recipient in the target column, so match on
        target/date/msg_id. Runs on the GUI thread (queued) so the table
        refresh — which touches Qt widgets — is safe.
        """
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                conn.execute(
                    "UPDATE messages SET delivered = 1 "
                    "WHERE target = ? AND date = ? AND msg_id = ?",
                    (callsign, date, msg_id)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[DELIVERED] Failed to mark message delivered: {e}")

        self._load_message_data()

    @QtCore.pyqtSlot(str, str)
    def _show_delivered_popup(self, callsign: str, message: str) -> None:
        """Show a delivery confirmation popup when the commsrvr confirms a message was delivered."""
        print(f"[DELIVERED] Showing popup — callsign={callsign!r}  message={message!r}")
        from qrz_lookup import DeliveryConfirmationDialog
        dlg = DeliveryConfirmationDialog(
            callsign=callsign,
            message=message,
            module_background=self.config.get_color('module_background'),
            module_foreground=self.config.get_color('module_foreground'),
            program_background=self.config.get_color('program_background'),
            program_foreground=self.config.get_color('program_foreground'),
            parent=self,
        )
        dlg.exec_()

    @QtCore.pyqtSlot(str, str)
    def _show_expired_popup(self, callsign: str, message: str) -> None:
        """Show an expiry popup when the commsrvr reports a message expired before retrieval."""
        print(f"[EXPIRED] Showing popup — callsign={callsign!r}  message={message!r}")
        from qrz_lookup import MessageExpiredDialog
        dlg = MessageExpiredDialog(
            callsign=callsign,
            message=message,
            module_background=self.config.get_color('module_background'),
            module_foreground=self.config.get_color('module_foreground'),
            program_background=self.config.get_color('program_background'),
            program_foreground=self.config.get_color('program_foreground'),
            parent=self,
        )
        dlg.exec_()

    @QtCore.pyqtSlot(set)
    def _refresh_commsrvr_data(self, data_types: set) -> None:
        """Refresh UI for data received from commsrvr server (called from main thread).

        Args:
            data_types: Set of data types to refresh ('statrep', 'alert', 'message')
        """
        if 'statrep' in data_types:
            self._load_statrep_data()
            alert_after_map = self._trigger_show_alerts if 'alert' in data_types else None
            self._save_map_position(callback=lambda: self._load_map(callback=alert_after_map))

        if 'message' in data_types:
            self._load_message_data()

        if 'alert' in data_types:
            if 'statrep' not in data_types:
                self._trigger_show_alerts()
            self._load_live_feed()

    @QtCore.pyqtSlot(int)
    def _show_program_update_notification(self, new_build: int) -> None:
        """Show notification prompting user to restart (called from main thread)."""
        from ui_helpers import confirm
        if confirm(
            self,
            "Update Available",
            f"CommStat build {new_build} has been downloaded.\n\n"
            f"Please close the application to install the update.\n\n"
            f"Close CommStat now?",
            default_yes=True,
        ):
            # Close the application gracefully - this triggers closeEvent() which
            # disconnects TCP connections and saves state
            # commstat.py will apply the update on next launch
            self.close()

    def _load_slideshow_images(self) -> None:
        """Load images with priority: my_images > images > 00-default.png."""
        self.slideshow_items = []
        self.slideshow_index = 0
        valid_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.bmp')

        # Priority 1: Check my_images folder
        my_images_folder = os.path.join(os.getcwd(), "my_images")
        if os.path.isdir(my_images_folder):
            files = sorted(os.listdir(my_images_folder))
            for filename in files:
                if filename.lower().endswith(valid_extensions):
                    image_path = os.path.join(my_images_folder, filename)
                    self.slideshow_items.append(image_path)

        if self.slideshow_items:
            return

        # Priority 2: Check images folder
        images_folder = os.path.join(os.getcwd(), "images")
        if os.path.isdir(images_folder):
            files = sorted(os.listdir(images_folder))
            for filename in files:
                if filename.lower().endswith(valid_extensions):
                    image_path = os.path.join(images_folder, filename)
                    self.slideshow_items.append(image_path)

        if self.slideshow_items:
            return

        # Priority 3: Use default image
        default_image = os.path.join(os.getcwd(), "00-default.png")
        if os.path.isfile(default_image):
            self.slideshow_items.append(default_image)

    def _start_slideshow(self) -> None:
        """Start the image slideshow."""
        self._load_slideshow_images()
        if self.slideshow_items:
            self._show_current_image()
            self.slideshow_timer.start()
        else:
            self.map_disabled_label.setPixmap(QtGui.QPixmap())
            self.map_disabled_label.setText("Map Disabled")

    def _stop_slideshow(self) -> None:
        """Stop the image slideshow."""
        self.slideshow_timer.stop()

    def _show_current_image(self) -> None:
        """Display the current slideshow image."""
        if not self.slideshow_items:
            return

        image_path = self.slideshow_items[self.slideshow_index]
        pixmap = QtGui.QPixmap(image_path)

        # Scale to fit while maintaining aspect ratio
        scaled_pixmap = pixmap.scaled(
            MAP_WIDTH, MAP_HEIGHT,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )
        self.map_disabled_label.setPixmap(scaled_pixmap)
        self.map_disabled_label.setText("")

    def _show_next_image(self) -> None:
        """Advance to the next image in the slideshow."""
        if not self.slideshow_items:
            return

        self.slideshow_index = (self.slideshow_index + 1) % len(self.slideshow_items)
        self._show_current_image()

    def _check_commsrvr_content_async(self) -> None:
        """Background thread to check commsrvr for updates."""
        try:
            content = self._fetch_commsrvr_content()
            if not content:
                return

            self._commsrvr_fail_count = 0

            if content.strip() == '1':
                return

            # Check if server returns "0"
            if content.strip() == '0':
                print("Commsrvr server reply = 0")
                return
            content_stripped = content.strip()

            if content_stripped.startswith('db_update'):
                self._handle_db_update(content_stripped)
                return
            elif content_stripped.startswith('program_update'):
                self._handle_program_update(content_stripped)
                return

            if "::DELIVERED::" in content_stripped:
                print(f"[COMMSRVR] ::DELIVERED:: detected in content: {content_stripped!r}")
            if "::EXPIRED::" in content_stripped:
                print(f"[COMMSRVR] ::EXPIRED:: detected in content: {content_stripped!r}")

            if (re.search(r'^\d+:\s+\d{4}-\d{2}-\d{2}', content_stripped, re.MULTILINE) or
                    re.search(r'^\d+:\s+::STATREP-DELETE::', content_stripped, re.MULTILINE) or
                    re.search(r'::DELIVERED::', content_stripped) or
                    re.search(r'::EXPIRED::', content_stripped)):
                self._handle_commsrvr_data_messages(content_stripped)

        except Exception as e:
            self._commsrvr_fail_count += 1
            if self._commsrvr_fail_count >= self._commsrvr_max_failures:
                self.commsrvr_timer.stop()

    def _setup_live_feed(self) -> None:
        """Create the live feed text area."""
        # Feed text area
        self.feed_text = QtWidgets.QPlainTextEdit(self.central_widget)
        self.feed_text.setObjectName("feedText")
        mono_font = QtGui.QFont(FONT_MONO, -1, QtGui.QFont.Medium)
        mono_font.setPixelSize(13)
        self.feed_text.setFont(mono_font)
        self.feed_text.setStyleSheet(
            f"background-color: {self.config.get_color('feed_background')};"
            f"color: {self.config.get_color('feed_foreground')};"
        )
        self.feed_text.setReadOnly(True)

        # No word wrap, always show scrollbars
        self.feed_text.setWordWrapMode(QtGui.QTextOption.NoWrap)
        self.feed_text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.feed_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Add to layout (row 3, full width)
        self.main_layout.addWidget(self.feed_text, 3, 0, 1, 2)

    def _load_live_feed(self) -> None:
        """Initialize the live feed display from buffer."""
        self._update_feed_display()

    def _update_feed_display(self) -> None:
        """Update the live feed display from the message buffer."""
        # Safety check - feed_text may not exist during startup
        if not hasattr(self, 'feed_text'):
            return

        if not self.feed_messages:
            self.feed_text.setPlainText("Waiting for JS8Call traffic...")
            return

        # Filter messages based on settings
        messages = self.feed_messages
        if self.config.get_hide_heartbeat():
            messages = [
                msg for msg in messages
                if 'HEARTBEAT' not in msg.upper()
                and '@ALLCALL CQ' not in msg.upper()
            ]

        # Join messages (already in newest-first order)
        self.feed_text.setPlainText('\n'.join(messages))

    def _setup_message_table(self) -> None:
        """Create the message data table."""
        self.message_table = QtWidgets.QTableWidget(self.central_widget)
        self.message_table.setObjectName("messageTable")
        self.message_table.setColumnCount(7)
        self.message_table.setRowCount(0)

        self._setup_table_widget(self.message_table, [
            "", "Date Time", "Freq", "From", "To", "ID", "0 Messages"
        ])
        self.message_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.message_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.message_table.setFixedHeight(MAP_HEIGHT)

        self.message_table.itemClicked.connect(self._on_message_click)
        self.message_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.message_table.customContextMenuRequested.connect(
            lambda pos: self._show_table_copy_menu(self.message_table, pos)
        )

        self.main_layout.addWidget(self.message_table, 4, 1, 1, 1)

    def _setup_contacts_widget(self) -> None:
        """Create the QRZ contacts table widget spanning both map and message columns."""
        _CONTACTS_HEADERS = [
            "Callsign", "Name", "Address", "City", "State",
            "Zip", "Country", "Grid", "Class", "Email", "Image", "Date Added"
        ]

        self.contacts_widget = QtWidgets.QWidget(self.central_widget)
        outer = QtWidgets.QVBoxLayout(self.contacts_widget)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.contacts_table = QtWidgets.QTableWidget(self.central_widget)
        self.contacts_table.setObjectName("contactsTable")
        self.contacts_table.setColumnCount(len(_CONTACTS_HEADERS))
        self.contacts_table.setRowCount(1)  # row 0 = filter row

        self._setup_table_widget(self.contacts_table, _CONTACTS_HEADERS)
        self.contacts_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.contacts_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)

        # Interactive mode — resizeColumnsToContents() is called once after data loads
        header = self.contacts_table.horizontalHeader()
        header.setStretchLastSection(False)
        for col in range(len(_CONTACTS_HEADERS)):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.Interactive)

        # Embed a QLineEdit in every cell of row 0 as the filter row
        filter_font = QtGui.QFont("Kode Mono", -1)
        filter_font.setPixelSize(13)
        filter_style = (
            "QLineEdit { background-color: white; color: #333333; "
            "border: none; padding: 1px 3px; }"
        )
        self._contacts_filters: List[QtWidgets.QLineEdit] = []
        for col in range(len(_CONTACTS_HEADERS)):
            edit = QtWidgets.QLineEdit()
            edit.setFont(filter_font)
            edit.setStyleSheet(filter_style)
            edit.textChanged.connect(self._apply_contacts_filter)
            self.contacts_table.setCellWidget(0, col, edit)
            self._contacts_filters.append(edit)
        self.contacts_table.setRowHeight(0, 30)

        self.contacts_table.itemClicked.connect(self._on_contacts_item_clicked)
        self.contacts_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.contacts_table.customContextMenuRequested.connect(self._on_contacts_context_menu)


        outer.addWidget(self.contacts_table)

        self.contacts_widget.setFixedHeight(MAP_HEIGHT)
        self.main_layout.addWidget(self.contacts_widget, 4, 0, 1, 2)
        self.contacts_widget.hide()

    def _load_contacts_data(self) -> None:
        """Populate the contacts table from the QRZ cache."""
        _IMAGE_COL = 10
        _DATE_COL = 11

        data_fg = self.config.get_color('data_foreground')
        kode_font = QtGui.QFont("Kode Mono", -1)
        kode_font.setPixelSize(13)
        kode_bold = QtGui.QFont("Kode Mono", -1)
        kode_bold.setPixelSize(13)
        kode_bold.setBold(True)

        rows = self.db.get_qrz_contacts()

        col_keys = ["callsign", "name", "address", "city", "state",
                    "zip", "country", "grid", "class", "email", "image", "insert_date"]

        self.contacts_table.setHorizontalHeaderItem(
            1, QTableWidgetItem(f"Name   ({len(rows)} Records)")
        )

        self.contacts_table.setUpdatesEnabled(False)
        self.contacts_table.setRowCount(1 + len(rows))  # row 0 reserved for filters

        for i, row in enumerate(rows):
            table_row = i + 1
            for col, key in enumerate(col_keys):
                raw = row[key] if row[key] is not None else ""

                if col == _IMAGE_COL:
                    if raw:
                        item = QTableWidgetItem("View")
                        item.setForeground(QColor("#0078d7"))
                        item.setData(Qt.UserRole, raw)
                    else:
                        item = QTableWidgetItem("—")
                        item.setForeground(QColor(data_fg))
                        item.setData(Qt.UserRole, None)
                elif col == _DATE_COL:
                    try:
                        dt = datetime.fromisoformat(str(raw))
                        formatted = dt.strftime("%b %d, %Y")
                    except (ValueError, TypeError):
                        formatted = str(raw)
                    item = QTableWidgetItem(formatted)
                else:
                    item = QTableWidgetItem(str(raw))

                if col == 0:
                    item.setFont(kode_bold)
                    item.setTextAlignment(Qt.AlignCenter)
                else:
                    item.setFont(kode_font)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.contacts_table.setItem(table_row, col, item)

        self.contacts_table.setUpdatesEnabled(True)
        self.contacts_table.resizeColumnsToContents()

        # Cap narrow columns that ResizeToContents makes too wide
        _MAX_WIDTHS = {4: 55, 5: 70, 7: 70, 8: 60, 10: 55}
        header = self.contacts_table.horizontalHeader()
        for col, max_w in _MAX_WIDTHS.items():
            if header.sectionSize(col) > max_w:
                header.resizeSection(col, max_w)

    def _apply_contacts_filter(self) -> None:
        """Show/hide data rows based on per-column filter inputs. Row 0 is always shown."""
        filters = [edit.text().strip().upper() for edit in self._contacts_filters]
        for row in range(1, self.contacts_table.rowCount()):
            match = True
            for col, f in enumerate(filters):
                if not f:
                    continue
                item = self.contacts_table.item(row, col)
                cell = item.text().upper() if item else ""
                if col == 0:
                    if f not in cell:
                        match = False
                        break
                else:
                    if f not in cell:
                        match = False
                        break
            self.contacts_table.setRowHidden(row, not match)

    def _on_contacts_item_clicked(self, item: QTableWidgetItem) -> None:
        """Callsign opens QRZ lookup; image opens URL; all others just select."""
        if item.row() == 0:
            return

        col = item.column()

        if col == 0:
            callsign = item.text().strip()
            if callsign:
                from qrz_lookup import QRZLookupDialog
                dlg = QRZLookupDialog(
                    module_background=self.config.get_color('module_background'),
                    module_foreground=self.config.get_color('module_foreground'),
                    program_background=self.config.get_color('program_background'),
                    program_foreground=self.config.get_color('program_foreground'),
                    parent=self
                )
                dlg.cs_edit.setText(callsign)
                dlg._search()
                dlg.msg_edit.setFocus()
                dlg.exec_()
                self.contacts_table.viewport().setFocus()
        elif col == 10:
            url = item.data(Qt.UserRole)
            if url:
                QDesktopServices.openUrl(QUrl(url))
        else:
            text = item.text().strip()
            if text and text != "—":
                QtWidgets.QApplication.clipboard().setText(text)

    def _on_contacts_context_menu(self, pos) -> None:
        """Show right-click context menu with Copy option for contacts table."""
        item = self.contacts_table.itemAt(pos)
        if not item or item.row() == 0 or item.text().strip() == "—":
            return
        self._show_table_copy_menu(self.contacts_table, pos)

    def _copy_contacts_current_cell(self) -> None:
        """Copy the currently selected contacts cell text via Ctrl+C."""
        item = self.contacts_table.currentItem()
        if item and item.row() != 0:
            text = item.text().strip()
            if text and text != "—":
                QtWidgets.QApplication.clipboard().setText(text)

    def _handle_copy_shortcut(self) -> None:
        """Single Ctrl+C handler — dispatches to whichever table viewport has focus."""
        focused = QtWidgets.QApplication.focusWidget()
        if hasattr(self, 'contacts_table') and focused in (
            self.contacts_table, self.contacts_table.viewport()
        ):
            self._copy_contacts_current_cell()
        elif focused in (self.statrep_table, self.statrep_table.viewport()):
            self._copy_table_current_cell(self.statrep_table)
        elif focused in (self.message_table, self.message_table.viewport()):
            self._copy_table_current_cell(self.message_table)

    def _show_table_copy_menu(self, table: QtWidgets.QTableWidget, pos) -> None:
        """Show a right-click Copy context menu for any table cell."""
        item = table.itemAt(pos)
        if not item:
            return
        text = item.text().strip()
        if not text:
            return
        menu = QtWidgets.QMenu(table)
        menu.setStyleSheet(
            "QMenu { background-color: #FFFFE1; color: black; border: 1px solid black; }"
            "QMenu::item:selected { background-color: #e6e600; color: black; }"
        )
        copy_action = menu.addAction("Copy")
        action = menu.exec_(table.viewport().mapToGlobal(pos))
        if action == copy_action:
            QtWidgets.QApplication.clipboard().setText(text)

    def _copy_table_current_cell(self, table: QtWidgets.QTableWidget) -> None:
        """Copy the currently selected cell in any table via Ctrl+C."""
        item = table.currentItem()
        if item:
            text = item.text().strip()
            if text:
                QtWidgets.QApplication.clipboard().setText(text)

    def _load_message_data(self) -> None:
        """Load message data from database into the table."""
        data = self.db.get_message_data(
            groups=[],
            start='',
            end='',
            show_all=True
        )

        self._populate_table(self.message_table, data)

        count = len(data)
        label = "Message" if count == 1 else "Messages"
        self.message_table.setHorizontalHeaderItem(
            6, QTableWidgetItem(f"{count} {label}")
        )

    def _load_map(self, callback=None) -> None:
        """Generate and display the folium map with StatRep pins."""
        filters = self.config.filter_settings
        groups, exclude_groups, show_all = self._get_filtered_groups()

        # Use saved map position or default to US center
        if not hasattr(self, 'map_center'):
            self.map_center = (38.8199286, -96.7782551)
            self.map_zoom = 4

        # zoomSnap=0.25 allows fractional zoom so wheelPxPerZoomLevel actually
        # matters — with Leaflet's default zoomSnap=1, every wheel tick rounds
        # up to a full zoom level no matter how small the per-tick delta is.
        m = folium.Map(
            zoom_start=self.map_zoom,
            location=self.map_center,
            wheelPxPerZoomLevel=MAP_WHEEL_PX_PER_ZOOM,
            zoomSnap=0.25,
        )

        # Add local tile layer
        folium.raster_layers.TileLayer(
            tiles='tiles://local/{z}/{x}/{y}.png',
            name='Local Tiles',
            attr='Local Tiles',
            max_zoom=8,
            control=False
        ).add_to(m)

        # Add online tile layer (OpenStreetMap) for zoom > 8, only if internet available
        if self._internet_available:
            folium.raster_layers.TileLayer(
                tiles='https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
                name='OpenStreetMap',
                attr='OpenStreetMap',
                min_zoom=8,
                control=False
            ).add_to(m)

        # Bounding boxes used to count pins per region for status-bar indicators
        # (lat_min, lat_max, lng_min, lng_max). Boxes can overlap; a pin near a
        # boundary may light multiple buttons.
        REGION_BBOX = {
            "us":      ( 24.0,  50.0, -125.0,  -66.0),
            "eu":      ( 35.0,  72.0,  -10.0,   40.0),
            "mideast": ( 12.0,  42.0,   25.0,   65.0),
            "seasia":  (-10.0,  30.0,   90.0,  140.0),
        }
        # "world" lights up when any pin falls OUTSIDE the continental US bbox.
        US_BBOX = REGION_BBOX["us"]
        region_counts = {"us": 0, "eu": 0, "mideast": 0, "seasia": 0, "world": 0}

        # Get StatRep data for pins
        try:
            _map_callsign = next((cs for cs in self.rig_callsigns.values() if cs), "") or ""
            if not _map_callsign:
                _map_callsign, _, __ = self.db.get_user_settings()
            data = self.db.get_statrep_data(
                groups=groups,
                start=filters.get('start', DEFAULT_FILTER_START),
                end=filters.get('end', ''),
                show_all=show_all,
                exclude_groups=exclude_groups,
                user_callsign=_map_callsign
            )

            gridlist = []
            for row in data:
                callsign = row[3]   # from_callsign
                srid = row[5]       # sr_id (display only)
                grid = row[6]       # grid
                status = str(row[8])  # map (status)
                statrep_id = row[22]  # database primary key (unique)

                # Convert grid to coordinates
                try:
                    coords = mh.to_location(grid, center=True)
                    lat = float(coords[0])
                    lon = float(coords[1])

                    # Offset duplicate grids
                    count = gridlist.count(grid)
                    if count > 0:
                        lat += count * 0.01
                        lon += count * 0.01
                    gridlist.append(grid)

                    # Create popup HTML
                    sr_date = row[1][:10] if row[1] else ""
                    html = f'''<HTML style="height:100%;">
                        <BODY style="margin:0;font-family:Arial,sans-serif;text-align:center;
                                     height:100%;display:flex;flex-direction:column;
                                     justify-content:center;align-items:center;">
                            <p style="font-size:15px;font-weight:bold;margin:0 0 2px 0;">{callsign}</p>
                            <p style="font-size:15px;font-weight:normal;margin:0 0 4px 0;">{sr_date}</p>
                            <a href="http://localhost/statrep/{statrep_id}/{callsign}"
                               style="font-size:15px;color:#0000EE;">Details</a>
                        </BODY>
                    </HTML>'''
                    iframe = folium.IFrame(html, width=120, height=78)
                    popup = folium.Popup(iframe, min_width=80, max_width=120)

                    # Skip green pins when filter is active
                    if self._hide_green_pins and status == "1":
                        continue

                    # Skip internet-sourced statreps when filter is active
                    if self._hide_internet_statrep and row[21] != 1:
                        continue

                    # Count this pin against any region whose bounding box contains it
                    for _region, (_lat_min, _lat_max, _lng_min, _lng_max) in REGION_BBOX.items():
                        if _lat_min <= lat <= _lat_max and _lng_min <= lon <= _lng_max:
                            region_counts[_region] += 1
                    # "world" counts pins OUTSIDE the continental US bbox
                    if not (US_BBOX[0] <= lat <= US_BBOX[1] and US_BBOX[2] <= lon <= US_BBOX[3]):
                        region_counts["world"] += 1

                    # Determine pin color and size
                    if status == "1":
                        color = "green"
                        radius = 5
                    elif status == "2":
                        color = "orange"
                        radius = 10
                    elif status == "3":
                        color = "red"
                        radius = 10
                    else:
                        color = "black"
                        radius = 5

                    folium.CircleMarker(
                        radius=radius,
                        fill=True,
                        color=color,
                        fill_color=color,
                        location=[lat, lon],
                        popup=popup
                    ).add_to(m)
                except Exception as e:
                    print(f"Error adding pin for grid {grid}: {e}")

        except Exception as e:
            print(f"Error loading map data: {e}")

        # Publish per-region pin counts to status-bar indicators
        self._region_pin_counts = region_counts
        self._update_region_button_pin_indicators()

        # Save map to bytes and display
        map_data = io.BytesIO()
        m.save(map_data, close_file=False)

        map_html = map_data.getvalue().decode()

        # Circle marker popups open on click and auto-close after 3 seconds.
        hover_js = """<script>
window.addEventListener('load', function() {
    setTimeout(function() {
        Object.keys(window).forEach(function(key) {
            var obj = window[key];
            if (obj && obj._leaflet_id && obj.eachLayer) {
                obj.eachLayer(function(layer) {
                    if (layer.getPopup && layer.getPopup()) {
                        layer._popupCloseTimer = null;
                        layer.on('popupopen', function(e) {
                            if (layer._popupCloseTimer) clearTimeout(layer._popupCloseTimer);
                            layer._popupCloseTimer = setTimeout(function() {
                                layer.closePopup();
                            }, 3000);
                        });
                        layer.on('popupclose', function(e) {
                            if (layer._popupCloseTimer) {
                                clearTimeout(layer._popupCloseTimer);
                                layer._popupCloseTimer = null;
                            }
                        });
                    }
                });
            }
        });
    }, 500);
});
</script>"""
        webkit_shim = """<script>
if (window.webkitStorageInfo === undefined && navigator.webkitTemporaryStorage) {
    Object.defineProperty(window, 'webkitStorageInfo', {
        get: function() { return navigator.webkitTemporaryStorage; }
    });
}
</script>"""
        map_html = map_html.replace('</head>', webkit_shim + '\n</head>')
        map_html = map_html.replace('</body>', hover_js + '\n</body>')

        # Always set new HTML content (reload() only refreshes cached content)
        self._last_map_html = map_html
        self.map_widget.setHtml(self._last_map_html, QUrl("http://localhost/"))
        if getattr(self, '_large_map_dlg', None) and self._large_map_dlg.isVisible():
            self._large_map_dlg.update_map(self._last_map_html)
        self.map_loaded = True

        if callback:
            callback()

    def _save_map_position(self, callback=None) -> None:
        """Save current map center and zoom via JavaScript."""
        if not self.map_loaded:
            if callback:
                callback()
            return

        js_code = """
        (function() {
            try {
                var mapId = Object.keys(window).find(k => k.startsWith('map_'));
                if (mapId && window[mapId]) {
                    var map = window[mapId];
                    var center = map.getCenter();
                    var zoom = map.getZoom();
                    return JSON.stringify({lat: center.lat, lng: center.lng, zoom: zoom});
                }
            } catch(e) {}
            return null;
        })();
        """

        def handle_result(result):
            if result:
                try:
                    import json
                    data = json.loads(result)
                    self.map_center = (data['lat'], data['lng'])
                    self.map_zoom = data['zoom']
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    pass
            if callback:
                callback()

        self.map_widget.page().runJavaScript(js_code, handle_result)

    def _load_statrep_data(self) -> None:
        """Load StatRep data from database into the table."""
        filters = self.config.filter_settings
        groups, exclude_groups, show_all = self._get_filtered_groups()

        user_callsign = next((cs for cs in self.rig_callsigns.values() if cs), "") or ""
        if not user_callsign:
            user_callsign, _, __ = self.db.get_user_settings()

        # Fetch data from database
        data = self.db.get_statrep_data(
            groups=groups,
            start=filters.get('start', DEFAULT_FILTER_START),
            end=filters.get('end', ''),
            show_all=show_all,
            exclude_groups=exclude_groups,
            user_callsign=user_callsign
        )

        if self._hide_internet_statrep:
            data = [row for row in data if row[21] == 1]
        if self._hide_green_pins:
            data = [row for row in data if str(row[8]) != "1"]

        # Status color mapping for values 1-4
        status_colors = {
            "1": "condition_green",
            "2": "condition_yellow",
            "3": "condition_red",
            "4": "condition_gray"
        }
        self._populate_table(self.statrep_table, data, status_colors)

    def _on_statrep_click(self, item: QTableWidgetItem) -> None:
        """From callsign (col 3) opens detail view; all other cells copy their text."""
        _FROM_COL = 3
        row = item.row()

        if item.column() == _FROM_COL:
            callsign_item = self.statrep_table.item(row, _FROM_COL)
            if callsign_item:
                callsign  = callsign_item.text().strip()
                record_id = callsign_item.data(QtCore.Qt.UserRole)
                table = self.statrep_table
                def build_record_list():
                    items = []
                    for r in range(table.rowCount()):
                        ci = table.item(r, _FROM_COL)
                        if ci:
                            rid = ci.data(QtCore.Qt.UserRole)
                            if rid is not None:
                                items.append((rid, ci.text().strip()))
                    return items
                from qrz_lookup import StatRepDetailDialog
                dlg = StatRepDetailDialog(
                    record_id, callsign, self._internet_available,
                    commsrvr_url=_COMMSRVR,
                    module_background=self.config.get_color('module_background'),
                    module_foreground=self.config.get_color('module_foreground'),
                    title_bar_background=self.config.get_color('title_bar_background'),
                    title_bar_foreground=self.config.get_color('title_bar_foreground'),
                    data_background=self.config.get_color('data_background'),
                    program_background=self.config.get_color('program_background'),
                    program_foreground=self.config.get_color('program_foreground'),
                    condition_green=self.config.get_color('condition_green'),
                    condition_yellow=self.config.get_color('condition_yellow'),
                    condition_red=self.config.get_color('condition_red'),
                    condition_gray=self.config.get_color('condition_gray'),
                    tcp_pool=self.tcp_pool,
                    connector_manager=self.connector_manager,
                    record_list_provider=build_record_list,
                    refresh_callback=self._load_message_data,
                    parent=self
                )
                dlg.pin_changed.connect(
                    lambda _: self._save_map_position(callback=self._load_map)
                )
                dlg.record_deleted.connect(self._load_statrep_data)
                dlg.record_deleted.connect(
                    lambda: self._save_map_position(callback=self._load_map)
                )
                if dlg.exec_() == 1:
                    self._load_statrep_data()
        else:
            text = item.text().strip()
            if text:
                QtWidgets.QApplication.clipboard().setText(text)

    def _on_message_click(self, item: QTableWidgetItem) -> None:
        """From callsign (col 3) opens detail view; all other cells copy their text."""
        _FROM_COL = 3
        row = item.row()

        if item.column() == _FROM_COL:
            callsign_item = self.message_table.item(row, _FROM_COL)
            message_item  = self.message_table.item(row, 6)
            msg_id_item   = self.message_table.item(row, 5)
            if callsign_item:
                callsign = callsign_item.text().strip()
                message_text = (message_item.data(QtCore.Qt.UserRole) or message_item.text()) if message_item else ""
                msg_id = msg_id_item.text().strip() if msg_id_item else ""
                from qrz_lookup import MessageDetailDialog
                dlg = MessageDetailDialog(
                    callsign, message_text, self._internet_available,
                    module_background=self.config.get_color('module_background'),
                    module_foreground=self.config.get_color('module_foreground'),
                    data_background=self.config.get_color('data_background'),
                    program_background=self.config.get_color('program_background'),
                    program_foreground=self.config.get_color('program_foreground'),
                    msg_id=msg_id,
                    refresh_callback=self._load_message_data,
                    parent=self
                )
                dlg.record_deleted.connect(self._load_message_data)
                if dlg.exec_() == 1:
                    self._load_message_data()
        else:
            text = item.text().strip()
            if text:
                QtWidgets.QApplication.clipboard().setText(text)

    def _on_qrz_record_written(self, callsign: str) -> None:
        """Bold any matching From-callsign (col 3) cells in StatRep and Message tables."""
        cs = callsign.strip().upper()
        if not cs:
            return
        for table in (self.statrep_table, self.message_table):
            for row in range(table.rowCount()):
                item = table.item(row, 3)
                if item and item.text().strip().upper() == cs:
                    font = item.font()
                    if not font.bold():
                        font.setBold(True)
                        item.setFont(font)
                    item.setToolTip("Exists in QRZ local cache")

    def _on_qrz_contacts_menu(self) -> None:
        """Toggle QRZ Contacts view; switch back to map if already showing."""
        if hasattr(self, 'contacts_widget') and self.contacts_widget.isVisible():
            self._set_map_view_mode(getattr(self, "_last_map_region", "us"))
        else:
            self._set_map_view_mode("contacts")

    def _on_grid_finder(self) -> None:
        """Open Grid Finder as an in-process modeless window; reuse if already open."""
        existing = getattr(self, "_grid_finder_window", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        Cls = self._resolve_dialog_class("gridfinder", "GridFinderApp")
        win = Cls(parent=self)
        geo = self.geometry()
        ww, wh = win.width(), win.height()
        win.move(geo.x() + (geo.width() - ww) // 2, geo.y() + (geo.height() - wh) // 2)
        self._grid_finder_window = win
        win.show()

    def _on_brevity_generator(self) -> None:
        """Open the Brevity Code Generator in-process; reuse if already open."""
        existing = getattr(self, "_brevity_window", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        Cls = self._resolve_dialog_class("brevity", "BrevityApp")
        win = Cls(
            panel_bg=self.config.get_color('module_background'),
            panel_fg=self.config.get_color('module_foreground'),
            parent=self,
        )
        geo = self.geometry()
        ww, wh = win.width(), win.height()
        win.move(geo.x() + (geo.width() - ww) // 2, geo.y() + (geo.height() - wh) // 2)
        self._brevity_window = win
        win.show()

    def _resolve_dialog_class(self, module_name: str, class_name: str):
        import importlib
        return getattr(importlib.import_module(module_name), class_name)

    def _on_help(self) -> None:
        HelpDialogCls = self._resolve_dialog_class("help", "HelpDialog")
        dlg = HelpDialogCls(self)
        dlg.exec_()

    def _on_whats_new(self) -> None:
        """Open the What's New page in the user's browser."""
        QDesktopServices.openUrl(QUrl("https://commstat.app/new-features.php"))

    def _on_live_better(self) -> None:
        """Open the Live Better page in the user's browser."""
        QDesktopServices.openUrl(QUrl("https://commstat.app/how-are-you-feeling.php"))

    def _on_live_radiation_map(self) -> None:
        """Open the Live Radiation Map (gmcmap.com) in the user's browser."""
        QDesktopServices.openUrl(QUrl("https://gmcmap.com/"))

    def _on_qrz_lookup(self) -> None:
        """Open standalone QRZ Lookup dialog (Tools menu)."""
        QRZLookupDialogCls = self._resolve_dialog_class("qrz_lookup", "QRZLookupDialog")
        dlg = QRZLookupDialogCls(
            module_background=self.config.get_color('module_background'),
            module_foreground=self.config.get_color('module_foreground'),
            program_background=self.config.get_color('program_background'),
            program_foreground=self.config.get_color('program_foreground'),
            refresh_callback=self._load_message_data,
            parent=self
        )
        dlg.exec_()

    def _setup_timers(self) -> None:
        """Setup timers for clock, data refresh, and news feed animation."""
        # Clock timer - updates every second
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._update_time)
        self.clock_timer.start(1000)
        self._update_time()  # Initial display
        self._update_connected_rigs_display()  # Initial connected rigs display

        # Periodic resync of the status-bar rig indicators against live socket
        # state. Why: the indicators are otherwise event-driven via
        # any_connection_changed; if a signal is missed (e.g. socket transitions
        # without `disconnected` firing) the indicators drift out of sync with
        # the JS8 Connectors dialog. A 5s poll keeps them self-correcting
        # without churning sqlite during degraded states.
        self.rig_status_timer = QTimer(self)
        self.rig_status_timer.timeout.connect(self._update_connected_rigs_display)
        self.rig_status_timer.start(5000)

        # Internet check timer - retries every 30 minutes if offline
        self.internet_timer = QTimer(self)
        self.internet_timer.timeout.connect(self._retry_internet_check)
        if not self._internet_available:
            self.internet_timer.start(INTERNET_CHECK_INTERVAL)

        # Commsrvr check timer - runs every 3 minutes, starts 30 seconds after launch
        self._commsrvr_fail_count = 0
        self._commsrvr_max_failures = 20
        self.commsrvr_timer = QTimer(self)
        self.commsrvr_timer.timeout.connect(self._check_commsrvr)
        if self._internet_available:
            # Delay first heartbeat by HEARTBEAT_DELAY_MS, then start timer for subsequent heartbeats
            def start_commsrvr_heartbeat():
                self._check_commsrvr()  # Send first heartbeat immediately
                self.commsrvr_timer.start(180000)  # Then start 3 minute interval timer
            QTimer.singleShot(HEARTBEAT_DELAY_MS, start_commsrvr_heartbeat)

        # News ticker animation timer
        self.newsfeed_timer = QTimer(self)
        self.newsfeed_timer.timeout.connect(self._tick_newsfeed)
        # Owned single-shot for the type-on→scroll-off pause; named so we can
        # cancel it when the animation is restarted from outside the natural
        # _next_headline chain (resize debounce, RSS refresh, feed change).
        self.newsfeed_pause_timer = QTimer(self)
        self.newsfeed_pause_timer.setSingleShot(True)
        self.newsfeed_pause_timer.timeout.connect(self._start_scroll_phase)
        self._newsfeed_frame = 0
        self._newsfeed_phase = 0  # 0 = type-on, 1 = scroll-off
        self._scroll_start = 0.0

        # News ticker state
        self.newsfeed_text = ""
        self.newsfeed_chars = 0
        self.rss_fetcher = RSSFetcher()
        self.headline_index = 0
        self.headlines: List[str] = []

        # RSS refresh timer - refreshes feed every 5 minutes
        self.rss_timer = QTimer(self)
        self.rss_timer.timeout.connect(self._refresh_rss_feed)
        self.rss_timer.start(300000)  # 5 minutes

        # Contacts retention timer - purge rows older than
        # CONTACTS_RETENTION_HOURS at startup, then once per hour.
        self.contacts_purge_timer = QTimer(self)
        self.contacts_purge_timer.timeout.connect(self._purge_old_contacts)
        self.contacts_purge_timer.start(3600 * 1000)  # 1 hour
        self._purge_old_contacts()

        # Initial RSS fetch
        if self.config.get_selected_rss_feed() == "Disable":
            self.newsfeed_label.setText("      +++  News Feed Disabled  +++")
        elif self._internet_available:
            self._start_rss_fetch()

    def _check_commsrvr(self) -> None:
        """Check commsrvr server for content updates (runs in background thread)."""
        if not self._internet_available:
            return
        thread = threading.Thread(target=self._check_commsrvr_content_async, daemon=True)
        thread.start()

    def _update_time(self) -> None:
        """Update the time display with current UTC time."""
        current_time = QDateTime.currentDateTimeUtc()
        self.time_label.setText(current_time.toString("hh:mm:ss"))

    def _update_connected_rigs_display(self) -> None:
        """Update the connected rigs display with currently connected rig names."""
        connected = self.tcp_pool.get_connected_rig_names()

        # Update status bar widgets for each rig
        all_rigs = self.connector_manager.get_all_connectors()

        # Remove widgets for rigs that no longer exist
        all_rig_names = [r['rig_name'] for r in all_rigs]
        for rig_name in list(self.rig_status_widgets.keys()):
            if rig_name not in all_rig_names:
                label_rig, label_status = self.rig_status_widgets[rig_name]
                self.statusbar.removeWidget(label_rig)
                self.statusbar.removeWidget(label_status)
                label_rig.deleteLater()
                label_status.deleteLater()
                del self.rig_status_widgets[rig_name]

        # Create or update widgets for each rig
        for rig in all_rigs:
            rig_name = rig['rig_name']
            is_enabled = rig.get('enabled', 1) == 1
            is_connected = rig_name in connected

            if rig_name not in self.rig_status_widgets:
                # Create new widgets for this rig
                # Rig name label with sunken effect
                label_rig = QtWidgets.QLabel(f" {rig_name} ")
                label_rig.setFrameStyle(QtWidgets.QFrame.Panel | QtWidgets.QFrame.Sunken)
                label_rig.setLineWidth(2)

                # Status label with sunken effect
                label_status = QtWidgets.QLabel()
                label_status.setFrameStyle(QtWidgets.QFrame.Panel | QtWidgets.QFrame.Sunken)
                label_status.setLineWidth(2)

                # Add to status bar (on right side)
                self.statusbar.addPermanentWidget(label_rig)
                self.statusbar.addPermanentWidget(label_status)

                # Store references
                self.rig_status_widgets[rig_name] = (label_rig, label_status)

            # Update status label
            _, label_status = self.rig_status_widgets[rig_name]
            if not is_enabled:
                label_status.setText(" Disabled ")
                label_status.setStyleSheet(
                    "background-color: #888888; color: white;"
                    " font-family: Roboto; font-size: 12px; font-weight: normal;"
                )
            elif is_connected:
                label_status.setText(" Connected ")
                label_status.setStyleSheet(
                    "background-color: #00dd00; color: black;"
                    " font-family: Roboto; font-size: 12px; font-weight: normal;"
                )
            else:
                label_status.setText(" Disconnected ")
                label_status.setStyleSheet(
                    "background-color: #dd0000; color: white;"
                    " font-family: Roboto; font-size: 12px; font-weight: normal;"
                )

    def _tick_newsfeed(self) -> None:
        """Timer-driven tick for news feed animation."""
        text = self.newsfeed_text
        visible = self.newsfeed_chars

        if self._newsfeed_phase == 0:
            # Type-on: reveal characters one at a time
            frame = self._newsfeed_frame
            self.newsfeed_label.setText(text[0:frame])
            self._newsfeed_frame += 1
            if self._newsfeed_frame >= visible:
                # Window is full — pause before scrolling
                self.newsfeed_timer.stop()
                self.newsfeed_pause_timer.start(NEWSFEED_PAUSE_MS)
        else:
            # Scroll-off: wall-clock-based so duration is accurate on Windows
            elapsed = time.monotonic() - self._scroll_start
            progress = min(1.0, elapsed / (NEWSFEED_SCROLL_DURATION_MS / 1000.0))
            scroll_steps = len(text) - visible
            offset = int(progress * scroll_steps)
            frame = visible + offset
            self.newsfeed_label.setText(text[frame - visible:frame])
            if progress >= 1.0:
                self.newsfeed_timer.stop()
                self._next_headline()

    def _start_scroll_phase(self) -> None:
        """Begin the scroll-off phase after the pause."""
        self._newsfeed_phase = 1
        self._scroll_start = time.monotonic()
        self.newsfeed_timer.start(16)  # ~60 fps; position derived from wall-clock time

    def _next_headline(self) -> None:
        """Called when news ticker animation completes - show next headline."""
        if self.headlines:
            self.headline_index = (self.headline_index + 1) % len(self.headlines)
        self._display_current_headline()

    def _start_rss_fetch(self) -> None:
        """Start fetching RSS feed in background."""
        feed_name = self.config.get_selected_rss_feed()
        feed_url = DEFAULT_RSS_FEEDS.get(feed_name, list(DEFAULT_RSS_FEEDS.values())[0])
        self.newsfeed_label.setText("  Loading news...")
        self.rss_fetcher.fetch_async(feed_url, callback=self._on_rss_fetched)

    def _on_rss_fetched(self) -> None:
        """Called when RSS fetch completes (from background thread)."""
        # Use QTimer to safely update UI from main thread
        QTimer.singleShot(0, self._update_headlines_from_fetch)

    def _update_headlines_from_fetch(self) -> None:
        """Update headlines list and start display (called on main thread)."""
        feed_name = self.config.get_selected_rss_feed()
        feed_url = DEFAULT_RSS_FEEDS.get(feed_name, list(DEFAULT_RSS_FEEDS.values())[0])
        self.headlines = self.rss_fetcher.get_headlines(feed_url)
        self.headline_index = 0
        self._display_current_headline()

    def _refresh_rss_feed(self) -> None:
        """Refresh RSS feed periodically."""
        feed_name = self.config.get_selected_rss_feed()
        if self._internet_available and feed_name != "Disable":
            feed_url = DEFAULT_RSS_FEEDS.get(feed_name, list(DEFAULT_RSS_FEEDS.values())[0])
            self.rss_fetcher.fetch_async(feed_url, callback=self._on_rss_fetched)

    def _display_current_headline(self) -> None:
        """Display the current headline with scrolling animation."""
        if not self.headlines:
            self.newsfeed_label.setText("  No news available")
            return

        try:
            headline = self.headlines[self.headline_index]

            # Set green color for news headlines
            self.newsfeed_label.setStyleSheet(
                f"background-color: {self.config.get_color('newsfeed_background')};"
                f"color: {self.config.get_color('newsfeed_foreground')};"
            )

            # Build ticker text with headline
            ticker_text = f" {headline}"

            # Calculate how many characters fit in the ticker width.
            # averageCharWidth() overestimates for typical ASCII news text because
            # it averages across all Unicode glyphs. Measure a representative
            # lowercase+space sample instead for a more accurate fit.
            fm = self.newsfeed_label.fontMetrics()
            sample = 'abcdefghijklmnopqrstuvwxyz '
            avg_char_px = fm.horizontalAdvance(sample) / len(sample)
            self.newsfeed_chars = int(self.newsfeed_label.width() / avg_char_px)

            # Add padding spaces
            padding = ' ' * self.newsfeed_chars
            self.newsfeed_text = ticker_text + padding

            # Setup and start animation — cancel any pending pause from a
            # previous cycle so it can't fire scroll-off mid-type-on.
            self.newsfeed_timer.stop()
            self.newsfeed_pause_timer.stop()
            self._newsfeed_frame = 0
            self._newsfeed_phase = 0
            self.newsfeed_timer.start(NEWSFEED_TYPE_INTERVAL_MS)
        except (IndexError, TypeError) as e:
            print(f"Error displaying headline: {e}")
            self.newsfeed_label.setText("  News feed error")

    def eventFilter(self, obj, event):
        """Watch for newsfeed_label resizes to refresh ticker length."""
        if obj is getattr(self, 'newsfeed_label', None) and event.type() == QtCore.QEvent.Resize:
            # Debounce: restart the timer so we only refresh once the drag settles.
            self._newsfeed_resize_timer.start(150)
        return super().eventFilter(obj, event)

    def _refresh_newsfeed_for_resize(self) -> None:
        """Recompute and restart the current headline at the new label width."""
        # Only refresh if we are actively scrolling a headline (not Disabled / no-internet states).
        feed_name = self.config.get_selected_rss_feed()
        if feed_name == "Disable" or not self._internet_available or not self.headlines:
            return
        self.newsfeed_timer.stop()
        self.newsfeed_pause_timer.stop()
        self._display_current_headline()

    def _on_feed_changed(self, feed_name: str) -> None:
        """Handle feed selection change."""
        self.config.set_selected_rss_feed(feed_name)
        self.rss_fetcher.clear_cache()
        self.headlines = []
        self.headline_index = 0
        self.newsfeed_timer.stop()
        self.newsfeed_pause_timer.stop()
        if feed_name == "Disable":
            self.newsfeed_label.setText("      +++  News Feed Disabled  +++")
        elif self._internet_available:
            self._start_rss_fetch()
        else:
            self.newsfeed_label.setText("  No internet connection")

    def _on_last20_clicked(self) -> None:
        """Show dialog with last 20 news headlines."""
        headlines = self.headlines if self.headlines else ["No headlines available"]

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Last 20 News Headlines")
        dialog.setMinimumSize(600, 400)

        layout = QtWidgets.QVBoxLayout(dialog)

        # Feed name label
        feed_name = self.feed_combo.currentText()
        feed_label = QtWidgets.QLabel(f"Feed: {feed_name}")
        feed_label.setFont(QtGui.QFont("Arial", 12, QtGui.QFont.Bold))
        layout.addWidget(feed_label)

        # Headlines list
        list_widget = QtWidgets.QListWidget()
        _list_font = QtGui.QFont("Roboto", -1)
        _list_font.setPixelSize(13)
        list_widget.setFont(_list_font)
        list_widget.setAlternatingRowColors(True)
        for i, headline in enumerate(headlines[:20], 1):
            list_widget.addItem(f"{i}. {headline}")
        layout.addWidget(list_widget)

        # Close button
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)

        dialog.exec_()

    # -------------------------------------------------------------------------
    # Menu Action Handlers (placeholders for now)
    # -------------------------------------------------------------------------

    def _on_js8email(self) -> None:
        """Open JS8 Email window."""
        Cls = self._resolve_dialog_class("js8mail", "JS8MailDialog")
        dialog = Cls(self.tcp_pool, self.connector_manager, self)
        dialog.exec_()

    def _on_js8sms(self) -> None:
        """Open JS8 SMS window."""
        Cls = self._resolve_dialog_class("js8sms", "JS8SMSDialog")
        dialog = Cls(self.tcp_pool, self.connector_manager, self)
        dialog.exec_()

    def _on_js8_direct_message(self) -> None:
        """Open JS8 Direct Message window."""
        Cls = self._resolve_dialog_class("js8_direct_message", "JS8DirectMessageDialog")
        dialog = Cls(self.tcp_pool, self.connector_manager, self._load_message_data, parent=self)
        dialog.exec_()

    def _on_statrep(self) -> None:
        """Open StatRep window."""
        Cls = self._resolve_dialog_class("statrep", "StatRepDialog")
        dialog = Cls(
            self.tcp_pool, self.connector_manager, self,
            module_background=self.config.get_color('module_background'),
            data_background=self.config.get_color('data_background')
        )
        dialog.exec_()

    def _on_send_message(self) -> None:
        """Open Send Message window."""
        Cls = self._resolve_dialog_class("group_message", "GroupMessageDialog")
        dialog = Cls(self.tcp_pool, self.connector_manager, self._load_message_data, parent=self)
        dialog.exec_()

    def _on_group_alert(self) -> None:
        """Open Group Alert window."""
        Cls = self._resolve_dialog_class("alert", "AlertDialog")
        dialog = Cls(self.tcp_pool, self.connector_manager, self._trigger_show_alerts, parent=self)
        dialog.exec_()

    def _on_filter(self) -> None:
        """Open Display Filter window."""
        Cls = self._resolve_dialog_class("filter", "FilterDialog")
        dialog = Cls(self.config.filter_settings, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            # Update filter settings directly
            self.config.filter_settings = dialog.get_filters()
            # Refresh data with new filters
            self._refresh_all_data()

    def _reset_filter_date(self, days_ago: int) -> None:
        """Reset filter start date to specified days ago and apply."""
        from datetime import datetime, timedelta, timezone

        # Calculate new start date using UTC time
        if days_ago == 0:
            # For midnight, use current UTC date at 00:00:00
            utc_now = datetime.now(timezone.utc)
            new_start = utc_now.strftime("%Y-%m-%d") + " 00:00:00"
        else:
            # For days ago, calculate from current UTC time
            new_start = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")

        # Update in-memory filter settings
        self.config.filter_settings = {
            'start': new_start,
            'end': ''  # No end date
        }

        # Refresh data with new filters
        self._refresh_all_data()

        print(f"Filter reset (UTC): start={new_start}")

    def _on_toggle_heartbeat(self, checked: bool) -> None:
        """Toggle heartbeat message filtering in live feed."""
        self.config.set_hide_heartbeat(checked)
        self._load_live_feed()

    def _on_toggle_hide_internet_statrep(self, checked: bool) -> None:
        """Show only RF-sourced (source=1) statreps in table and map. Session-only — resets on restart."""
        self._hide_internet_statrep = checked
        self._load_statrep_data()
        self._save_map_position(callback=self._load_map)

    def _on_toggle_hide_green_pins(self, checked: bool) -> None:
        """Hide green (all-clear) statreps from table and map. Session-only — resets on restart."""
        self._hide_green_pins = checked
        self._load_statrep_data()
        self._save_map_position(callback=self._load_map)

    def _on_toggle_save_all_alerts(self, checked: bool) -> None:
        """Save all incoming group alerts, not just those for groups in the local table."""
        self.config.set_save_all_alerts(checked)

    def _on_toggle_save_all_messages(self, checked: bool) -> None:
        """Save all incoming group messages, not just those for groups in the local table."""
        self.config.set_save_all_messages(checked)

    def _on_toggle_hide_live_feed(self, checked: bool) -> None:
        """Hide/show the live feed. Session-only — resets on restart."""
        self._hide_live_feed = checked
        if checked:
            self.feed_text.hide()
            self.main_layout.setRowStretch(3, 0)
        else:
            self.feed_text.show()
            self.main_layout.setRowStretch(3, 1)

    def _on_large_map(self) -> None:
        """Open or raise the large map breakout window."""
        if getattr(self, '_large_map_dlg', None) and self._large_map_dlg.isVisible():
            self._large_map_dlg.raise_()
            self._large_map_dlg.activateWindow()
            return
        html = getattr(self, '_last_map_html', '')
        self._large_map_dlg = LargeMapDialog(html, main_window=self, parent=self)
        # Drop our reference when the dialog is destroyed so a stale handle
        # doesn't keep the QWebEngineView (and its renderer) alive.
        self._large_map_dlg.destroyed.connect(
            lambda *_: setattr(self, '_large_map_dlg', None)
        )
        self._large_map_dlg.show()

    def _trigger_show_alerts(self) -> None:
        """Trigger Show Alerts mode when a new alert is received."""
        self._set_map_view_mode("alerts")

    def _get_filtered_groups(self) -> tuple:
        """Get groups list and show_all flag based on current filter settings.

        Returns:
            Tuple of (include_groups, exclude_groups, show_all) where:
            - include_groups: Group names to include (used when show_all=False)
            - exclude_groups: Group names to exclude (used when show_all=True)
            - show_all: True if base query shows all records (filtered by exclusions)
        """
        show_all = self.config.get_show_every_group()
        all_groups = self.db.get_all_groups()
        unchecked = set(self.config.get_unchecked_groups())

        if show_all:
            exclude = [g for g in all_groups if g in unchecked]
            return [], exclude, True
        else:
            checked = [g for g in all_groups if g not in unchecked]
            return checked, [], False

    def _populate_table(self, table, data, status_colors: dict = None) -> None:
        """Populate a table widget with data.

        Args:
            table: QTableWidget to populate
            data: List of row tuples
            status_colors: Optional dict mapping values to config color keys
        """
        table.setRowCount(0)
        is_message_table = (table == self.message_table)
        is_statrep_table = (table == self.statrep_table)

        # Pre-fetch QRZ callsigns and user callsign for bold highlighting
        qrz_callsigns = self.db.get_qrz_callsigns()
        user_callsign, _, __ = self.db.get_user_settings()
        user_callsign = user_callsign.upper() if user_callsign else ""

        for row_num, row_data in enumerate(data):
            table.insertRow(row_num)

            for col_num, value in enumerate(row_data):
                display_value = str(value) if value is not None else ""

                # Decode || newline placeholders in statrep remarks and messages
                raw_message = None
                if is_statrep_table and col_num == 20 and "||" in display_value:
                    decoded_remarks = display_value.replace("||", "\n")
                    display_value = display_value.replace("||", " ")
                elif is_message_table and col_num == 6 and "||" in display_value:
                    raw_message = display_value          # preserve for detail dialog
                    display_value = display_value.replace("||", " ")
                    decoded_remarks = None
                else:
                    decoded_remarks = None

                # Handle SNR (db) column (first column)
                if (is_statrep_table or is_message_table) and col_num == 0:
                    display_value = ""
                    item = QTableWidgetItem(display_value)
                    try:
                        # Check if source = 2 (Internet source)
                        source_value = None
                        if is_statrep_table and len(row_data) > 21:
                            source_value = int(row_data[21]) if row_data[21] is not None else 0
                        elif is_message_table and len(row_data) > 7:
                            source_value = int(row_data[7]) if row_data[7] is not None else 0

                        if source_value == 2:
                            item.setToolTip("   Internet")
                            color = QColor("#9400ff")
                            item.setBackground(color)
                            table.setItem(row_num, col_num, item)
                            continue

                        if source_value == 3:
                            item.setToolTip("   Internet Only")
                            color = QColor("#FF00FF")
                            item.setBackground(color)
                            table.setItem(row_num, col_num, item)
                            continue

                        # Default SNR-based coloring
                        db_value = int(value) if value is not None else 0
                        item.setToolTip(f"   RF SNR {db_value}")
                        if db_value >= -5:
                            color = QColor(self.config.get_color('condition_green'))
                        elif db_value >= -16:
                            color = QColor(self.config.get_color('condition_yellow'))
                        else:
                            color = QColor(self.config.get_color('condition_red'))
                        item.setBackground(color)
                    except (ValueError, TypeError):
                        pass
                    table.setItem(row_num, col_num, item)
                    continue

                # Format datetime column as "Mon DD HH:MM" - column 1 for both tables
                if (is_message_table or is_statrep_table) and col_num == 1:
                    try:
                        dt = datetime.strptime(display_value[:19], "%Y-%m-%d %H:%M:%S")
                        display_value = dt.strftime("%b-%d  %H:%M")
                    except (ValueError, TypeError):
                        display_value = display_value[:16]

                # Format frequency column (column 2) - convert Hz to MHz
                if (is_message_table or is_statrep_table) and col_num == 2:
                    try:
                        freq_mhz = hz_to_mhz(float(value) if value else 0)
                        display_value = f"{freq_mhz:.3f}"  # Show as 7.110
                    except (ValueError, TypeError):
                        pass

                item = QTableWidgetItem(display_value)

                # Use Kode Mono for remarks/message text columns
                if (is_statrep_table and col_num == 20) or (is_message_table and col_num == 6):
                    item.setFont(QtGui.QFont("Kode Mono", -1))

                # Add tooltip for multi-line remarks
                if decoded_remarks:
                    item.setToolTip(decoded_remarks)

                # Store raw message text (with ||) so detail dialog can show newlines
                if raw_message is not None and is_message_table and col_num == 6:
                    item.setData(QtCore.Qt.UserRole, raw_message)

                # Bold From callsign (col 3) if callsign is in QRZ cache
                if col_num == 3:
                    from_call = display_value.upper()
                    if from_call in qrz_callsigns:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                        item.setToolTip("Exists in QRZ local cache")
                # Bold To callsign (col 4) only when it matches the user's callsign
                elif col_num == 4:
                    to_call = display_value.upper()
                    if is_message_table and user_callsign and to_call == user_callsign:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                # Bold the message ID (col 5) with a "Delivered" tooltip once the
                # commsrvr confirms delivery (delivered = 1 in the messages table).
                elif is_message_table and col_num == 5 and len(row_data) > 8 and row_data[8]:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setToolTip("  Delivery Confirmed")

                if status_colors and value in status_colors:
                    color = QColor(self.config.get_color(status_colors[value]))
                    item.setBackground(color)
                    item.setForeground(color)

                table.setItem(row_num, col_num, item)

            # Store database id on the callsign cell for statrep rows
            if is_statrep_table and len(row_data) > 22:
                cs_item = table.item(row_num, 3)
                if cs_item:
                    cs_item.setData(QtCore.Qt.UserRole, row_data[22])

        # Alert table (non-statrep, non-message): sort by first column descending
        if not is_message_table and not is_statrep_table:
            table.sortItems(0, QtCore.Qt.DescendingOrder)

        if is_message_table:
            count = table.rowCount()
            label = "1 Message" if count == 1 else f"{count} Messages"
            header_item = table.horizontalHeaderItem(6)
            if header_item is not None:
                header_item.setText(label)

    def _get_normalization_settings(self) -> Tuple[bool, Optional[Dict[str, str]]]:
        """Get text normalization flag and abbreviations dict.

        Returns:
            (apply_normalization, abbreviations) where abbreviations is None if disabled.
        """
        apply = self.config.get_apply_text_normalization()
        abbrevs = self.db.get_abbreviations() if apply else None
        return apply, abbrevs

    def _normalize_text(self, text: str) -> str:
        """Apply smart title case with abbreviation expansion if normalization is enabled."""
        apply, abbrevs = self._get_normalization_settings()
        return smart_title_case(text, abbrevs, apply) if apply else text

    def _refresh_all_data(self) -> None:
        """Refresh all data views (statrep, messages, and map)."""
        self._load_statrep_data()
        self._load_message_data()
        self._save_map_position(callback=self._load_map)

    def _on_toggle_show_every_group(self, checked: bool) -> None:
        """Toggle showing all groups data (no group filtering)."""
        self.config.set_show_every_group(checked)
        self._refresh_all_data()

    def _on_toggle_group_filter(self, group_name: str, checked: bool) -> None:
        """Toggle a single group's visibility in the StatRep display."""
        unchecked = self.config.get_unchecked_groups()
        if not checked and group_name not in unchecked:
            unchecked.append(group_name)
        elif checked and group_name in unchecked:
            unchecked.remove(group_name)
        self.config.set_unchecked_groups(unchecked)
        self._refresh_all_data()

    def _on_toggle_text_normalization(self, checked: bool) -> None:
        """Toggle text normalization (abbreviation expansion and smart title case)."""
        self.config.set_apply_text_normalization(checked)
        self._refresh_all_data()

    def _on_manage_groups(self) -> None:
        """Open Manage Groups window."""
        Cls = self._resolve_dialog_class("groups", "GroupsDialog")
        dialog = Cls(self.db, self)
        dialog.exec_()
        self._populate_groups_menu()
        self._populate_filter_groups_menu()
        # Prune unchecked_groups entries for groups that no longer exist
        all_groups = set(self.db.get_all_groups())
        pruned = [g for g in self.config.get_unchecked_groups() if g in all_groups]
        self.config.set_unchecked_groups(pruned)
        self._refresh_all_data()


    def _create_menu_checkbox(self, menu, label: str, is_checked: bool, handler) -> QtWidgets.QCheckBox:
        """Create a styled checkbox as a menu item and add it to the given menu."""
        panel_bg = self.config.get_color('module_background')
        panel_fg = self.config.get_color('module_foreground')
        checked_color = self.config.get_color('menu_background')
        checkbox = QtWidgets.QCheckBox(label)
        checkbox.setChecked(is_checked)
        _cb_font = QtGui.QFont("Roboto")
        _cb_font.setPixelSize(13)
        checkbox.setFont(_cb_font)
        checkbox.setStyleSheet(f"""
            QCheckBox {{
                padding: 4px 8px;
                background-color: {panel_bg};
                color: {panel_fg};
                font-family: Roboto;
                font-size: 13px;
            }}
            QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                background-color: white;
                border: 1px solid #555555;
                border-radius: 2px;
            }}
            QCheckBox::indicator:checked {{
                background-color: {checked_color};
                border: 1px solid {checked_color};
                border-radius: 2px;
            }}
        """)
        checkbox.stateChanged.connect(lambda state: handler(state == Qt.Checked))
        action = QtWidgets.QWidgetAction(self)
        action.setDefaultWidget(checkbox)
        menu.addAction(action)
        return checkbox

    def _populate_groups_menu(self) -> None:
        """Remove any stale group label actions from the Config menu."""
        actions = self.groups_menu.actions()
        for action in actions[8:]:
            self.groups_menu.removeAction(action)

    def _populate_filter_groups_menu(self) -> None:
        """Populate filter menu with per-group checkboxes above 'Show All Groups'."""
        for action in self.filter_group_actions.values():
            self.filter_menu.removeAction(action)
        self.filter_group_actions.clear()

        panel_bg = self.config.get_color('module_background')
        panel_fg = self.config.get_color('module_foreground')
        checked_color = self.config.get_color('menu_background')

        unchecked = set(self.config.get_unchecked_groups())
        for name in self.db.get_all_groups():
            checkbox = QtWidgets.QCheckBox(name)
            checkbox.setChecked(name not in unchecked)
            checkbox.setStyleSheet(f"""
                QCheckBox {{
                    padding: 4px 8px;
                    background-color: {panel_bg};
                    color: {panel_fg};
                    font-family: Roboto;
                    font-size: 13px;
                }}
                QCheckBox::indicator {{
                    width: 14px;
                    height: 14px;
                    background-color: white;
                    border: 1px solid #555555;
                    border-radius: 2px;
                }}
                QCheckBox::indicator:checked {{
                    background-color: {checked_color};
                    border: 1px solid {checked_color};
                    border-radius: 2px;
                }}
            """)
            checkbox.stateChanged.connect(
                lambda state, g=name: self._on_toggle_group_filter(g, state == Qt.Checked)
            )
            widget_action = QtWidgets.QWidgetAction(self)
            widget_action.setDefaultWidget(checkbox)
            self.filter_menu.insertAction(self.show_every_group_action, widget_action)
            self.filter_group_actions[name] = widget_action

    def _on_js8_connectors(self) -> None:
        """Open JS8 Connectors management window."""
        Cls = self._resolve_dialog_class("js8_connectors", "JS8ConnectorsDialog")
        dialog = Cls(self.connector_manager, self.tcp_pool, self)
        dialog.exec_()

    def _handle_connection_changed(self, rig_name: str, is_connected: bool) -> None:
        """
        Handle TCP connection status changes.

        Args:
            rig_name: Name of the rig.
            is_connected: True if connected, False if disconnected.
        """
        if not is_connected:
            # For disconnects, add the message here
            from datetime import datetime, timezone
            utc_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            status_line = f"{utc_str}\t[{rig_name}] Disconnected"
            self.feed_messages.insert(0, status_line)
            self._update_feed_display()
            # Clear status logged flag so it will log again on reconnect
            self.rig_status_logged.discard(rig_name)
        self._update_connected_rigs_display()

    def _handle_callsign_received(self, rig_name: str, callsign: str) -> None:
        """
        Handle callsign received from JS8Call.

        Args:
            rig_name: Name of the rig.
            callsign: Callsign configured in JS8Call.
        """
        if callsign:
            self.rig_callsigns[rig_name] = callsign
            # Callsign is printed later after frequency is received

    def get_callsign_for_rig(self, rig_name: str) -> str:
        """
        Get cached callsign for a rig.

        Args:
            rig_name: Name of the rig.

        Returns:
            Callsign string or empty string if not known.
        """
        return self.rig_callsigns.get(rig_name, "")

    def _handle_grid_received(self, rig_name: str, grid: str) -> None:
        """
        Handle grid received from JS8Call.

        Prints combined rig status line with all collected info.

        Args:
            rig_name: Name of the rig.
            grid: Maidenhead grid square from JS8Call.
        """
        from statrep import get_state_from_connector

        if grid:
            self.rig_grids[rig_name] = grid

            # Get state from connector table
            state = get_state_from_connector(self.connector_manager, rig_name)
            if state:
                self.rig_states[rig_name] = state

            # Get cached values from the TCP client
            client = self.tcp_pool.clients.get(rig_name)
            if client:
                # Only log the status message once per connection
                if rig_name not in self.rig_status_logged:
                    speed_name = client.speed_name or "UNKNOWN"
                    callsign = client.callsign or "UNKNOWN"
                    frequency = client.frequency

                    # Format: [IC-7300] Running in TURBO mode, N0DDK, EM83CV, GA on 7.110
                    status_line = f"[{rig_name}] Running in {speed_name} mode, {callsign}, {grid}, {state or 'XX'} on {frequency:.3f}"
                    print(status_line)
                    self._handle_status_message(rig_name, status_line)
                    self.rig_status_logged.add(rig_name)

    def _handle_status_message(self, rig_name: str, message: str) -> None:
        """Handle status message from TCP client (for live feed display)."""
        utc_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._add_to_feed(f"{utc_str}\t{message}", rig_name)

    def _capture_contacts(
        self,
        from_call: str,
        local_snr: int,
        value: str,
        freq_hz: float,
        offset_hz: float,
    ) -> None:
        """
        Snapshot a directed message into the contacts roster (Direct Message Part 1).

        The sender (`from_call`) is the *relay* — the station we directly heard.
        Any callsign parsed out of the body is the *target* — the station the
        relay reported hearing.

        Primary path: body matches '<TARGET_CS> SNR|HEARTBEAT SNR <SIGNED_NUMBER>'.
        Writes Entry 1 (target as heard via relay) and Entry 2 (relay self-presence).

        Hearing path: body matches '<ADDRESSEE_CS> HEARING <CS1> <CS2> ...'.
        Writes one row per listed callsign with relay_cs=from_call and
        target_cs=listed, plus a self-presence row. SNR is unknown for both
        ends of every heard link, so a fixed placeholder
        (_CONTACTS_HEARING_DEFAULT_SNR) is written in both SNR columns.

        Fallback path: any other RX.DIRECTED body (group posts, relayed
        messages, free-form text). Writes a single self-presence row using the
        relay as both relay_cs and target_cs and our local SNR observation in
        both SNR columns, so the roster still reflects who's on the air.

        Args:
            from_call: Sender callsign from params['FROM'] — the relay.
            local_snr: Our SNR reading of the sender (params['SNR']) — relay SNR.
            value:     Message body (params['value']).
            freq_hz:   JS8Call frequency in Hz (dial + offset).
            offset_hz: Audio offset in Hz.
        """
        relay_cs = _strip_cs_suffix(from_call)
        if not _CONTACTS_BASE_CS_PATTERN.match(relay_cs):
            return
        freq_mhz = round(hz_to_mhz(freq_hz, offset_hz), 3)
        value = re.sub(rf'^(?:{re.escape(relay_cs)}\s*:\s*)+', '', value, flags=re.IGNORECASE)
        parsed = parse_contacts_observation(value)
        if parsed is not None:
            target_cs, target_snr = parsed
            self.db.upsert_contacts_pair(relay_cs, int(local_snr), target_cs, target_snr, freq_mhz)
            return
        heard = parse_hearing_observation(value)
        if heard is not None:
            self.db.upsert_contacts_hearing(relay_cs, heard, freq_mhz)
            return
        self.db.upsert_contact_self(relay_cs, int(local_snr), freq_mhz)

    def _purge_old_contacts(self) -> None:
        """Drop contacts rows older than CONTACTS_RETENTION_HOURS. Runs hourly."""
        removed = self.db.purge_old_contacts(CONTACTS_RETENTION_HOURS)
        if removed:
            print(f"[contacts] purged {removed} row(s) older than {CONTACTS_RETENTION_HOURS}h")

    def _handle_tcp_message(self, rig_name: str, message: dict) -> None:
        """
        Handle incoming TCP message from JS8Call.

        Args:
            rig_name: Name of the rig that received the message.
            message: Parsed JSON message from JS8Call.
        """
        from datetime import datetime, timezone

        msg_type = message.get("type", "")
        value = message.get("value", "")
        params = message.get("params", {})

        # JS8Call appends a non-ASCII terminator (e.g. ♦) at end of every value.
        # A non-ASCII char anywhere else usually means a corrupt/garbled decode —
        # surface it so the operator can see the frame was mangled.
        _v_stripped = value.rstrip() if value else ""
        if _v_stripped and any(ord(c) > 127 for c in _v_stripped[:-1]):
            print(f"{ConsoleColors.WARNING}[{rig_name}] Malformed Data — non-ASCII character in payload: {value!r}{ConsoleColors.RESET}")

        # Handle RX.DIRECTED messages
        if msg_type == "RX.DIRECTED":
            from_call = params.get("FROM", "")
            to_call = params.get("TO", "")
            grid = params.get("GRID", "")
            freq = params.get("FREQ", 0)
            offset = params.get("OFFSET", 0)
            snr = params.get("SNR", 0)
            utc_ms = params.get("UTC", 0)

            # Convert UTC milliseconds to datetime strings
            utc_dt = datetime.fromtimestamp(utc_ms / 1000, tz=timezone.utc)
            utc_db = utc_dt.strftime("%Y-%m-%d %H:%M:%S")  # Single space for database
            utc_display = utc_dt.strftime("%Y-%m-%d %H:%M:%S")

            # Format feed line to match DIRECTED.TXT format:
            # DATETIME    FREQ_MHZ    OFFSET    SNR    CALLSIGN: MESSAGE
            # FREQ from JS8Call is dial + offset, so subtract offset to get dial frequency
            dial_freq_mhz = hz_to_mhz(freq, offset)
            feed_line = f"{utc_display}\t{dial_freq_mhz:.3f}\t{offset}\t{snr:+03d}\t{from_call}: {value}"

            # Add to feed buffer (newest first)
            self._add_to_feed(feed_line, rig_name)

            print(f"[{rig_name}] {feed_line}")

            # Process the message for database insertion
            # Use dial frequency (freq - offset) for database storage
            dial_freq = freq - offset if freq else 0

            # --- Relay message detection ---
            # Handles JS8Call relay protocol: RELAY: USER_CALL> CONTENT *DE* SENDER
            import re as _re_relay
            _clean = self._preprocess_message_value(value, from_call)
            _user_call = self.get_callsign_for_rig(rig_name)

            if _user_call:
                # Pattern A: USER_CALL> ACK *DE* RECIPIENT
                _ack = _re_relay.match(
                    r'^(\w+)>\s+ACK\s+\*DE\*\s+(\w+)', _clean, _re_relay.IGNORECASE
                )
                if _ack and _ack.group(1).upper() == _user_call.upper():
                    _recipient = _ack.group(2).upper()
                    QtWidgets.QMessageBox.information(
                        self,
                        "Message Delivered",
                        f"Your message to {_recipient} was delivered successfully."
                    )
                    return  # fully handled

                # Pattern B: USER_CALL> CONTENT *DE* SENDER (not ACK)
                _relay = _re_relay.match(
                    r'^(\w+)>\s+(?!ACK\b)(.+?)\s+\*DE\*\s+(\w+)\s*$', _clean, _re_relay.IGNORECASE
                )
                if _relay and _relay.group(1).upper() == _user_call.upper():
                    _actual_sender = _relay.group(3)
                    _content = f"{_relay.group(2).strip()} - Relayed by: {from_call}"
                    _data_type = self._process_relay_message(
                        rig_name, _actual_sender, _content, _user_call, dial_freq, snr, utc_db
                    )
                    if _data_type == "message":
                        self._load_message_data()
                    return  # fully handled
            # --- End relay detection ---

            # Direct Message Part 1: snapshot SNR-style observations into contacts.
            # Runs after relay handlers (which return early) so relayed traffic
            # is not double-counted as a direct observation.
            self._capture_contacts(from_call, snr, value, freq, offset)

            data_type = self._process_directed_message(
                rig_name, value, from_call, to_call, grid, dial_freq, snr, utc_db
            )

            # Refresh only the relevant UI component
            if data_type == "statrep":
                self._load_statrep_data()
                if not self.config.get_show_alerts():
                    self._save_map_position(callback=self._load_map)
            elif data_type == "message":
                self._load_message_data()
            elif data_type == "alert":
                self._trigger_show_alerts()

        # Handle RX.ACTIVITY messages (band activity for live feed)
        elif msg_type == "RX.ACTIVITY":
            from_call = params.get("FROM", "")
            freq = params.get("FREQ", 0)
            offset = params.get("OFFSET", 0)
            snr = params.get("SNR", 0)
            utc_ms = params.get("UTC", 0)

            if value and from_call:
                utc_dt = datetime.fromtimestamp(utc_ms / 1000, tz=timezone.utc)
                utc_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
                dial_freq_mhz = hz_to_mhz(freq, offset)
                feed_line = f"{utc_str}\t{dial_freq_mhz:.3f}\t{offset}\t{snr:+03d}\t{from_call}: {value}"
                self._add_to_feed(feed_line, rig_name)

                # Direct Message Part 1: most "SENDER: RELAY SNR -nn" traffic
                # arrives here (RX.ACTIVITY) rather than RX.DIRECTED, because the
                # local JS8Call is usually not subscribed to the conversation.
                self._capture_contacts(from_call, snr, value, freq, offset)

                # Also try to process as a directed message for database storage.
                # JS8Call users send "CALLSIGN: @GROUP MSG text♦" without CommStat's
                # ",msg_id,{^%}" suffix. These arrive as RX.ACTIVITY when the local
                # JS8Call is not subscribed to the target group. The msg_id is
                # generated from the timestamp inside _parse_message().
                # Relay messages like "K7RIE: N0DDK @MAGNET MSG..." (no colon after
                # the relayed callsign) are excluded — only "CALLSIGN: @GROUP MSG..."
                # matches. Duplicate callsigns (JS8Call bug "K7RIE: K7RIE: @MAGNET")
                # are stripped before the check so they still match correctly.
                import re as _re
                _check_value = strip_duplicate_callsign(value, from_call)
                _check_value = _re.sub(r'[^ -~]', '', _check_value).strip()
                _activity_match = _re.match(
                    r'^(?:\w+:\s+)?(@\w+)\s+MSG\s+', _check_value, _re.IGNORECASE
                )
                if _activity_match:
                    to_call = _activity_match.group(1)
                    utc_db = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
                    dial_freq = freq - offset if freq else 0
                    data_type = self._process_directed_message(
                        rig_name, value, from_call, to_call, "", dial_freq, snr, utc_db
                    )
                    if data_type == "message":
                        self._load_message_data()
                elif "{&%}" in _check_value or "{F%}" in _check_value:
                    # STATREP arriving via RX.ACTIVITY — extract target from message body
                    # e.g. "K7RIE: N0DDK  ,CN96OU,..." or "K7RIE: @MAGNET ,CN96OU,..."
                    _sr_match = _re.match(
                        r'^(?:\w+:\s+)?(@?\w+)\s*,', _check_value, _re.IGNORECASE
                    )
                    if _sr_match:
                        _sr_to_call = _sr_match.group(1)
                        utc_db = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
                        dial_freq = freq - offset if freq else 0
                        data_type = self._process_directed_message(
                            rig_name, value, from_call, _sr_to_call, "", dial_freq, snr, utc_db
                        )
                        if data_type == "statrep":
                            self._load_statrep_data()
                            if not self.config.get_show_alerts():
                                self._save_map_position(callback=self._load_map)

    def _add_to_feed(self, line: str, rig_name: str) -> None:
        """
        Add a message line to the live feed buffer.

        Args:
            line: Formatted message line.
            rig_name: Name of the rig (unused, frequency identifies rig).
        """
        # Insert at beginning (newest first)
        self.feed_messages.insert(0, line)

        # Trim buffer if too large
        if len(self.feed_messages) > self.max_feed_messages:
            self.feed_messages = self.feed_messages[:self.max_feed_messages]

        # Update display
        self._update_feed_display()

    def _preprocess_message_value(self, value: str, from_call: str) -> str:
        """
        Preprocess message value before parsing.

        Applies:
        1. Duplicate callsign removal (JS8Call bug fix)
        2. Slash suffix stripping from message callsign

        Args:
            value: Raw message text
            from_call: Sender callsign (may include suffix)

        Returns:
            Cleaned message value
        """
        import re

        # Strip duplicate callsign (JS8Call bug: "W8APP: W8APP: @GROUP" → "W8APP: @GROUP")
        value = strip_duplicate_callsign(value, from_call)

        # Strip slash suffix from callsign in message (W3BFO/P: → W3BFO:)
        value = re.sub(r'^(\w+)/\w+:', r'\1:', value)

        # Strip non-ASCII characters (e.g., JS8Call EOL diamond ♦) so the
        # commsrvr regex can correctly match and discard the {^%} terminator
        value = re.sub(r'[^ -~]', '', value).strip()

        return value

    def _parse_standard_statrep(
        self,
        rig_name: str,
        message_value: str,
        from_callsign: str,
        target: str,
        grid: str,
        freq: int,
        snr: int,
        utc: str,
        source: int,
        global_id: int = 0
    ) -> tuple:
        """
        Parse standard STATREP message format.

        Format: ,GRID,PREC,SRID,SRCODE,COMMENTS,{&%}
        Forwarded: ,GRID,PREC,SRID,SRCODE,COMMENTS,ORIG_CALL,{F%}

        Args:
            rig_name: Name of the rig/source
            message_value: Message text
            from_callsign: Sender callsign (base callsign without suffix)
            target: Target @GROUP or callsign
            grid: Grid square from TCP params or empty for commsrvr
            freq: Frequency in Hz
            snr: Signal-to-noise ratio in dB
            utc: UTC timestamp string "YYYY-MM-DD HH:MM:SS"
            source: 1=Radio (TCP), 2=Internet (commsrvr)

        Returns:
            (message_type, None) where message_type is "statrep" or ""
        """
        import re

        is_forwarded = "{F%}" in message_value
        marker = "{F%}" if is_forwarded else "{&%}"

        # Extract statrep data before marker
        match = re.search(r',(.+?)' + re.escape(marker), message_value)
        if not match:
            return ("", None)

        fields = match.group(1).split(",")

        # Need at least 4 fields: GRID, PREC, SRID, SRCODE
        if len(fields) < 4:
            return ("", None)

        statrep_grid = fields[0].strip()
        prec_num = fields[1].strip()
        sr_id = fields[2].strip()
        srcode = fields[3].strip()

        # Expand "+" shorthand
        srcode = expand_plus_shorthand(srcode)

        # Validate SRCODE: must be at least 12 numeric digits
        if len(srcode) < 12 or not srcode[:12].isdigit():
            print(f"{ConsoleColors.WARNING}[{rig_name}] WARNING: Invalid STATREP SRCODE from {from_callsign} - must be 12 numeric digits, got: {srcode}{ConsoleColors.RESET}")
            return ("", None)

        # Validate and get grid (use QRZ if invalid/missing)
        statrep_grid = self._resolve_grid(rig_name, statrep_grid, from_callsign, grid, "STATREP")

        # Comments — same parsing for both standard and forwarded
        # ("Forwarded By:" is already embedded in the remarks text by the sender)
        comments_raw = ",".join([f for f in fields[4:] if f.strip()]).strip() if len(fields) > 4 else ""
        comments = sanitize_ascii(comments_raw)

        # Map scope
        SCOPE_MAP = {
            "1": "My Location",
            "2": "My Community",
            "3": "My County",
            "4": "My Region",
            "5": "Other Location"
        }
        scope = SCOPE_MAP.get(prec_num, "Unknown")

        # Insert statrep
        sr_fields = list(srcode[:12])  # Use only first 12 digits
        date_only, _ = parse_message_datetime(utc)

        # Commsrvr duplicate detection: if we already have this record, only update global_id
        if source == 2:
            try:
                with sqlite3.connect(DATABASE_FILE, timeout=10) as _conn:
                    _existing = _conn.execute(
                        "SELECT id, global_id FROM statrep WHERE date = ? AND from_callsign = ? AND sr_id = ?",
                        (date_only, from_callsign, sr_id)
                    ).fetchone()
                    if _existing:
                        _ex_id, _ex_gid = _existing
                        if global_id and not _ex_gid:
                            _conn.execute(
                                "UPDATE statrep SET global_id = ? WHERE id = ?",
                                (global_id, _ex_id)
                            )
                            _conn.commit()
            except sqlite3.Error:
                _existing = None
            if _existing:
                print(f"[{rig_name}] Skipping duplicate STATREP {sr_id} from {from_callsign} — already received (Global ID: {global_id})")
                return ("", None)

        # Build data dict for insertion
        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': source,
            'sr_id': sr_id,
            'from_callsign': from_callsign,
            'target': target,
            'grid': statrep_grid,
            'scope': scope,
            'map': sr_fields[0],
            'power': sr_fields[1],
            'water': sr_fields[2],
            'med': sr_fields[3],
            'telecom': sr_fields[4],
            'travel': sr_fields[5],
            'internet': sr_fields[6],
            'fuel': sr_fields[7],
            'food': sr_fields[8],
            'crime': sr_fields[9],
            'civil': sr_fields[10],
            'political': sr_fields[11],
            'comments': comments,
            'global_id': global_id
        }

        fwd_marker = " (FORWARDED)" if is_forwarded else ""
        result = self._insert_message_data(
            rig_name, "statrep", data, "sr_id", "statrep", from_callsign, fwd_marker
        )
        if result:
            return (result, None)

        return ("", None)

    def _parse_alert(
        self,
        rig_name: str,
        message_value: str,
        from_callsign: str,
        target: str,
        freq: int,
        snr: int,
        utc: str,
        source: int
    ) -> tuple:
        """
        Parse ALERT message format.

        New format: @GROUP ,ALERT_ID,COLOR,TITLE,MESSAGE,{%%}
        Old format: @GROUP ,COLOR,TITLE,MESSAGE,{%%}
        Legacy commsrvr format: LRT ,COLOR,TITLE,MESSAGE,{%%}

        Args:
            rig_name: Name of the rig/source
            message_value: Message text
            from_callsign: Sender callsign (base callsign without suffix)
            target: Target @GROUP or callsign
            freq: Frequency in Hz
            snr: Signal-to-noise ratio in dB
            utc: UTC timestamp string "YYYY-MM-DD HH:MM:SS"
            source: 1=Radio (TCP), 2=Internet (commsrvr)

        Returns:
            (message_type, None) where message_type is "alert" or ""
        """
        import re

        # Try standard @GROUP pattern first
        match = re.search(r'(@\w+)\s*,(.+?)\{\%\%\}', message_value)
        if match:
            alert_target = match.group(1).strip()
            fields_str = match.group(2).strip()
        else:
            # Try LRT pattern (legacy commsrvr format)
            match = re.search(r'LRT\s*,(.+?)\{\%\%\}', message_value)
            if match:
                alert_target = target if target else "@ALL"
                fields_str = match.group(1).strip()
            else:
                return ("", None)

        # Split fields (max 3 splits to preserve commas in message)
        fields = fields_str.split(",", 3)

        # Determine if we have the new format (with alert_id) or old format
        if len(fields) >= 4:
            # New format: ALERT_ID, COLOR, TITLE, MESSAGE
            alert_id = fields[0].strip()
            try:
                alert_color = int(fields[1].strip())
            except ValueError:
                print(f"{ConsoleColors.WARNING}[{rig_name}] WARNING: Invalid alert color in message from {from_callsign}{ConsoleColors.RESET}")
                return ("", None)
            alert_title = sanitize_ascii(fields[2].strip())
            alert_message = sanitize_ascii(fields[3].strip())
            # Extract date for new format
            date_only, _ = parse_message_datetime(utc)
        elif len(fields) >= 3:
            # Old format: COLOR, TITLE, MESSAGE (no alert_id, generate one)
            try:
                alert_color = int(fields[0].strip())
            except ValueError:
                print(f"{ConsoleColors.WARNING}[{rig_name}] WARNING: Invalid alert color in message from {from_callsign}{ConsoleColors.RESET}")
                return ("", None)
            alert_title = sanitize_ascii(fields[1].strip())
            alert_message = sanitize_ascii(fields[2].strip())
            # Generate time-based alert ID for old format
            date_only, alert_id = parse_message_datetime(utc)
        else:
            return ("", None)

        # Filter alerts by target
        if not alert_target.startswith("@"):
            # No @ prefix — target is a callsign; accept if it matches any known callsign
            user_callsigns = [c.upper() for c in self.rig_callsigns.values() if c]
            if not user_callsigns:
                local_callsign, _, __ = self.db.get_user_settings()
                if local_callsign:
                    user_callsigns = [local_callsign.upper()]
            if alert_target.upper() not in user_callsigns:
                return ("", None)
        else:
            # @GROUP — only save if we're a member of that group (active or not),
            # unless "Save all Alerts" is enabled, which imports every group alert.
            if not self.config.get_save_all_alerts():
                group_name = alert_target[1:].upper()
                all_groups = self.db.get_all_groups()
                if group_name not in all_groups:
                    return ("", None)

        # Build data dict for insertion
        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': source,
            'alert_id': alert_id,
            'from_callsign': from_callsign,
            'target': alert_target,
            'color': alert_color,
            'title': alert_title,
            'message': alert_message
        }

        result = self._insert_message_data(
            rig_name, "alerts", data, "alert_id", "alert", from_callsign
        )
        if result:
            return (result, None)

        return ("", None)

    def _parse_message(
        self,
        rig_name: str,
        message_value: str,
        from_callsign: str,
        target: str,
        freq: int,
        snr: int,
        utc: str,
        source: int
    ) -> tuple:
        """
        Parse MESSAGE format.

        TCP format: CALLSIGN: TARGET MSG message_text
        Commsrvr format: @GROUP MSG ,MSG_ID,MESSAGE_TEXT,{^%}

        Args:
            rig_name: Name of the rig/source
            message_value: Message text
            from_callsign: Sender callsign (base callsign without suffix)
            target: Target @GROUP or callsign
            freq: Frequency in Hz
            snr: Signal-to-noise ratio in dB
            utc: UTC timestamp string "YYYY-MM-DD HH:MM:SS"
            source: 1=Radio (TCP), 2=Internet (commsrvr)

        Returns:
            (message_type, None) where message_type is "message" or ""
        """
        import re

        msg_id = None
        msg_target = target
        message_text = None

        # Try to parse commsrvr format: [SENDER: ]@GROUP MSG ,MSG_ID,MESSAGE[,{^%}]
        # Callsign prefix is optional — new JS8Call omits it (sender is in from_callsign param).
        # Old JS8Call includes it; strip_duplicate_callsign reduces any double prefix first.
        commsrvr_pattern = re.match(r'^(?:\w+:\s+)?(@?\w+)\s+MSG\s+,([^,]+),(.+?)(?:\s*,\{\^%\})?$', message_value, re.IGNORECASE)
        if commsrvr_pattern:
            msg_target = commsrvr_pattern.group(1).strip()
            msg_id = commsrvr_pattern.group(2).strip()
            message_text = commsrvr_pattern.group(3).strip()
        else:
            # Try TCP MSG pattern: [CALLSIGN: ]TARGET MSG message_text
            tcp_pattern = re.match(r'^(?:\w+:\s+)?(@?\w+)\s+MSG\s+(.+)$', message_value, re.IGNORECASE)
            if tcp_pattern:
                msg_target = tcp_pattern.group(1).strip()
                message_text = tcp_pattern.group(2).strip()
            elif source == 2:
                # Commsrvr fallback: accept raw message (for older formats)
                message_text = message_value
                msg_target = target if target else ""
            else:
                # TCP: require MSG keyword
                return ("", None)

        # Skip if message is empty
        if not message_text:
            return ("", None)

        # Clean up message text
        message_text = message_text.strip()

        # Extract date and generate msg_id if not extracted from message
        date_only, generated_msg_id = parse_message_datetime(utc)
        if not msg_id:
            msg_id = generated_msg_id

        # Check if message is to a group we're in or to one of our callsigns
        if msg_target.startswith("@"):
            # Group message - only save if we're a member of that group, unless
            # "Save all Messages" is enabled, which imports every group message.
            if not self.config.get_save_all_messages():
                group_name = msg_target[1:].upper()  # Remove @ and normalize
                all_groups = self.db.get_all_groups()
                if group_name not in all_groups:
                    # Skip messages to groups we're not in
                    return ("", None)
        else:
            # Direct message - only save if to one of our callsigns
            target_call = msg_target.upper()
            user_callsigns = [c.upper() for c in self.rig_callsigns.values() if c]
            if not user_callsigns:
                # No JS8 connectors active — fall back to user settings callsign
                settings_callsign, _, __ = self.db.get_user_settings()
                if settings_callsign:
                    user_callsigns = [settings_callsign.upper()]
            if target_call not in user_callsigns:
                # Skip messages not to our callsigns
                return ("", None)

        # Build data dict for insertion
        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': source,
            'msg_id': msg_id,
            'from_callsign': from_callsign,
            'target': msg_target,
            'message': message_text
        }

        result = self._insert_message_data(
            rig_name, "messages", data, "msg_id", "message", from_callsign
        )
        if result:
            return (result, None)

        return ("", None)

    def _process_relay_message(
        self,
        rig_name: str,
        actual_sender: str,
        content: str,
        target: str,
        freq: int,
        snr: int,
        utc: str
    ) -> str:
        """
        Process a relay-forwarded message addressed to the local user.

        Format: RELAY: USER_CALL> CONTENT *DE* ORIGINAL_SENDER

        Args:
            rig_name: Name of the rig.
            actual_sender: Callsign after *DE* (the original message author).
            content: Message text between '>' and '*DE*'.
            target: User's local callsign (the relay destination).
            freq: Frequency in Hz.
            snr: Signal-to-noise ratio.
            utc: UTC timestamp "YYYY-MM-DD HH:MM:SS".

        Returns:
            "message" on successful insert, "" otherwise.
        """
        from id_utils import parse_message_datetime

        actual_sender = actual_sender.split("/")[0].upper()
        content = content.strip()
        if not content or not actual_sender:
            return ""

        date_only, msg_id = parse_message_datetime(utc)

        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': 1,
            'msg_id': msg_id,
            'from_callsign': actual_sender,
            'target': target,
            'message': content
        }

        result = self._insert_message_data(
            rig_name, "messages", data, "msg_id", "message", actual_sender
        )
        return result if result else ""

    def _parse_commstat_message(
        self,
        rig_name: str,
        from_callsign: str,
        message_value: str,
        target: str,
        grid: str,
        freq: int,
        snr: int,
        utc: str,
        source: int,  # 1=Radio, 2=Internet
        global_id: int = 0
    ) -> tuple:
        """
        Parse and validate CommStat message in any format.

        Processes messages in priority order:
        1. Standard STATREP ({&%} or {F%})
        2. F!304 STATREP (8-digit format)
        3. F!301 STATREP (9-digit format)
        4. ALERT ({%%})
        5. MESSAGE (contains "MSG" keyword)

        Args:
            rig_name: Name of the rig/source
            from_callsign: Sender callsign (base callsign without suffix)
            message_value: Message text (already preprocessed)
            target: Target @GROUP or callsign
            grid: Grid square from TCP params or empty for commsrvr
            freq: Frequency in Hz
            snr: Signal-to-noise ratio in dB
            utc: UTC timestamp string "YYYY-MM-DD HH:MM:SS"
            source: 1=Radio (TCP), 2=Internet (commsrvr)

        Returns:
            (message_type, data_dict) where:
            - message_type: "statrep", "alert", "message", or "" (invalid/skip)
            - data_dict: None (already inserted by sub-parsers)
        """
        # Validate inputs
        if not from_callsign or not message_value:
            return ("", None)

        # Extract base callsign (remove /P, /M suffixes)
        from_callsign = from_callsign.split("/")[0]

        # Detect Internet-Only markers and normalize
        if "{&%3}" in message_value or "{%%3}" in message_value or "{^%3}" in message_value:
            source = 3
            message_value = (message_value
                .replace("{&%3}", "{&%}")
                .replace("{%%3}", "{%%}")
                .replace("{^%3}", "{^%}"))

        # PRIORITY 1: Standard STATREP ({&%} or {F%})
        if "{&%}" in message_value or "{F%}" in message_value:
            return self._parse_standard_statrep(
                rig_name, message_value, from_callsign, target, grid, freq, snr, utc, source, global_id
            )

        # PRIORITY 2: F!304 STATREP
        if "F!304" in message_value:
            result = self._process_fcode_statrep(
                rig_name, message_value, from_callsign, target, grid, freq, snr, utc, "F!304", source, global_id
            )
            if result:
                return (result, None)

        # PRIORITY 3: F!301 STATREP
        if "F!301" in message_value:
            result = self._process_fcode_statrep(
                rig_name, message_value, from_callsign, target, grid, freq, snr, utc, "F!301", source, global_id
            )
            if result:
                return (result, None)

        # PRIORITY 4: ALERT ({%%})
        if "{%%}" in message_value:
            return self._parse_alert(
                rig_name, message_value, from_callsign, target, freq, snr, utc, source
            )

        # PRIORITY 5: MESSAGE
        return self._parse_message(
            rig_name, message_value, from_callsign, target, freq, snr, utc, source
        )

    def _process_directed_message(
        self,
        rig_name: str,
        value: str,
        from_call: str,
        to_call: str,
        grid: str,
        freq: int,
        snr: int,
        utc: str
    ) -> str:
        """
        Process a directed message received via TCP from JS8Call.

        SIMPLIFIED: Only processes messages containing " MSG "
        - Process ALL messages to groups (to_call starts with @)
        - Process messages to user's callsign only

        Args:
            rig_name: Name of the rig that received the message.
            value: The message text content.
            from_call: Sender callsign (from TCP connection).
            to_call: Recipient callsign or @GROUP (from TCP connection).
            grid: Sender's grid square.
            freq: Frequency in Hz.
            snr: Signal-to-noise ratio.
            utc: UTC timestamp string.

        Returns:
            "statrep", "message", "alert", or empty string
        """
        # Preprocess message value
        value = self._preprocess_message_value(value, from_call)

        # Extract base callsign
        from_callsign = from_call.split("/")[0] if from_call else ""

        # Extract target group
        target = ""
        if to_call.startswith("@"):
            target = to_call

        # Determine if message is relevant (to group or to our callsign)
        user_callsign = self.get_callsign_for_rig(rig_name)
        if not user_callsign:
            user_callsign, _, __ = self.db.get_user_settings()
        is_to_user = to_call.split("/")[0].upper() == user_callsign.upper() if user_callsign else False

        # Group check: groups accepted only if in our groups list
        if to_call.startswith("@"):
            group_name = to_call[1:].upper()
            is_to_group = group_name in self.db.get_all_groups()
        else:
            is_to_group = False

        # Only process if to our group OR to our callsign
        if not (is_to_group or is_to_user):
            return ""

        # For direct-callsign messages, store the recipient callsign as target
        if is_to_user and not target:
            target = to_call.split("/")[0].upper()

        # Parse using unified parser (source=1 for Radio)
        msg_type, _ = self._parse_commstat_message(
            rig_name, from_callsign, value, target, grid, freq, snr, utc, source=1
        )

        return msg_type

    def _on_qrz_enable(self) -> None:
        """Open QRZ Settings dialog."""
        Cls = self._resolve_dialog_class("qrz_settings", "QRZSettingsDialog")
        dlg = Cls(self.db, parent=self)
        dlg.exec_()

    def _on_user_settings(self) -> None:
        """Open User Settings dialog."""
        Cls = self._resolve_dialog_class("user_settings", "UserSettingsDialog")
        dlg = Cls(self.db, parent=self)
        dlg.exec_()

    @QtCore.pyqtSlot(str)
    def _queue_notification_sound(self, msg_type: str) -> None:
        """Accumulate sound type and (re)start debounce timer."""
        self._pending_sound_types.add(msg_type)
        self._sound_debounce_timer.start(self._SOUND_DEBOUNCE_MS)

    def _play_pending_sound(self) -> None:
        """Play the highest-priority enabled pending sound, then clear the queue."""
        for msg_type in ("alert", "message", "statrep"):
            if msg_type in self._pending_sound_types:
                # play() no-ops if this event's sound is disabled, so keep
                # scanning so a disabled high-priority type can't mute an
                # enabled lower-priority one.
                if self.config.get_sound_enabled(msg_type):
                    self._sound_player.play(msg_type)
                    break
        self._pending_sound_types.clear()

    def _on_sound_settings(self) -> None:
        """Open Sound Settings dialog (per-event sound file + enable)."""
        Cls = self._resolve_dialog_class("sound_settings", "SoundSettingsDialog")
        dlg = Cls(self.config, self._sound_player, parent=self)
        dlg.exec_()

    def _show_image_dialog(
        self,
        title: str,
        image_url: str,
        link_html: str,
        loading_text: str,
        error_prefix: str
    ) -> None:
        """
        Display a dialog that fetches and shows an image from a URL.

        This helper method reduces code duplication for dialogs that display
        remote images (band conditions, solar flux, world map, etc.).

        Args:
            title: Window title for the dialog.
            image_url: URL of the image to fetch.
            link_html: HTML string for the attribution/link label.
            loading_text: Text to show while loading.
            error_prefix: Prefix for error message (e.g., "Failed to load band conditions").
        """
        panel_bg = DEFAULT_COLORS.get("module_background", "#DDDDDD")
        panel_fg = DEFAULT_COLORS.get("module_foreground", "#000000")

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumSize(480, 200)
        dialog.setWindowFlags(
            Qt.Window |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint
        )
        dialog.setStyleSheet(
            f"QDialog {{ background-color:{panel_bg}; color:{panel_fg}; }}"
            f"QLabel {{ font-size:13px; color:{panel_fg}; }}"
        )

        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        # Image label (shows loading text, then image or error)
        image_label = QtWidgets.QLabel(loading_text)
        image_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(image_label)

        # Attribution/link label
        link_label = QtWidgets.QLabel(link_html)
        link_label.setOpenExternalLinks(True)
        link_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(link_label)

        # Close button row (bottom right)
        from ui_helpers import make_button
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        close_btn = make_button("Close", "#555555", 80)
        close_btn.clicked.connect(dialog.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # Storage for fetched data (shared between threads)
        fetch_result = {'data': None, 'error': None}

        def fetch_image():
            """Background thread: fetch image from URL."""
            try:
                request = urllib.request.Request(
                    image_url,
                    headers={'User-Agent': 'CommStat/2.5'}
                )
                with urllib.request.urlopen(request, timeout=15, context=create_insecure_ssl_context()) as response:
                    fetch_result['data'] = response.read()
            except Exception as e:
                fetch_result['error'] = str(e)

        def update_ui():
            """Poll for fetch completion and update dialog."""
            if fetch_result['data']:
                pixmap = QtGui.QPixmap()
                pixmap.loadFromData(fetch_result['data'])
                image_label.setPixmap(pixmap)
                dialog.adjustSize()
            elif fetch_result['error']:
                image_label.setText(f"{error_prefix}: {fetch_result['error']}")
            else:
                # Still loading, check again in 100ms
                QTimer.singleShot(100, update_ui)

        # Start fetch in background thread
        thread = threading.Thread(target=fetch_image, daemon=True)
        thread.start()

        # Start polling for result
        QTimer.singleShot(100, update_ui)

        dialog.exec_()


# =============================================================================
# Application Entry Point
# =============================================================================

def main() -> None:
    """Application entry point."""
    # Replace commstat.py with commstat-copy.py if the copy exists
    _script_dir = Path(__file__).parent
    _copy = _script_dir / "commstat-copy.py"
    _launcher = _script_dir / "commstat.py"
    if _copy.exists():
        try:
            if _launcher.exists():
                _launcher.unlink()
            _copy.rename(_launcher)
            print("Launcher updated: commstat-copy.py → commstat.py")
        except Exception as e:
            print(f"Warning: Could not replace launcher: {e}")

    # Register tiles:// scheme before QApplication (Qt requirement)
    _tile_scheme = QWebEngineUrlScheme(b'tiles')
    _tile_scheme.setFlags(
        QWebEngineUrlScheme.SecureScheme |
        QWebEngineUrlScheme.LocalScheme |
        QWebEngineUrlScheme.CorsEnabled
    )
    QWebEngineUrlScheme.registerScheme(_tile_scheme)

    QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv)

    # Install tile scheme handler on the default profile
    _tile_handler = TileSchemeHandler("tilesPNG2")
    QWebEngineProfile.defaultProfile().installUrlSchemeHandler(b'tiles', _tile_handler)

    # Set tooltip colors to match Windows (tan background, black text)
    app.setStyleSheet("QToolTip { background-color: #FFFFE1; color: black; border: 1px solid black; }")

    # Load bundled fonts
    from PyQt5.QtGui import QFontDatabase
    import os

    font_dir = os.path.join(os.path.dirname(__file__), 'fonts')
    fonts_to_load = [
        'Roboto-Regular.ttf',
        'Roboto-Bold.ttf',
        'RobotoSlab-Regular.ttf',
        'RobotoSlab-Bold.ttf',
        'RobotoSlab-Black.ttf',
        'KodeMono-Regular.ttf',
        'KodeMono-Medium.ttf',
        'KodeMono-Bold.ttf',
    ]

    for font_file in fonts_to_load:
        font_path = os.path.join(font_dir, font_file)
        if os.path.exists(font_path):
            font_id = QFontDatabase.addApplicationFont(font_path)
            if font_id == -1:
                print(f"Warning: Failed to load font {font_file}")
            else:
                families = QFontDatabase.applicationFontFamilies(font_id)
                print(f"Loaded font: {font_file} -> {families}")
        else:
            print(f"Warning: Font file not found: {font_path}")

    # Load configuration
    config = ConfigManager()

    db = DatabaseManager()

    # Create and show main window
    window = MainWindow(config, db)
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
