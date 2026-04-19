#!/usr/bin/env bash
set -euo pipefail

# PrintBot one-liner installer
# Usage: sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/mhd12e/printbot/main/install.sh)"

REPO="https://github.com/mhd12e/printbot.git"
INSTALL_DIR="$HOME/printbot"

# If run via sudo, install to the calling user's home
if [ -n "${SUDO_USER:-}" ]; then
    INSTALL_DIR="$(eval echo "~$SUDO_USER")/printbot"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}PrintBot Installer${NC}"
echo ""

# Check root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Run with sudo:${NC}"
    echo '  sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/mhd12e/printbot/main/install.sh)"'
    exit 1
fi

# Install git if missing
if ! command -v git &> /dev/null; then
    echo "Installing git..."
    apt update -qq && apt install -y -qq git > /dev/null
fi

# Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "PrintBot already cloned at $INSTALL_DIR. Pulling latest..."
    git -C "$INSTALL_DIR" pull origin main
else
    echo "Cloning PrintBot to $INSTALL_DIR..."
    git clone "$REPO" "$INSTALL_DIR"
    chown -R "$SUDO_USER:$(id -gn "$SUDO_USER")" "$INSTALL_DIR"
fi

echo ""

# Hand off to printbot install
chmod +x "$INSTALL_DIR/printbot"
exec "$INSTALL_DIR/printbot" install
