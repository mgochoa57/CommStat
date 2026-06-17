# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.

"""
CommStat UI and application constants.
Import with: from constants import *
"""

from typing import Dict

# =============================================================================
# Application Identity
# =============================================================================

VERSION = "4.3.a"

WINDOW_TITLE = f"CommStat (v{VERSION}) by N0DDK"
WINDOW_SIZE = (1360, 768)
CONFIG_FILE = "config.ini"
ICON_FILE = "radiation-32.png"
DATABASE_FILE = "traffic.db3"
SOUNDS_DIR = "sounds"

# =============================================================================
# Fonts
# =============================================================================

# Timing
HEARTBEAT_DELAY_MS  = 5000   # initial delay before first CommStat server heartbeat
RIG_FETCH_DELAY_MS  = 100    # staggered delay for grid/callsign requests after rig select
RIG_FREQ_DELAY_MS   = 200    # staggered delay for frequency request after rig select

FONT_ROBOTO   = "Roboto"
FONT_MONO     = "Kode Mono"

# CSS font-family stacks — use in stylesheets for graceful fallback if fonts
# are not installed (e.g. fresh Linux install without bundled .ttf files).
FONT_ROBOTO_STACK = "Roboto"
FONT_MONO_STACK   = "'Kode Mono'"

# =============================================================================
# UI Colors
# =============================================================================

# Input / form fields
COLOR_INPUT_TEXT    = "#333333"
COLOR_INPUT_BORDER  = "#cccccc"
COLOR_DISABLED_BG   = "#e9ecef"
COLOR_DISABLED_TEXT = "#999999"

# Semantic button colors
COLOR_BTN_RED   = "#dc3545"
COLOR_BTN_GREEN = "#28a745"
COLOR_BTN_BLUE  = "#007bff"
COLOR_BTN_CYAN  = "#17a2b8"
COLOR_BTN_GRAY  = "#6c757d"

# =============================================================================
# Default Color Scheme (used by ConfigManager / config.ini)
# =============================================================================

DEFAULT_COLORS: Dict[str, str] = {
    # Main window
    'program_background': '#A52A2A',       # Maroon
    'program_foreground': '#FFFFFF',
    'menu_background': '#3050CC',          # Blue
    'menu_foreground': '#FFFFFF',
    'title_bar_background': '#F07800',     # Orange
    'title_bar_foreground': '#FFFFFF',
    # News feed marquee
    'newsfeed_background': '#242424',      # Dark gray
    'newsfeed_foreground': '#00FF00',      # Green text
    # Clock display
    'time_background': '#282864',          # Navy blue
    'time_foreground': '#FFFF00',
    # StatRep condition indicators (traffic light)
    'condition_green': '#28A745',          # Good / normal
    #'condition_yellow': '#FFFF77',         # Caution / degraded
    'condition_yellow': '#FFDC78',         # Caution / degraded
    'condition_red': '#DC3534',            # Critical / emergency
    'condition_gray': '#6C757D',           # Unknown / no data
    # Data tables
    'data_background': '#F5EDD7',          # Cream was F8F6F4
    'data_foreground': '#000000',
    # Live feed display
    'feed_background': '#000000',
    'feed_foreground': '#FFFFFF',
    # Module / dialog background
    'module_background': '#E4E4E4',
    'module_foreground': '#242424',
}

# =============================================================================
# Filter / Map / Slideshow
# =============================================================================

DEFAULT_FILTER_START  = "2023-01-01"
MAX_GROUP_NAME_LENGTH = 8
MAP_WIDTH             = 604
MAP_HEIGHT            = 340
SLIDESHOW_INTERVAL    = 5   # minutes between image changes

# =============================================================================
# Timing
# =============================================================================

INTERNET_CHECK_INTERVAL    = 30 * 60 * 1000   # 30 minutes in ms
NEWSFEED_TYPE_INTERVAL_MS  = 60               # ms per character during type-on
NEWSFEED_PAUSE_MS          = 20000            # ms to hold when window is full
NEWSFEED_SCROLL_DURATION_MS = 1000            # total ms for scroll-off phase

# Contacts roster retention. Hourly cleanup deletes rows whose insert_date is
# older than this many hours, keeping the JS8 Direct Message target/relay
# pickers focused on recently-heard stations.
CONTACTS_RETENTION_HOURS   = 6

# =============================================================================
# StatRep Table Headers
# =============================================================================

STATREP_HEADERS = [
    "", "Date Time", "Freq", "From", "To", "ID", "Grid", "Scope", "Map",
    "Powr", "H2O", "Med", "Comm", "Trvl", "Inet", "Fuel", "Food",
    "Crime", "Civil", "Pol", "Remarks"
]

# =============================================================================
# Console Colors (ANSI)
# =============================================================================

class ConsoleColors:
    """ANSI color codes for console output."""
    SUCCESS = "\033[92m"   # Green
    WARNING = "\033[93m"   # Yellow
    ERROR   = "\033[91m"   # Red
    RESET   = "\033[0m"

# =============================================================================
# Default RSS News Feeds
# =============================================================================

DEFAULT_RSS_FEEDS: Dict[str, str] = {
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "AP News":    "https://news.google.com/rss/search?q=when:24h+site:apnews.com&hl=en-US&gl=US&ceid=US:en",
    "BBC World":  "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Fox News":   "https://moxie.foxnews.com/google-publisher/latest.xml",
    "NPR News":   "https://feeds.npr.org/1001/rss.xml",
    "Reuters":    "https://news.google.com/rss/search?q=when:24h+site:reuters.com&hl=en-US&gl=US&ceid=US:en",
}
