#!/bin/bash
# CommStat Linux Installation Script
# For Debian/Ubuntu-based systems (including Linux Mint, Raspberry Pi OS)

set -e  # Exit on error

echo "=============================================="
echo "CommStat Linux Installer"
echo "=============================================="

# Check if running on Linux
if [[ "$OSTYPE" != "linux-gnu"* ]]; then
    echo "Error: This script is for Linux only."
    echo "For macOS, run: python3 install.py"
    exit 1
fi

# Check for apt (Debian/Ubuntu)
if ! command -v apt &> /dev/null; then
    echo "Error: apt package manager not found."
    echo "This script is for Debian/Ubuntu-based systems."
    echo ""
    echo "For other distros, manually install PyQt5 WebEngine and Multimedia:"
    echo "  Fedora: sudo dnf install python3-qt5-webengine qt5-qtmultimedia"
    echo "  Arch:   sudo pacman -S python-pyqt5-webengine qt5-multimedia"
    echo "Then run: python3 install.py"
    exit 1
fi

# Check for Python 3
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 not found. Please install Python 3.8 or newer."
    exit 1
fi

echo ""
echo "Installing PyQt5 WebEngine via apt..."
echo "(You may be prompted for your sudo password)"
echo ""

sudo apt update
sudo apt install -y python3-pyqt5 python3-pyqt5.qtwebengine python3-pyqt5.qtmultimedia libqt5multimedia5-plugins libenchant-2-dev python3-tk

echo ""
echo "Running Python installer..."
echo ""

python3 install.py

echo ""
echo "Installing application icon..."
sudo cp "$(dirname "$0")/radiation-32.png" /usr/share/pixmaps/radiation-32.png

echo ""
echo "Creating desktop shortcut..."
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/Desktop"
cat > "$HOME/Desktop/CommStat.desktop" << EOF
[Desktop Entry]
Type=Application
Name=CommStat
Comment=Run CommStat
Exec=$INSTALL_DIR/linuxlauncher.sh
Icon=radiation-32.png
Terminal=false
StartupNotify=true
Path=$INSTALL_DIR
EOF
chmod +x "$HOME/Desktop/CommStat.desktop"
echo "Desktop shortcut created at $HOME/Desktop/CommStat.desktop"

echo ""
echo "=============================================="
echo "Installation complete!"
echo "https://commstat.app/"
echo ""
# Raspberry Pi-specific reminder
if [[ "$(uname -m)" == "aarch64" || "$(uname -m)" == "armv7l" ]]; then
    echo "Raspberry Pi detected!"
    echo "Note: JS8Call must be installed separately."
    echo "Download the ARM build from https://js8call-improved.com/"
    echo "CommStat connects to JS8Call on localhost:2442."
fi
echo "=============================================="
