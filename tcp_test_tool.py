#!/usr/bin/env python3
"""
tcp_test_tool.py — Standalone JS8Call TCP mock server for CommStat testing.

Acts as a fake JS8Call so CommStat can connect and receive injected test
messages without needing a live radio. Run this, then start/restart CommStat
configured to connect to the same port (default 2442).

Usage:
    python tcp_test_tool.py
"""

import json
import socket
import sys
import threading
from datetime import datetime, timezone

HOUR_LETTERS = "ABCDEFGHIJKLMNPQRSTUVWXY"

def _current_msg_id() -> str:
    now = datetime.now(timezone.utc)
    return f"{HOUR_LETTERS[now.hour]}{now.minute:02d}"

from PyQt5 import QtCore, QtWidgets

DEFAULT_PORT = 2822
MOCK_GRID = "EM50"
DEFAULT_MY_CALLSIGN = "W1TEST"


# ---------------------------------------------------------------------------
# TCP server (runs in a background thread)
# ---------------------------------------------------------------------------

class MockServer(QtCore.QObject):
    """Listens for a single CommStat connection and relays messages to it."""

    connected = QtCore.pyqtSignal(str)       # addr string
    disconnected = QtCore.pyqtSignal()
    logged = QtCore.pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._conn: socket.socket | None = None
        self._running = False
        self._my_callsign = DEFAULT_MY_CALLSIGN

    def set_my_callsign(self, callsign: str) -> None:
        self._my_callsign = callsign.strip().upper() or DEFAULT_MY_CALLSIGN

    # ------------------------------------------------------------------
    def start(self, host: str, port: int) -> None:
        self._running = True
        threading.Thread(target=self._serve, args=(host, port), daemon=True).start()

    def restart(self, host: str, port: int) -> None:
        self._running = False
        QtCore.QThread.msleep(1200)
        self.start(host, port)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    def _serve(self, host: str, port: int) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((host, port))
            srv.listen(1)
        except OSError as exc:
            self.logged.emit(f"ERROR binding to {host}:{port}: {exc}")
            return

        srv.settimeout(1.0)
        self.logged.emit(f"Listening on {host}:{port} — waiting for CommStat")

        while self._running:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except Exception as exc:
                if self._running:
                    self.logged.emit(f"Accept error: {exc}")
                continue

            self.logged.emit(f"CommStat connected from {addr[0]}:{addr[1]}")
            with self._lock:
                self._conn = conn
            self.connected.emit(f"{addr[0]}:{addr[1]}")
            self._handle(conn)
            with self._lock:
                self._conn = None
            self.disconnected.emit()
            self.logged.emit("CommStat disconnected — waiting for reconnect")

        srv.close()

    def _handle(self, conn: socket.socket) -> None:
        """Read inbound requests from CommStat and answer the handshake."""
        buf = b""
        conn.settimeout(1.0)
        while self._running:
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._respond(conn, json.loads(line.decode()))
                    except json.JSONDecodeError:
                        pass
            except socket.timeout:
                continue
            except Exception:
                break

    def _respond(self, conn: socket.socket, msg: dict) -> None:
        """Answer CommStat's initialisation queries."""
        t = msg.get("type", "")
        self.logged.emit(f"  << {t}")
        replies = {
            "STATION.GET_CALLSIGN": {"type": "STATION.CALLSIGN",  "value": self._my_callsign, "params": {}},
            "MODE.GET_SPEED":       {"type": "MODE.SPEED",         "value": "",                "params": {"SPEED": 0}},
            "RIG.GET_FREQ":         {"type": "RIG.FREQ",           "value": "",                "params": {"DIAL": 7110000, "OFFSET": 1940, "FREQ": 7111940}},
            "STATION.GET_GRID":     {"type": "STATION.GRID",       "value": MOCK_GRID,         "params": {}},
        }
        reply = replies.get(t)
        if reply:
            self._write(conn, reply)

    def _write(self, conn: socket.socket, msg: dict) -> None:
        try:
            conn.sendall((json.dumps(msg) + "\n").encode())
            self.logged.emit(f"  >> {msg['type']}")
        except Exception as exc:
            self.logged.emit(f"Send error: {exc}")

    # ------------------------------------------------------------------
    def inject(self, msg: dict) -> None:
        """Send an arbitrary message to the connected CommStat instance."""
        with self._lock:
            conn = self._conn
        if conn is None:
            self.logged.emit("No CommStat connected — message not sent")
            return
        self._write(conn, msg)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class MainWindow(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.server = MockServer()
        self.server.logged.connect(self._log)
        self.server.connected.connect(self._on_connected)
        self.server.disconnected.connect(self._on_disconnected)

        self._build_ui()
        self._start_server()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowTitle("JS8Call TCP Mock — CommStat Test Tool")
        self.setMinimumWidth(560)
        self.resize(620, 580)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(15, 15, 15, 15)
        root.setSpacing(10)

        # -- Status -------------------------------------------------------
        self.status_lbl = QtWidgets.QLabel("Waiting for CommStat connection…")
        self.status_lbl.setStyleSheet("font-weight:bold; color:#AA6600;")
        root.addWidget(self.status_lbl)

        # -- IP / Port ----------------------------------------------------
        port_row = QtWidgets.QHBoxLayout()
        port_row.addWidget(QtWidgets.QLabel("IP address:"))
        self.ip_edit = QtWidgets.QLineEdit("0.0.0.0")
        self.ip_edit.setMaximumWidth(120)
        port_row.addWidget(self.ip_edit)
        port_row.addSpacing(12)
        port_row.addWidget(QtWidgets.QLabel("Port:"))
        self.port_edit = QtWidgets.QLineEdit(str(DEFAULT_PORT))
        self.port_edit.setMaximumWidth(70)
        port_row.addWidget(self.port_edit)
        self.restart_btn = QtWidgets.QPushButton("Restart Server")
        self.restart_btn.setStyleSheet(
            "QPushButton { background:#17a2b8; color:white; border-radius:4px;"
            "  padding:4px 10px; font-weight:bold; }"
            "QPushButton:hover { background:#117a8b; }"
            "QPushButton:pressed { background:#0c5460; }"
        )
        self.restart_btn.clicked.connect(self._restart_server)
        port_row.addSpacing(12)
        port_row.addWidget(self.restart_btn)
        port_row.addStretch()
        root.addLayout(port_row)

        root.addWidget(self._hr())

        # -- My Station Callsign (reported to CommStat as STATION.CALLSIGN) --
        my_call_row = QtWidgets.QHBoxLayout()
        my_call_row.addWidget(QtWidgets.QLabel("My Callsign:"))
        self.my_call_edit = QtWidgets.QLineEdit(DEFAULT_MY_CALLSIGN)
        self.my_call_edit.setMaximumWidth(100)
        self.my_call_edit.setMaxLength(12)
        self.my_call_edit.setToolTip(
            "Station callsign CommStat will receive via STATION.CALLSIGN handshake.\n"
            "Set this to match the TO field when testing direct-callsign messages."
        )
        self.my_call_edit.textChanged.connect(
            lambda t: (self.my_call_edit.blockSignals(True),
                       self.my_call_edit.setText(t.upper()),
                       self.my_call_edit.blockSignals(False),
                       self.server.set_my_callsign(t))
        )
        my_call_row.addWidget(self.my_call_edit)
        my_call_row.addWidget(QtWidgets.QLabel(
            "(must match TO field for direct-callsign messages)"
        ))
        my_call_row.addStretch()
        root.addLayout(my_call_row)

        root.addWidget(self._hr())

        # -- Message type -------------------------------------------------
        type_row = QtWidgets.QHBoxLayout()
        type_row.addWidget(QtWidgets.QLabel("Message type:"))
        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.addItems(["RX.DIRECTED", "RX.ACTIVITY"])
        self.type_combo.currentTextChanged.connect(self._on_type_changed)
        type_row.addWidget(self.type_combo)
        type_row.addStretch()
        root.addLayout(type_row)

        # -- FROM / TO ----------------------------------------------------
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(8)

        grid.addWidget(QtWidgets.QLabel("FROM callsign:"), 0, 0)
        self.from_edit = QtWidgets.QLineEdit("KG4AQH")
        self.from_edit.setMaxLength(12)
        self.from_edit.textChanged.connect(self._upper)
        self.from_edit.textChanged.connect(self._auto_build_value)
        grid.addWidget(self.from_edit, 0, 1)

        grid.addWidget(QtWidgets.QLabel("TO (group or call):"), 1, 0)
        self.to_edit = QtWidgets.QLineEdit("@AMRRON")
        self.to_edit.setMaxLength(15)
        self.to_edit.textChanged.connect(self._upper)
        self.to_edit.textChanged.connect(self._auto_build_value)
        grid.addWidget(self.to_edit, 1, 1)

        root.addLayout(grid)

        # -- JS8Call format selector --------------------------------------
        fmt_row = QtWidgets.QHBoxLayout()
        fmt_row.addWidget(QtWidgets.QLabel("JS8Call format:"))
        self.fmt_combo = QtWidgets.QComboBox()
        self.fmt_combo.addItems(["New (fixed) — value has no callsign prefix",
                                  "Old (buggy) — value repeats callsign"])
        self.fmt_combo.currentIndexChanged.connect(self._auto_build_value)
        fmt_row.addWidget(self.fmt_combo)
        fmt_row.addStretch()
        root.addLayout(fmt_row)

        # -- Value field --------------------------------------------------
        val_row = QtWidgets.QHBoxLayout()
        val_row.addWidget(QtWidgets.QLabel("Value (editable):"))
        self.value_edit = QtWidgets.QLineEdit()
        self.value_edit.setPlaceholderText("auto-filled — edit freely before sending")
        val_row.addWidget(self.value_edit)
        root.addLayout(val_row)

        # -- SNR / Dial / Offset ------------------------------------------
        params_row = QtWidgets.QHBoxLayout()
        params_row.addWidget(QtWidgets.QLabel("SNR dB:"))
        self.snr_edit = QtWidgets.QLineEdit("+11")
        self.snr_edit.setMaximumWidth(55)
        params_row.addWidget(self.snr_edit)

        params_row.addSpacing(16)
        params_row.addWidget(QtWidgets.QLabel("Dial MHz:"))
        self.freq_edit = QtWidgets.QLineEdit("7.110000")
        self.freq_edit.setMaximumWidth(90)
        params_row.addWidget(self.freq_edit)

        params_row.addSpacing(16)
        params_row.addWidget(QtWidgets.QLabel("Offset Hz:"))
        self.offset_edit = QtWidgets.QLineEdit("1940")
        self.offset_edit.setMaximumWidth(65)
        params_row.addWidget(self.offset_edit)
        params_row.addStretch()
        root.addLayout(params_row)

        # -- Send button --------------------------------------------------
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        self.send_btn = QtWidgets.QPushButton("Send to CommStat")
        self.send_btn.setEnabled(False)
        self.send_btn.setMinimumWidth(160)
        self.send_btn.setStyleSheet(
            "QPushButton { background:#007bff; color:white; border-radius:4px;"
            "  padding:6px 14px; font-weight:bold; }"
            "QPushButton:hover { background:#0056b3; }"
            "QPushButton:pressed { background:#004094; }"
            "QPushButton:disabled { background:#aaa; }"
        )
        self.send_btn.clicked.connect(self._send)
        btn_row.addWidget(self.send_btn)
        root.addLayout(btn_row)

        root.addWidget(self._hr())

        # -- Log ----------------------------------------------------------
        root.addWidget(QtWidgets.QLabel("Log:"))
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(300)
        self.log_view.setStyleSheet("font-family:Consolas; font-size:11px;")
        root.addWidget(self.log_view)

        self._auto_build_value()

    def _hr(self) -> QtWidgets.QFrame:
        f = QtWidgets.QFrame()
        f.setFrameShape(QtWidgets.QFrame.HLine)
        f.setFrameShadow(QtWidgets.QFrame.Sunken)
        return f

    # ------------------------------------------------------------------
    def _upper(self, text: str) -> None:
        """Auto-uppercase callsign fields."""
        sender = self.sender()
        if text != text.upper():
            sender.blockSignals(True)
            sender.setText(text.upper())
            sender.blockSignals(False)

    def _on_type_changed(self, msg_type: str) -> None:
        self._auto_build_value()

    def _auto_build_value(self) -> None:
        """Pre-fill the value field based on current inputs."""
        from_call = self.from_edit.text().strip().upper()
        to_call = self.to_edit.text().strip().upper()
        old_format = self.fmt_combo.currentIndex() == 1

        # Build a sample value — user can override before sending
        if old_format:
            # Old buggy JS8Call: callsign duplicated in value
            prefix = f"{from_call}: {from_call}: "
        else:
            # New fixed JS8Call: no callsign in value for RX.DIRECTED
            # (for RX.ACTIVITY the full line is in value)
            msg_type = self.type_combo.currentText()
            if msg_type == "RX.DIRECTED":
                prefix = ""
            else:
                prefix = f"{from_call}: "

        self.value_edit.setPlaceholderText(
            f"{prefix}{to_call} MSG ,{_current_msg_id()},TEST MESSAGE,{{^%}}"
        )
        self.value_edit.setText(f"{prefix}{to_call} MSG ,{_current_msg_id()},TEST MESSAGE,{{^%}}")

    # ------------------------------------------------------------------
    def _start_server(self) -> None:
        host = self.ip_edit.text().strip() or "127.0.0.1"
        try:
            port = int(self.port_edit.text())
        except ValueError:
            port = DEFAULT_PORT
        self.server.start(host, port)

    def _restart_server(self) -> None:
        host = self.ip_edit.text().strip() or "127.0.0.1"
        try:
            port = int(self.port_edit.text())
        except ValueError:
            port = DEFAULT_PORT
        self._log(f"Restarting server on {host}:{port}…")
        threading.Thread(target=self.server.restart, args=(host, port), daemon=True).start()

    def _on_connected(self, addr: str) -> None:
        self.status_lbl.setText(f"CommStat CONNECTED ({addr})")
        self.status_lbl.setStyleSheet("font-weight:bold; color:#28a745;")
        self.send_btn.setEnabled(True)

    def _on_disconnected(self) -> None:
        self.status_lbl.setText("Waiting for CommStat connection…")
        self.status_lbl.setStyleSheet("font-weight:bold; color:#AA6600;")
        self.send_btn.setEnabled(False)

    # ------------------------------------------------------------------
    def _send(self) -> None:
        from_call = self.from_edit.text().strip()
        to_call = self.to_edit.text().strip()
        value = self.value_edit.text().strip()
        msg_type = self.type_combo.currentText()

        try:
            snr = int(self.snr_edit.text().strip().lstrip("+"))
        except ValueError:
            snr = 0
        try:
            dial_hz = int(float(self.freq_edit.text().strip()) * 1_000_000)
        except ValueError:
            dial_hz = 7_110_000
        try:
            offset = int(self.offset_edit.text().strip())
        except ValueError:
            offset = 1940

        utc_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        msg = {
            "type": msg_type,
            "value": value,
            "params": {
                "FROM": from_call,
                "TO": to_call,
                "GRID": "",
                "FREQ": dial_hz + offset,
                "OFFSET": offset,
                "SNR": snr,
                "UTC": utc_ms,
            }
        }

        self.server.inject(msg)
        self._log(f"Injected {msg_type}: FROM={from_call} TO={to_call} | {value}")

    # ------------------------------------------------------------------
    def _log(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {text}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
