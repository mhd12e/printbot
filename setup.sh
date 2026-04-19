#!/usr/bin/env bash
set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────
BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="printer-bot"
VENV_DIR="$BOT_DIR/venv"

echo "=== Printer Bot Setup (Ubuntu Server) ==="
echo "Install directory: $BOT_DIR"
echo ""

# ── 1. System packages ─────────────────────────────────────────────
echo "[1/7] Installing system packages..."
sudo apt update
sudo apt install -y \
    cups libcups2-dev \
    hplip \
    libreoffice-core libreoffice-writer libreoffice-impress \
    poppler-utils \
    python3 python3-pip python3-venv python3-dev \
    gcc

# ── 2. CUPS ────────────────────────────────────────────────────────
echo "[2/7] Configuring CUPS..."
sudo systemctl enable --now cups
sudo usermod -aG lpadmin "$USER"

# ── 3. Printer setup ──────────────────────────────────────────────
echo "[3/7] Setting up HP Smart Tank 725..."
echo ""

# Check if any HP printer is already configured
if lpstat -p 2>/dev/null | grep -qi "hp"; then
    echo "HP printer already detected in CUPS:"
    lpstat -p
    echo ""
    read -rp "Skip printer setup? (Y/n): " SKIP_PRINTER
    SKIP_PRINTER="${SKIP_PRINTER:-Y}"
else
    SKIP_PRINTER="n"
fi

if [[ "${SKIP_PRINTER,,}" != "y" ]]; then
    echo "Connect the HP Smart Tank 725 via USB now, then press Enter."
    read -r
    sudo hp-setup -i
    echo ""
    echo "Printer configured. Verifying..."
    lpstat -p
fi

# Get printer name
DETECTED_PRINTER=$(lpstat -p 2>/dev/null | head -1 | awk '{print $2}' || true)
if [ -z "$DETECTED_PRINTER" ]; then
    echo "WARNING: No printer detected. You'll need to set PRINTER_NAME in .env manually."
    DETECTED_PRINTER="HP_Smart_Tank_725"
fi
echo "Using printer: $DETECTED_PRINTER"

# ── 4. Python virtual environment ─────────────────────────────────
echo "[4/7] Setting up Python environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$BOT_DIR/requirements.txt"

# ── 5. Configure .env ─────────────────────────────────────────────
echo "[5/7] Configuring bot..."

if [ -f "$BOT_DIR/.env" ]; then
    echo ".env already exists, keeping it."
else
    echo ""
    read -rp "Telegram bot token (from @BotFather): " BOT_TOKEN
    read -rp "Your Telegram user ID (from @userinfobot): " USER_IDS

    cat > "$BOT_DIR/.env" <<ENVEOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
ALLOWED_USER_IDS=$USER_IDS
PRINTER_NAME=$DETECTED_PRINTER
ENVEOF

    chmod 600 "$BOT_DIR/.env"
    echo ".env created."
fi

# ── 6. Create temp directory ──────────────────────────────────────
echo "[6/7] Creating temp directory..."
mkdir -p /tmp/printer_bot

# ── 7. Systemd service (run on startup, restart on crash) ─────────
echo "[7/7] Installing systemd service..."

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<SERVICEEOF
[Unit]
Description=Telegram Printer Bot
After=network-online.target cups.service
Wants=network-online.target cups.service

[Service]
Type=simple
User=$USER
Group=$(id -gn)
WorkingDirectory=$BOT_DIR
ExecStart=$VENV_DIR/bin/python3 $BOT_DIR/bot.py
Restart=always
RestartSec=5
EnvironmentFile=$BOT_DIR/.env

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/tmp/printer_bot $BOT_DIR
PrivateTmp=false

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"

echo ""
echo "=== Setup complete! ==="
echo ""
echo "The bot is running and will start automatically on boot."
echo ""
echo "Useful commands:"
echo "  Status:   sudo systemctl status $SERVICE_NAME"
echo "  Logs:     sudo journalctl -u $SERVICE_NAME -f"
echo "  Restart:  sudo systemctl restart $SERVICE_NAME"
echo "  Stop:     sudo systemctl stop $SERVICE_NAME"
echo ""
echo "To update the bot later:"
echo "  cd $BOT_DIR"
echo "  git pull"
echo "  sudo systemctl restart $SERVICE_NAME"
