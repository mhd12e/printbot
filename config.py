import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS: set[int] = {
    int(uid.strip()) for uid in os.environ["ALLOWED_USER_IDS"].split(",")
}
PRINTER_NAME: str = os.getenv("PRINTER_NAME", "HP_Smart_Tank_725")
TEMP_DIR: Path = Path(os.getenv("TEMP_DIR", "/tmp/printbot"))
CUPS_POLL_INTERVAL: int = int(os.getenv("CUPS_POLL_INTERVAL", "3"))

# Gemini (optional — voice features disabled if not set)
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")

# Supported file extensions
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"}
SUPPORTED_EXTENSIONS = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS
CONVERTIBLE_EXTENSIONS = {".docx", ".pptx"}

# CUPS option mappings
COLOR_OPTIONS = {"color": "color", "bw": "monochrome"}
SIDES_OPTIONS = {
    "one": "one-sided",
    "long": "two-sided-long-edge",
    "short": "two-sided-short-edge",
}
ORIENTATION_OPTIONS = {"portrait": "3", "landscape": "4"}
NUP_OPTIONS = [1, 2, 4, 6, 9]

# Default print settings
DEFAULT_SETTINGS = {
    "color": "color",
    "sides": "one",
    "orientation": "portrait",
    "nup": 1,
    "page_range": "all",
    "copies": 1,
}

# Human-readable option descriptions (used in Gemini prompts)
AVAILABLE_OPTIONS = {
    "color": {"color": "full color", "bw": "black and white"},
    "sides": {
        "one": "one-sided",
        "long": "duplex long edge (normal two-sided)",
        "short": "duplex short edge",
    },
    "orientation": {"portrait": "portrait / vertical", "landscape": "landscape / horizontal"},
    "nup": [1, 2, 4, 6, 9],
    "copies": "1-99",
    "page_range": "e.g. 1-3,5,8-10 or all",
}

# Max voice note duration (seconds)
MAX_VOICE_DURATION = 60

TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Ensure temp dir is writable (may have been created by root during install)
if not os.access(TEMP_DIR, os.W_OK):
    import sys
    print(f"WARNING: {TEMP_DIR} is not writable. Run: sudo printbot update", file=sys.stderr)
