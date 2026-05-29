# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
qrz_client.py - QRZ.com XML API Client for CommStat

Provides callsign lookups via QRZ.com with local database caching
to minimize API calls.
"""

import sqlite3
import sys
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple
from pathlib import Path


# Constants
QRZ_API_URL = "https://xmldata.qrz.com/xml/current/"
CACHE_DAYS = 30  # How long to cache callsign data
DB_PATH = Path(__file__).parent / "traffic.db3"

def qrz_log(msg: str) -> None:
    """Always print a QRZ console log line."""
    print(f"[QRZ] {msg}")


def load_qrz_config() -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Load QRZ configuration from database.

    Returns:
        Tuple of (active, username, password)
    """
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT username, password, is_active FROM qrz_settings WHERE id = 1")
            result = cursor.fetchone()
            if result:
                username = result[0] or ""
                password = result[1] or ""
                is_active = bool(result[2])
                return is_active, username or None, password or None
            return False, None, None
    except sqlite3.Error:
        return False, None, None


def set_qrz_active(active: bool) -> bool:
    """
    Set the QRZ active flag in database.

    Args:
        active: True to enable, False to disable

    Returns:
        True if successful
    """
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE qrz_settings SET is_active = ? WHERE id = 1",
                (1 if active else 0,)
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error:
        return False


def get_qrz_cached(callsign: str, include_stale: bool = False) -> Optional[Dict]:
    """Return cached QRZ data for *callsign* without creating a full client.

    Returns a dict (same shape as QRZClient._get_cached) or None on miss.
    When include_stale=True, returns the record even if older than CACHE_DAYS
    (used to display existing data when subscription is inactive).
    """
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cs = max(callsign.upper().split('/'), key=len) if '/' in callsign else callsign.upper()
            cursor.execute("SELECT * FROM qrz WHERE callsign = ?", (cs,))
            row = cursor.fetchone()
            if row:
                cached_date = datetime.fromisoformat(row["insert_date"])
                if cached_date.tzinfo is None:
                    cached_date = cached_date.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - cached_date).days
                if age_days < CACHE_DAYS:
                    qrz_log(f"Cache hit for {cs} (age: {age_days} days)")
                    return dict(row)
                if include_stale:
                    qrz_log(f"Returning stale cache for {cs} (age: {age_days} days)")
                    return dict(row)
    except Exception:
        pass
    return None


