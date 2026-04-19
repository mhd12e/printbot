# PrintBot

A Telegram bot that prints documents and images from your phone to a USB printer. Send a file, pick your settings with buttons, and it prints. That's it.

```
You (Telegram) --> Bot (Python) --> CUPS --> Printer (USB)
```

## What It Does

- Send a **PDF, DOCX, PPTX**, or any **image** to the bot on Telegram
- Pick print settings with **inline buttons** — no typing needed
- The bot prints it and gives you **live status updates**

### Print Options (all buttons)

| Option | Choices |
|---|---|
| Color | Color, B&W |
| Sides | One-sided, Long edge, Short edge |
| Orientation | Portrait, Landscape |
| Pages per sheet | 1, 2, 4, 6, 9 |
| Page range | All, Custom |
| Copies | - / + |

### Notifications

The bot tells you when:
- A job finishes or fails (with retry button)
- The printer goes offline or comes back
- Ink is running low

## Quick Start

### Prerequisites

- **Ubuntu Server** (tested on 24.04 LTS)
- **HP Smart Tank 725** connected via USB (other CUPS-compatible printers will work too)
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather)
- Your **Telegram user ID** from [@userinfobot](https://t.me/userinfobot)

### Install

One command:

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/mhd12e/printbot/main/install.sh)"
```

This clones the repo to `~/printbot` and runs the full installer: system packages, CUPS, printer drivers, Python venv, config prompts, systemd service, and adds `printbot` to your PATH.

After install, open your bot on Telegram and send `/start`.

## Managing the Bot

```bash
printbot status           # Check bot status, version, printer info
printbot logs -f          # Follow live logs
printbot logs 100         # Last 100 log lines
sudo printbot restart     # Restart the bot
sudo printbot stop        # Stop the bot
sudo printbot start       # Start the bot
```

## Updating

```bash
sudo printbot update
```

Pulls latest code from git, updates Python packages, runs migrations (prompts for new config if needed), and restarts the service.

## User Management

Multiple Telegram users can be authorized to use the bot:

```bash
printbot user list                          # List all authorized users
sudo printbot user add 123456789 --name Mo  # Add a user with a name
sudo printbot user add 987654321            # Add without a name
sudo printbot user remove 987654321         # Revoke access
sudo printbot user label 123456789 Mohamed  # Set a display name
```

Get any Telegram user's ID from [@userinfobot](https://t.me/userinfobot).

## Configuration

```bash
printbot config                       # Show all config (secrets masked)
printbot config get PRINTER_NAME      # Get a specific value
sudo printbot config set KEY=VALUE    # Set a value
sudo printbot config remove KEY       # Remove an optional key
sudo printbot config edit             # Open .env in editor
printbot config keys                  # List all known keys + descriptions
```

Every command supports `--help`:

```bash
printbot config --help    # Detailed usage + all known config keys
printbot user --help      # User management help
printbot logs --help      # Log viewing options
```

## Printer

```bash
printbot printer              # Printer status and queue
sudo printbot printer setup   # HP printer setup wizard
sudo printbot printer test    # Print a test page
sudo printbot printer cancel  # Cancel all print jobs
```

## Uninstall

```bash
sudo printbot uninstall   # Removes service and venv, keeps .env and code
```

## All Commands

Every command supports `--help` for detailed usage.

```
Setup:
  install                  Fresh install
  update                   Pull, update deps, migrate, restart
  uninstall                Remove service and venv
  migrate                  Run pending version migrations

Service:
  start / stop / restart   Service control
  status                   Status, version, printer, update check

Users:
  user list                List authorized users
  user add <ID> [--name]   Authorize a user
  user remove <ID>         Revoke access
  user label <ID> <name>   Set display name

Config:
  config                   Show config (secrets masked)
  config get/set/remove    Read, write, delete keys
  config edit              Open .env in editor
  config keys              List all known keys

Logs:
  logs                     Last 50 lines
  logs -f                  Follow live
  logs <N>                 Last N lines
  logs --error / --today   Filtered views

Printer:
  printer                  Status and queue
  printer setup            HP setup wizard
  printer test             Test page
  printer cancel           Cancel all jobs

Other:
  version                  Version + git info
  help                     All commands
```

## How It Works

```
User sends file
      |
      v
  Download file
      |
      v
  DOCX/PPTX? --yes--> Convert to PDF (LibreOffice headless)
      |                        |
      no                       |
      |                        v
      +<-----------------------+
      |
      v
  Show settings screen (inline buttons)
      |
      v
  User taps [Print]
      |
      v
  Submit to CUPS with selected options
      |
      v
  Poll job status every 3s, update message
      |
      v
  Done / Failed (with retry) / Cancelled
```

## Project Structure

```
bot.py           Telegram handlers, conversation flow, background polling
printer.py       CUPS wrapper (submit, cancel, status, queue, ink levels)
converter.py     DOCX/PPTX to PDF conversion, page counting
config.py        Loads .env, defines constants and CUPS option mappings
setup.sh         One-command Ubuntu Server setup
requirements.txt Python dependencies
.env.example     Config template
```

## Supported Formats

| Type | Formats |
|---|---|
| Documents | PDF, DOCX, PPTX |
| Images | JPEG, PNG, GIF, BMP, TIFF, WEBP |

DOCX and PPTX are converted to PDF before printing. Images are sent directly to CUPS.

## Requirements

- **OS:** Ubuntu Server 24.04 LTS (recommended)
- **Printer:** Any CUPS-compatible printer via USB (tested with HP Smart Tank 725)
- **System packages:** CUPS, HPLIP, LibreOffice, poppler-utils
- **Python:** 3.10+
- **Python packages:** python-telegram-bot, pycups, Pillow, python-dotenv

## License

MIT