class QRZClient:
    """
    QRZ.com XML API client with local caching.

    Caches callsign lookups in SQLite to reduce API calls.
    Session keys are reused until they expire.
    """

    def __init__(self, username: str = None, password: str = None):
        """
        Initialize QRZ client.

        Args:
            username: QRZ.com username
            password: QRZ.com password
        """
        self.username = username
        self.password = password
        self.session_key: Optional[str] = None

    @staticmethod
    def is_active() -> bool:
        """Check if QRZ lookups are enabled in config."""
        active, _, _ = load_qrz_config()
        return active

    def _get_cached(self, callsign: str) -> tuple:
        """
        Check cache for callsign data.

        Args:
            callsign: Callsign to look up

        Returns:
            (data_dict, is_fresh) where data_dict may be None if no row exists,
            and is_fresh is True when the cached entry is within CACHE_DAYS.
        """
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM qrz WHERE callsign = ?",
                    (callsign.upper(),)
                )
                row = cursor.fetchone()

                if row:
                    cached_date = datetime.fromisoformat(row["insert_date"])
                    if cached_date.tzinfo is None:
                        cached_date = cached_date.replace(tzinfo=timezone.utc)
                    age_days = (datetime.now(timezone.utc) - cached_date).days

                    if age_days < CACHE_DAYS:
                        return dict(row), True
                    else:
                        qrz_log(f"Cache expired for {callsign} (age: {age_days} days), refreshing from API")
                        return dict(row), False

                return None, False
        except sqlite3.Error:
            return None, False

    def _save_to_cache(self, data: Dict) -> None:
        """
        Save callsign data to cache.

        Args:
            data: Callsign data dict from QRZ XML API
        """
        # Compute active flag from expiration date
        expdate_str = data.get("expdate")
        if expdate_str:
            try:
                active = 1 if expdate_str >= datetime.now(timezone.utc).strftime("%Y-%m-%d") else 0
            except Exception:
                active = None
        else:
            active = None

        # Combine fname + name into a single full name, apply normalization
        fname     = (data.get("fname")   or "").strip()
        name      = (data.get("name")    or "").strip()
        full_name = " ".join(x for x in (fname, name) if x).title()
        address   = (data.get("addr1")   or "").strip().title()
        city      = (data.get("addr2")   or "").strip().title()   # QRZ uses addr2 for city
        county    = (data.get("county")  or "").strip().title()
        email     = (data.get("email")   or "").strip().lower()

        callsign = data.get("call", "").upper()
        values = (
            active, full_name, address, city, county,
            data.get("state"), data.get("zip"), data.get("country"),
            data.get("ccode"), data.get("lat"), data.get("lon"),
            data.get("grid"), data.get("fips"),
            data.get("efdate"),      # API sends efdate (one f), stored as effdate
            data.get("expdate"),
            data.get("class"),       # dict key access — "class" is valid here
            email, data.get("image"), data.get("areacode"),
            data.get("timezone"), data.get("born"), data.get("moddate"),
            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )

        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cursor = conn.cursor()
                # Update existing row (preserves memo column)
                cursor.execute("""
                    UPDATE qrz SET
                        active=?, name=?, address=?, city=?, county=?,
                        state=?, zip=?, country=?, ccode=?, lat=?, lon=?,
                        grid=?, fips=?, effdate=?, expdate=?,
                        class=?, email=?, image=?, areacode=?,
                        timezone=?, born=?, moddate=?, insert_date=?
                    WHERE callsign = ?
                """, values + (callsign,))
                if cursor.rowcount == 0:
                    # New callsign — insert fresh row
                    cursor.execute("""
                        INSERT INTO qrz (
                            callsign, active, name, address, city, county, state, zip,
                            country, ccode, lat, lon, grid, fips, effdate, expdate,
                            class, email, image, areacode, timezone, born, moddate, insert_date
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (callsign,) + values)
                conn.commit()
        except sqlite3.Error:
            pass

    def _api_request(self, params: Dict) -> Optional[ET.Element]:
        """
        Make API request to QRZ.

        Args:
            params: Query parameters

        Returns:
            XML root element or None on error
        """
        try:
            url = QRZ_API_URL + "?" + urllib.parse.urlencode(params, safe="")
            with urllib.request.urlopen(url, timeout=10) as response:
                xml_data = response.read().decode("utf-8")
                return ET.fromstring(xml_data)

        except urllib.error.URLError as e:
            qrz_log(f"Connection failed: {e.reason}")
            return None
        except ET.ParseError as e:
            qrz_log(f"Response parse error: {e}")
            return None

    def login(self, username: str = None, password: str = None) -> bool:
        """
        Authenticate with QRZ and get session key.

        Args:
            username: QRZ username (uses stored if not provided)
            password: QRZ password (uses stored if not provided)

        Returns:
            True if login successful
        """
        username = username or self.username
        password = password or self.password

        if not username or not password:
            qrz_log("Login skipped: no credentials configured")
            return False

        self.username = username
        self.password = password

        params = {
            "username": username,
            "password": password,
            "agent": "CommStat/2.5"
        }

        root = self._api_request(params)
        if root is None:
            return False

        # Handle XML namespace - QRZ uses xmlns="http://xmldata.qrz.com"
        ns = {"qrz": "http://xmldata.qrz.com"}

        # Try with namespace first, then without (for compatibility)
        session = root.find(".//qrz:Session", ns)
        if session is None:
            session = root.find(".//Session")

        if session is None:
            qrz_log("Login failed: unexpected server response (no Session element)")
            return False

        # Find Key element (with and without namespace)
        key_elem = session.find("qrz:Key", ns)
        if key_elem is None:
            key_elem = session.find("Key")

        if key_elem is not None and key_elem.text:
            self.session_key = key_elem.text
            qrz_log(f"Connected to QRZ.com as {username}")

            # Check subscription status
            sub_exp = session.find("qrz:SubExp", ns)
            if sub_exp is None:
                sub_exp = session.find("SubExp")
            if sub_exp is not None and sub_exp.text:
                qrz_log(f"Subscription expires: {sub_exp.text}")

            return True

        # Check for error - disable QRZ on auth failure
        error = session.find("qrz:Error", ns)
        if error is None:
            error = session.find("Error")
        if error is not None and error.text:
            qrz_log(f"Login failed: {error.text}")
            # Disable QRZ on authentication errors
            if "invalid" in error.text.lower() or "password" in error.text.lower():
                qrz_log("QRZ lookups disabled due to authentication failure")
                set_qrz_active(False)

        return False

    def lookup(self, callsign: str, use_cache: bool = True) -> Optional[Dict]:
        """
        Look up a callsign.

        Args:
            callsign: Callsign to look up
            use_cache: Check cache first (default True)

        Returns:
            Dict with callsign data or None if not found
        """
        callsign = callsign.upper().strip()
        if '/' in callsign:
            callsign = max(callsign.split('/'), key=len)

        # Check cache first (works even if QRZ is disabled)
        stale_data = None
        if use_cache:
            cached, fresh = self._get_cached(callsign)
            if cached and fresh:
                qrz_log(f"Cache hit for {callsign}")
                return cached
            if cached:
                stale_data = cached

        # Check if QRZ is active before making API calls
        if not self.is_active():
            qrz_log("QRZ disabled or QRZ not configured")
            return stale_data

        # Need session key
        if not self.session_key:
            if not self.login():
                return stale_data

        # Make API call
        params = {
            "s": self.session_key,
            "callsign": callsign
        }

        root = self._api_request(params)
        if root is None:
            return stale_data

        # Handle XML namespace
        ns = {"qrz": "http://xmldata.qrz.com"}

        # Check for errors (session expired, not found, etc.)
        session = root.find(".//qrz:Session", ns)
        if session is None:
            session = root.find(".//Session")
        if session is not None:
            error = session.find("qrz:Error", ns)
            if error is None:
                error = session.find("Error")
            if error is not None and error.text:
                if "Session Timeout" in error.text or "Invalid session" in error.text:
                    # Session expired, re-login and retry
                    qrz_log(f"Session expired, re-authenticating...")
                    self.session_key = None
                    if self.login():
                        return self.lookup(callsign, use_cache=False)
                else:
                    qrz_log(f"Lookup error for {callsign}: {error.text}")
                return stale_data

        # Parse callsign data
        callsign_elem = root.find(".//qrz:Callsign", ns)
        if callsign_elem is None:
            callsign_elem = root.find(".//Callsign")
        if callsign_elem is None:
            qrz_log(f"Data not found for {callsign}")
            return stale_data

        # Extract all fields (strip namespace from tag names)
        data = {}
        for child in callsign_elem:
            # Remove namespace prefix like {http://xmldata.qrz.com}
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            data[tag] = child.text

        # Save to cache
        self._save_to_cache(data)

        name = " ".join(filter(None, [data.get("fname", ""), data.get("name", "")])).strip()
        grid = data.get("grid", "")
        details = ", ".join(filter(None, [name, grid]))
        qrz_log(f"Data returned for {callsign}" + (f" ({details})" if details else ""))
        return data


# Command-line test
if __name__ == "__main__":
    import sys

    print("QRZ.com API Test")
    print("-" * 40)

    # Get config from database
    active, username, password = load_qrz_config()

    print(f"QRZ Active: {active}")

    if username and password:
        print(f"Using credentials from database (user: {username})")
    elif len(sys.argv) >= 3:
        username = sys.argv[1]
        password = sys.argv[2]
        active = True  # Override for command-line testing
    else:
        print("No credentials in database")
        username = input("QRZ Username: ")
        password = input("QRZ Password: ")
        active = True  # Override for manual testing

    if not active:
        print("\nQRZ is disabled. Enable it in Menu > QRZ ENABLE.")
        sys.exit(0)

    # Get callsign to lookup
    if len(sys.argv) > 3:
        callsign = sys.argv[3]
    elif len(sys.argv) == 2:
        callsign = sys.argv[1]
    else:
        callsign = input("Callsign to lookup (default AA7BQ): ") or "AA7BQ"

    # Test
    print(f"\nLooking up: {callsign}")
    client = QRZClient(username, password)

    print("Attempting login...")
    if client.login():
        print()
        result = client.lookup(callsign)

        if result:
            print()
            print(f"Results for {callsign}:")
            print("-" * 40)

            # Display key fields
            fields = [
                ("call", "Callsign"),
                ("name", "Name"),
                ("addr1", "Address"),
                ("addr2", "City"),
                ("state", "State"),
                ("country", "Country"),
                ("ccode", "Country Code"),
                ("grid", "Grid"),
                ("lat", "Latitude"),
                ("lon", "Longitude"),
                ("county", "County"),
                ("fips", "FIPS"),
                ("efdate", "Eff. Date"),
                ("expdate", "Exp. Date"),
                ("class", "License Class"),
                ("email", "Email"),
                ("areacode", "Area Code"),
                ("timezone", "Timezone"),
                ("born", "Born"),
            ]

            for key, label in fields:
                value = result.get(key)
                if value:
                    print(f"{label:15}: {value}")
        else:
            print(f"No results for {callsign}")
    else:
        print("Login failed - check credentials")
