from __future__ import annotations

import logging
import os
import re
from functools import wraps
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import converter
import gemini
import printer

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
SETTINGS = 0
PAGE_RANGE = 1
VOICE_PENDING = 2
BATCH_COLLECTING = 3
BATCH_SETTINGS = 4
BATCH_PAGE_RANGE = 5

# Valid fields and their allowed values for setting toggles
_VALID_SETTINGS = {
    "color": {"color", "bw"},
    "sides": {"one", "long", "short"},
    "orientation": {"portrait", "landscape"},
}
_VALID_NUP = set(config.NUP_OPTIONS)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def authorized(func):
    """Only allow whitelisted Telegram user IDs."""

    @wraps(func)
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        user_id = update.effective_user.id
        if user_id not in config.ALLOWED_USER_IDS:
            text = "Sorry, you are not authorized to use this bot."
            if update.callback_query:
                await update.callback_query.answer(text, show_alert=True)
            elif update.message:
                await update.message.reply_text(text)
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@authorized
async def cmd_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    # Clean up any pending state
    context.user_data.pop("job", None)
    context.user_data.pop("batch", None)
    context.user_data.pop("voice_instruction", None)

    lines = [
        "Welcome to PrintBot!",
        "",
        "Send me a file or photo and I'll print it.",
    ]
    if config.GEMINI_API_KEY:
        lines.append("You can also send a voice note with instructions.")
    lines += [
        "",
        "Supported formats:",
        "PDF, DOCX, PPTX, JPG, PNG, GIF, BMP, TIFF, WEBP",
    ]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\U0001f5a8 Printer Status", callback_data="main:status"
                ),
                InlineKeyboardButton(
                    "\U0001f4cb Print Queue", callback_data="main:queue"
                ),
            ]
        ]
    )
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=keyboard,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Shared file preparation
# ---------------------------------------------------------------------------

async def _prepare_file(
    tg_file_obj, file_name: str, ext: str, user_id: int, file_unique_id: str,
    status_msg=None,
) -> dict | str:
    """Download, validate, convert a file. Returns file info dict or error string."""
    local_path = config.TEMP_DIR / f"{user_id}_{file_unique_id}{ext}"
    await tg_file_obj.download_to_drive(local_path)

    if local_path.stat().st_size == 0:
        converter.cleanup_temp_files(local_path)
        return "File is empty."

    is_image = converter.is_image(ext)
    pdf_path = None
    page_count = None

    if converter.needs_conversion(ext):
        if status_msg:
            await status_msg.edit_text(f"Converting {file_name} to PDF\u2026")
        try:
            pdf_path = await converter.convert_to_pdf(local_path)
        except Exception as e:
            logger.error("Conversion failed: %s", e)
            converter.cleanup_temp_files(local_path)
            return "Conversion failed. The file may be corrupted."
        page_count = await converter.get_pdf_page_count(pdf_path)
    elif ext == ".pdf":
        page_count = await converter.get_pdf_page_count(local_path)

    return {
        "file_path": local_path,
        "pdf_path": pdf_path,
        "original_name": file_name,
        "is_image": is_image,
        "page_count": page_count,
        "settings": dict(config.DEFAULT_SETTINGS),
    }


# ---------------------------------------------------------------------------
# Settings screen builders
# ---------------------------------------------------------------------------

def _mark(label: str, settings: dict, field: str, value) -> str:
    return f"\u2713 {label}" if settings[field] == value else label


def build_settings_screen(
    job: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build single-file print-settings message and keyboard."""
    s = job["settings"]
    name = job["original_name"]

    if job["is_image"]:
        header = f"\U0001f5bc {name} \u2014 image"
    elif job["page_count"]:
        header = f"\U0001f4c4 {name} \u2014 {job['page_count']} pages"
    else:
        header = f"\U0001f4c4 {name}"

    text = header

    rows: list[list[InlineKeyboardButton]] = []

    # Color
    rows.append(
        [
            InlineKeyboardButton(
                _mark("Color", s, "color", "color"),
                callback_data="set:color:color",
            ),
            InlineKeyboardButton(
                _mark("B&W", s, "color", "bw"),
                callback_data="set:color:bw",
            ),
        ]
    )

    if not job["is_image"]:
        # Sides
        rows.append(
            [
                InlineKeyboardButton(
                    _mark("One-sided", s, "sides", "one"),
                    callback_data="set:sides:one",
                ),
                InlineKeyboardButton(
                    _mark("Long edge", s, "sides", "long"),
                    callback_data="set:sides:long",
                ),
                InlineKeyboardButton(
                    _mark("Short edge", s, "sides", "short"),
                    callback_data="set:sides:short",
                ),
            ]
        )

    # Orientation
    rows.append(
        [
            InlineKeyboardButton(
                _mark("Portrait", s, "orientation", "portrait"),
                callback_data="set:orientation:portrait",
            ),
            InlineKeyboardButton(
                _mark("Landscape", s, "orientation", "landscape"),
                callback_data="set:orientation:landscape",
            ),
        ]
    )

    # Pages per sheet (documents and images)
    rows.append(
        [
            InlineKeyboardButton(
                _mark(str(n), s, "nup", n),
                callback_data=f"set:nup:{n}",
            )
            for n in config.NUP_OPTIONS
        ]
    )

    if not job["is_image"]:
        # Page range (documents only)
        if s["page_range"] == "all":
            rows.append(
                [
                    InlineKeyboardButton(
                        "\u2713 All pages", callback_data="set:page_range:all"
                    ),
                    InlineKeyboardButton(
                        "Custom\u2026", callback_data="pr:custom"
                    ),
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        "All pages", callback_data="set:page_range:all"
                    ),
                    InlineKeyboardButton(
                        f"\u2713 {s['page_range']}",
                        callback_data="pr:custom",
                    ),
                ]
            )

    # Copies
    rows.append(
        [
            InlineKeyboardButton("\u2212", callback_data="set:copies:dec"),
            InlineKeyboardButton(f"  {s['copies']}  ", callback_data="noop"),
            InlineKeyboardButton("+", callback_data="set:copies:inc"),
        ]
    )

    # Actions
    rows.append(
        [
            InlineKeyboardButton(
                "\U0001f5a8 Print", callback_data="act:print"
            ),
            InlineKeyboardButton(
                "\u2716 Cancel", callback_data="act:cancel"
            ),
        ]
    )

    return text, InlineKeyboardMarkup(rows)


def _build_settings_summary(settings: dict, is_image: bool = False) -> str:
    parts = [
        "Color" if settings["color"] == "color" else "B\u200a&\u200aW",
    ]
    if not is_image:
        parts.append(
            {
                "one": "One-sided",
                "long": "Long edge",
                "short": "Short edge",
            }[settings["sides"]]
        )
    parts.append(settings["orientation"].title())
    parts.append(f"{settings['nup']}/sheet")
    if not is_image:
        parts.append(f"Pages: {settings['page_range']}")
    parts.append(
        f"{settings['copies']} copy"
        if settings["copies"] == 1
        else f"{settings['copies']} copies"
    )
    return " | ".join(parts)


def _build_collecting_message(batch: dict) -> str:
    """Build the batch collecting status message."""
    files = batch["files"]
    n = len(files)
    file_list = _build_batch_file_list(files)
    return f"\U0001f4e5 {n} file{'s' if n != 1 else ''} received:\n{file_list}\n\nSend more or tap Continue."


def _build_batch_file_list(files: list[dict]) -> str:
    """Build numbered file list for batch screen header."""
    lines = []
    for i, f in enumerate(files, 1):
        icon = "\U0001f5bc" if f["is_image"] else "\U0001f4c4"
        if f["is_image"]:
            lines.append(f"  {icon} {f['original_name']}")
        elif f["page_count"]:
            lines.append(
                f"  {icon} {f['original_name']} ({f['page_count']}p)"
            )
        else:
            lines.append(f"  {icon} {f['original_name']}")
    return "\n".join(lines)


def build_batch_settings_screen(
    batch: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build batch settings message and keyboard."""
    files = batch["files"]
    s = batch["global_settings"]
    has_docs = batch["has_documents"]

    file_count = len(files)
    header = f"\U0001f4e8 {file_count} files ready to print:\n{_build_batch_file_list(files)}"
    text = header

    rows: list[list[InlineKeyboardButton]] = []

    # Color
    rows.append(
        [
            InlineKeyboardButton(
                _mark("Color", s, "color", "color"),
                callback_data="bset:color:color",
            ),
            InlineKeyboardButton(
                _mark("B&W", s, "color", "bw"),
                callback_data="bset:color:bw",
            ),
        ]
    )

    if has_docs:
        # Sides
        rows.append(
            [
                InlineKeyboardButton(
                    _mark("One-sided", s, "sides", "one"),
                    callback_data="bset:sides:one",
                ),
                InlineKeyboardButton(
                    _mark("Long edge", s, "sides", "long"),
                    callback_data="bset:sides:long",
                ),
                InlineKeyboardButton(
                    _mark("Short edge", s, "sides", "short"),
                    callback_data="bset:sides:short",
                ),
            ]
        )

    # Orientation
    rows.append(
        [
            InlineKeyboardButton(
                _mark("Portrait", s, "orientation", "portrait"),
                callback_data="bset:orientation:portrait",
            ),
            InlineKeyboardButton(
                _mark("Landscape", s, "orientation", "landscape"),
                callback_data="bset:orientation:landscape",
            ),
        ]
    )

    # Pages per sheet (documents and images)
    rows.append(
        [
            InlineKeyboardButton(
                _mark(str(n), s, "nup", n),
                callback_data=f"bset:nup:{n}",
            )
            for n in config.NUP_OPTIONS
            ]
        )

    # Copies
    rows.append(
        [
            InlineKeyboardButton("\u2212", callback_data="bset:copies:dec"),
            InlineKeyboardButton(str(s["copies"]), callback_data="noop"),
            InlineKeyboardButton("+", callback_data="bset:copies:inc"),
        ]
    )

    # Per-file buttons (max 4 per row)
    file_buttons = []
    for i, f in enumerate(files):
        short_name = f["original_name"]
        if len(short_name) > 15:
            short_name = short_name[:12] + "\u2026"
        file_buttons.append(
            InlineKeyboardButton(
                f"{i + 1}. {short_name}", callback_data=f"bfile:{i}"
            )
        )
    for j in range(0, len(file_buttons), 3):
        rows.append(file_buttons[j : j + 3])

    # Actions
    rows.append(
        [
            InlineKeyboardButton(
                f"\U0001f5a8 Print All ({file_count})",
                callback_data="bact:print",
            ),
            InlineKeyboardButton("\u2716 Cancel", callback_data="bact:cancel"),
        ]
    )

    return text, InlineKeyboardMarkup(rows)


def build_batch_file_settings_screen(
    batch: dict, index: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Build per-file settings screen within a batch."""
    f = batch["files"][index]
    s = f["settings"]
    name = f["original_name"]

    if f["is_image"]:
        header = f"File {index + 1}: {name} \u2014 image"
    elif f["page_count"]:
        header = f"File {index + 1}: {name} \u2014 {f['page_count']} pages"
    else:
        header = f"File {index + 1}: {name}"

    text = header

    rows: list[list[InlineKeyboardButton]] = []
    prefix = f"bfset:{index}"

    # Color
    rows.append(
        [
            InlineKeyboardButton(
                _mark("Color", s, "color", "color"),
                callback_data=f"{prefix}:color:color",
            ),
            InlineKeyboardButton(
                _mark("B&W", s, "color", "bw"),
                callback_data=f"{prefix}:color:bw",
            ),
        ]
    )

    if not f["is_image"]:
        rows.append(
            [
                InlineKeyboardButton(
                    _mark("One-sided", s, "sides", "one"),
                    callback_data=f"{prefix}:sides:one",
                ),
                InlineKeyboardButton(
                    _mark("Long edge", s, "sides", "long"),
                    callback_data=f"{prefix}:sides:long",
                ),
                InlineKeyboardButton(
                    _mark("Short edge", s, "sides", "short"),
                    callback_data=f"{prefix}:sides:short",
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                _mark("Portrait", s, "orientation", "portrait"),
                callback_data=f"{prefix}:orientation:portrait",
            ),
            InlineKeyboardButton(
                _mark("Landscape", s, "orientation", "landscape"),
                callback_data=f"{prefix}:orientation:landscape",
            ),
        ]
    )

    # Pages per sheet (documents and images)
    rows.append(
        [
            InlineKeyboardButton(
                _mark(str(n), s, "nup", n),
                callback_data=f"{prefix}:nup:{n}",
            )
            for n in config.NUP_OPTIONS
        ]
    )

    if not f["is_image"]:
        if s["page_range"] == "all":
            rows.append(
                [
                    InlineKeyboardButton(
                        "\u2713 All",
                        callback_data=f"{prefix}:page_range:all",
                    ),
                    InlineKeyboardButton(
                        "Custom\u2026",
                        callback_data=f"bpr:custom:{index}",
                    ),
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        "All",
                        callback_data=f"{prefix}:page_range:all",
                    ),
                    InlineKeyboardButton(
                        f"\u2713 Pages: {s['page_range']}",
                        callback_data=f"bpr:custom:{index}",
                    ),
                ]
            )

    rows.append(
        [
            InlineKeyboardButton(
                "\u2212", callback_data=f"{prefix}:copies:dec"
            ),
            InlineKeyboardButton(str(s["copies"]), callback_data="noop"),
            InlineKeyboardButton(
                "+", callback_data=f"{prefix}:copies:inc"
            ),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                "\u2b05 Back to all files", callback_data="bfile:back"
            )
        ]
    )

    return text, InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Page range validation
# ---------------------------------------------------------------------------

def _validate_page_range(text: str, total_pages: int | None) -> str | None:
    cleaned = text.replace(" ", "")
    if not cleaned:
        return "Empty page range."

    if not re.match(r"^[\d,\-]+$", cleaned):
        return "Invalid characters. Use e.g. 1-3, 5, 8-10"

    segments = [s for s in cleaned.split(",") if s]
    if not segments:
        return "Empty page range."

    max_page = 0
    for segment in segments:
        if "-" in segment:
            parts = segment.split("-")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return f"Invalid range: {segment}"
            try:
                start, end = int(parts[0]), int(parts[1])
            except ValueError:
                return f"Invalid range: {segment}"
            if start < 1:
                return f"Page numbers start at 1, got {start}."
            if end < 1:
                return f"Page numbers start at 1, got {end}."
            if start > end:
                return f"Invalid range {start}-{end}: start is bigger than end."
            max_page = max(max_page, end)
        else:
            try:
                page = int(segment)
            except ValueError:
                return f"Invalid page: {segment}"
            if page < 1:
                return f"Page numbers start at 1, got {page}."
            max_page = max(max_page, page)

    if total_pages and max_page > total_pages:
        return (
            f"Document only has {total_pages} pages, "
            f"but you requested up to page {max_page}."
        )

    return None


# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------

async def _process_voice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> gemini.ParsedInstruction | None:
    """Download, transcribe, parse a voice note. Returns ParsedInstruction or None on error."""
    voice = update.message.voice

    if not config.GEMINI_API_KEY:
        await update.message.reply_text(
            "Voice notes are not configured. Send a file and use buttons."
        )
        return None

    if voice.duration and voice.duration > config.MAX_VOICE_DURATION:
        await update.message.reply_text(
            f"Voice note too long. Keep it under {config.MAX_VOICE_DURATION} seconds."
        )
        return None

    try:
        tg_file = await voice.get_file()
        local_path = (
            config.TEMP_DIR
            / f"{update.effective_user.id}_{voice.file_unique_id}.ogg"
        )
        await tg_file.download_to_drive(local_path)
    except Exception as e:
        logger.error("Voice download failed: %s", e)
        await update.message.reply_text(
            "Couldn't download voice note. Check your connection and try again."
        )
        return None

    msg = await update.message.reply_text("Listening\u2026")

    try:
        transcript = await gemini.transcribe_voice(local_path)
        parsed = await gemini.parse_print_instruction(transcript)
    except Exception as e:
        logger.error("Gemini error: %s", e)
        await msg.edit_text(
            "Couldn't understand the voice note. Try again or use buttons."
        )
        return None
    finally:
        converter.cleanup_temp_files(local_path)

    # Build response showing what was understood
    response = f'\U0001f399 "{parsed.transcript}"'

    # Show extracted settings
    extracted = []
    if parsed.color:
        extracted.append("Color" if parsed.color == "color" else "B&W")
    if parsed.sides:
        extracted.append({"one": "One-sided", "long": "Both sides", "short": "Both sides (short)"}[parsed.sides])
    if parsed.orientation:
        extracted.append(parsed.orientation.title())
    if parsed.nup and parsed.nup != 1:
        extracted.append(f"{parsed.nup} per sheet")
    if parsed.copies and parsed.copies != 1:
        extracted.append(f"{parsed.copies} copies")
    if parsed.page_range:
        extracted.append(f"Pages: {parsed.page_range}")

    if extracted:
        response += f"\n\nSettings: {', '.join(extracted)}"

    if parsed.clarification:
        response += f"\n\n{parsed.clarification}"

    await msg.edit_text(response)
    return parsed


# ---------------------------------------------------------------------------
# File handlers (conversation entry points)
# ---------------------------------------------------------------------------

@authorized
async def handle_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle incoming document — enters batch collecting mode."""
    doc = update.message.document
    file_name = doc.file_name or "document"
    ext = Path(file_name).suffix.lower()

    if ext not in config.SUPPORTED_EXTENSIONS:
        await update.message.reply_text(
            f"Can't print {ext} files.\n"
            "Supported: PDF, DOCX, PPTX, JPG, PNG, GIF, BMP, TIFF, WEBP"
        )
        return ConversationHandler.END

    tg_file = await doc.get_file()
    msg = await update.message.reply_text(f"Received {file_name}\u2026")

    result = await _prepare_file(
        tg_file, file_name, ext, update.effective_user.id,
        doc.file_unique_id, status_msg=msg,
    )
    if isinstance(result, str):
        await msg.edit_text(result)
        return ConversationHandler.END

    file_info = result

    # Apply pending voice instruction if any
    voice = context.user_data.pop("voice_instruction", None)
    if voice:
        gemini.apply_parsed_to_settings(voice, file_info["settings"])

    # Initialize batch
    batch = context.user_data.get("batch")
    if batch:
        # Already collecting — append
        batch["files"].append(file_info)
        if not file_info["is_image"]:
            batch["has_documents"] = True
        else:
            batch["has_images"] = True
        n = len(batch["files"])
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"\u27a1 Continue ({n} files)",
                        callback_data="batch:done",
                    )
                ]
            ]
        )
        await msg.edit_text(
            _build_collecting_message(batch),
            reply_markup=keyboard,
        )
        batch["status_message_id"] = msg.message_id
        return BATCH_COLLECTING

    # Start new batch
    context.user_data["batch"] = {
        "files": [file_info],
        "global_settings": dict(file_info["settings"]),
        "status_message_id": msg.message_id,
        "has_documents": not file_info["is_image"],
        "has_images": file_info["is_image"],
    }
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\u27a1 Continue (1 file)", callback_data="batch:done"
                )
            ]
        ]
    )
    await msg.edit_text(
        _build_collecting_message(context.user_data["batch"]),
        reply_markup=keyboard,
    )
    return BATCH_COLLECTING


@authorized
async def handle_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle incoming photo — enters batch collecting mode."""
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    file_name = f"photo_{photo.file_unique_id}.jpg"

    local_path = (
        config.TEMP_DIR
        / f"{update.effective_user.id}_{photo.file_unique_id}.jpg"
    )
    await tg_file.download_to_drive(local_path)

    file_info = {
        "file_path": local_path,
        "pdf_path": None,
        "original_name": file_name,
        "is_image": True,
        "page_count": None,
        "settings": dict(config.DEFAULT_SETTINGS),
    }

    # Apply pending voice instruction
    voice = context.user_data.pop("voice_instruction", None)
    if voice:
        gemini.apply_parsed_to_settings(voice, file_info["settings"])

    batch = context.user_data.get("batch")
    if batch:
        batch["files"].append(file_info)
        batch["has_images"] = True
        n = len(batch["files"])
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Continue ({n} files)",
                        callback_data="batch:done",
                    )
                ]
            ]
        )
        msg = await update.message.reply_text(
            f"{n} files received. Send more or tap Continue.",
            reply_markup=keyboard,
        )
        batch["status_message_id"] = msg.message_id
        return BATCH_COLLECTING

    context.user_data["batch"] = {
        "files": [file_info],
        "global_settings": dict(file_info["settings"]),
        "status_message_id": None,
        "has_documents": False,
        "has_images": True,
    }
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\u27a1 Continue (1 file)", callback_data="batch:done"
                )
            ]
        ]
    )
    msg = await update.message.reply_text(
        _build_collecting_message(context.user_data["batch"]),
        reply_markup=keyboard,
    )
    context.user_data["batch"]["status_message_id"] = msg.message_id
    return BATCH_COLLECTING


# ---------------------------------------------------------------------------
# Voice handlers
# ---------------------------------------------------------------------------

@authorized
async def handle_voice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Voice note as entry point — no file yet."""
    parsed = await _process_voice(update, context)
    if parsed is None:
        return ConversationHandler.END

    context.user_data["voice_instruction"] = parsed

    await update.message.reply_text("Now send me the file to print.")
    return VOICE_PENDING


@authorized
async def handle_voice_in_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Voice note while viewing single-file settings."""
    parsed = await _process_voice(update, context)
    if parsed is None:
        return SETTINGS

    job = context.user_data.get("job")
    if not job:
        return ConversationHandler.END

    gemini.apply_parsed_to_settings(parsed, job["settings"])

    text, keyboard = build_settings_screen(job)
    msg = await update.message.reply_text(text, reply_markup=keyboard)
    job["message_id"] = msg.message_id
    return SETTINGS


async def handle_voice_in_batch(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Voice note during batch collection — applies to global settings."""
    parsed = await _process_voice(update, context)
    if parsed is None:
        return BATCH_COLLECTING

    batch = context.user_data.get("batch")
    if batch:
        gemini.apply_parsed_to_settings(parsed, batch["global_settings"])
        # Also update each file's settings
        for f in batch["files"]:
            gemini.apply_parsed_to_settings(parsed, f["settings"])

    return BATCH_COLLECTING


async def handle_voice_in_batch_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Voice note while viewing batch settings screen."""
    parsed = await _process_voice(update, context)
    if parsed is None:
        return BATCH_SETTINGS

    batch = context.user_data.get("batch")
    if not batch:
        return ConversationHandler.END

    gemini.apply_parsed_to_settings(parsed, batch["global_settings"])
    for f in batch["files"]:
        gemini.apply_parsed_to_settings(parsed, f["settings"])

    text, keyboard = build_batch_settings_screen(batch)
    msg = await update.message.reply_text(text, reply_markup=keyboard)
    return BATCH_SETTINGS


# ---------------------------------------------------------------------------
# Batch handlers
# ---------------------------------------------------------------------------

async def handle_batch_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Additional document during batch collection."""
    doc = update.message.document
    file_name = doc.file_name or "document"
    ext = Path(file_name).suffix.lower()

    if ext not in config.SUPPORTED_EXTENSIONS:
        await update.message.reply_text(
            f"Can't print {ext} files. Skipped."
        )
        return BATCH_COLLECTING

    tg_file = await doc.get_file()
    msg = await update.message.reply_text(f"Processing {file_name}\u2026")

    result = await _prepare_file(
        tg_file, file_name, ext, update.effective_user.id,
        doc.file_unique_id, status_msg=msg,
    )
    if isinstance(result, str):
        await msg.edit_text(f"{file_name}: {result}")
        return BATCH_COLLECTING

    batch = context.user_data.get("batch")

    if not batch:
        # No batch yet — start one
        context.user_data["batch"] = {
            "files": [result],
            "global_settings": dict(result["settings"]),
            "status_message_id": None,
            "has_documents": not result["is_image"],
            "has_images": result["is_image"],
        }
        batch = context.user_data["batch"]
    else:
        result["settings"] = dict(batch["global_settings"])
        batch["files"].append(result)
        if not result["is_image"]:
            batch["has_documents"] = True
        else:
            batch["has_images"] = True

    n = len(batch["files"])
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"\u27a1 Continue ({n} file{'s' if n != 1 else ''})",
                    callback_data="batch:done",
                )
            ]
        ]
    )
    await msg.edit_text(
        _build_collecting_message(batch),
        reply_markup=keyboard,
    )
    batch["status_message_id"] = msg.message_id
    return BATCH_COLLECTING


async def handle_batch_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Additional photo during batch collection."""
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    file_name = f"photo_{photo.file_unique_id}.jpg"

    local_path = (
        config.TEMP_DIR
        / f"{update.effective_user.id}_{photo.file_unique_id}.jpg"
    )
    await tg_file.download_to_drive(local_path)

    batch = context.user_data.get("batch")
    file_info = {
        "file_path": local_path,
        "pdf_path": None,
        "original_name": file_name,
        "is_image": True,
        "page_count": None,
        "settings": dict(
            batch["global_settings"] if batch else config.DEFAULT_SETTINGS
        ),
    }

    if not batch:
        # No batch yet — start one
        context.user_data["batch"] = {
            "files": [file_info],
            "global_settings": dict(config.DEFAULT_SETTINGS),
            "status_message_id": None,
            "has_documents": False,
            "has_images": True,
        }
        batch = context.user_data["batch"]
    else:
        batch["files"].append(file_info)
        batch["has_images"] = True

    n = len(batch["files"])
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"\u27a1 Continue ({n} file{'s' if n != 1 else ''})",
                    callback_data="batch:done",
                )
            ]
        ]
    )
    msg = await update.message.reply_text(
        _build_collecting_message(batch),
        reply_markup=keyboard,
    )
    batch["status_message_id"] = msg.message_id
    return BATCH_COLLECTING


async def handle_batch_done(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped Continue — show settings."""
    query = update.callback_query
    await query.answer()

    batch = context.user_data.get("batch")
    if not batch or not batch["files"]:
        await query.edit_message_text("No files to print.")
        return ConversationHandler.END

    if len(batch["files"]) == 1:
        # Single file — switch to normal flow
        job = batch["files"][0]
        job["message_id"] = None
        job["cups_job_id"] = None
        context.user_data["job"] = job
        context.user_data.pop("batch", None)

        text, keyboard = build_settings_screen(job)
        await query.edit_message_text(text, reply_markup=keyboard)
        job["message_id"] = query.message.message_id
        return SETTINGS

    # Multiple files — batch settings
    text, keyboard = build_batch_settings_screen(batch)
    await query.edit_message_text(text, reply_markup=keyboard)
    return BATCH_SETTINGS


async def handle_batch_setting_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Toggle a global batch setting."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return BATCH_SETTINGS

    _, field, value = parts
    batch = context.user_data.get("batch")
    if not batch:
        return ConversationHandler.END

    s = batch["global_settings"]

    if field == "copies":
        if value == "inc":
            s["copies"] = min(s["copies"] + 1, 99)
        elif value == "dec":
            s["copies"] = max(s["copies"] - 1, 1)
    elif field == "nup":
        nup_val = int(value)
        if nup_val in _VALID_NUP:
            s["nup"] = nup_val
    elif field in _VALID_SETTINGS:
        if value in _VALID_SETTINGS[field]:
            s[field] = value

    # Propagate to all files
    for f in batch["files"]:
        f["settings"] = dict(s)

    text, keyboard = build_batch_settings_screen(batch)
    await query.edit_message_text(text, reply_markup=keyboard)
    return BATCH_SETTINGS


async def handle_batch_file_expand(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Show per-file settings for a specific file."""
    query = update.callback_query
    await query.answer()

    index = int(query.data.split(":")[1])
    batch = context.user_data.get("batch")
    if not batch or index >= len(batch["files"]):
        return BATCH_SETTINGS

    text, keyboard = build_batch_file_settings_screen(batch, index)
    await query.edit_message_text(text, reply_markup=keyboard)
    return BATCH_SETTINGS


async def handle_batch_file_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Return from per-file view to batch overview."""
    query = update.callback_query
    await query.answer()

    batch = context.user_data.get("batch")
    if not batch:
        return ConversationHandler.END

    text, keyboard = build_batch_settings_screen(batch)
    await query.edit_message_text(text, reply_markup=keyboard)
    return BATCH_SETTINGS


async def handle_batch_file_setting_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Toggle a per-file setting within batch."""
    query = update.callback_query
    await query.answer()

    # bfset:{index}:{field}:{value}
    parts = query.data.split(":")
    if len(parts) != 4:
        return BATCH_SETTINGS

    _, index_str, field, value = parts
    index = int(index_str)
    batch = context.user_data.get("batch")
    if not batch or index >= len(batch["files"]):
        return BATCH_SETTINGS

    s = batch["files"][index]["settings"]

    if field == "copies":
        if value == "inc":
            s["copies"] = min(s["copies"] + 1, 99)
        elif value == "dec":
            s["copies"] = max(s["copies"] - 1, 1)
    elif field == "nup":
        nup_val = int(value)
        if nup_val in _VALID_NUP:
            s["nup"] = nup_val
    elif field == "page_range":
        if value == "all":
            s["page_range"] = value
    elif field in _VALID_SETTINGS:
        if value in _VALID_SETTINGS[field]:
            s[field] = value

    text, keyboard = build_batch_file_settings_screen(batch, index)
    await query.edit_message_text(text, reply_markup=keyboard)
    return BATCH_SETTINGS


async def prompt_batch_page_range(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Prompt for custom page range for a batch file."""
    query = update.callback_query
    await query.answer()

    index = int(query.data.split(":")[2])
    context.user_data["batch_pr_index"] = index

    f = context.user_data["batch"]["files"][index]
    await query.edit_message_text(
        f"Page range for {f['original_name']} (e.g. 1-3, 5, 8-10):"
    )
    return BATCH_PAGE_RANGE


async def handle_batch_page_range_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive page range text for a batch file."""
    text = update.message.text.strip()
    batch = context.user_data.get("batch")
    index = context.user_data.get("batch_pr_index", 0)

    if not batch or index >= len(batch["files"]):
        return ConversationHandler.END

    f = batch["files"][index]
    error = _validate_page_range(text, f.get("page_count"))
    if error:
        await update.message.reply_text(
            f"{error}\nTry again (e.g. 1-3, 5, 8-10):"
        )
        return BATCH_PAGE_RANGE

    f["settings"]["page_range"] = text.replace(" ", "")

    txt, keyboard = build_batch_file_settings_screen(batch, index)
    await update.message.reply_text(txt, reply_markup=keyboard)
    return BATCH_SETTINGS


async def handle_batch_print(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Print all files in the batch."""
    query = update.callback_query
    await query.answer()

    batch = context.user_data.get("batch")
    if not batch or not batch["files"]:
        await query.edit_message_text("No files to print.")
        return ConversationHandler.END

    # Validate all page ranges
    for f in batch["files"]:
        if f["settings"]["page_range"] != "all" and not f["is_image"]:
            error = _validate_page_range(
                f["settings"]["page_range"], f.get("page_count")
            )
            if error:
                await query.answer(
                    f"{f['original_name']}: {error}", show_alert=True
                )
                return BATCH_SETTINGS

    # Submit all jobs
    lines = []
    for f in batch["files"]:
        print_path = f.get("pdf_path") or f["file_path"]

        if not Path(print_path).exists() or Path(print_path).stat().st_size == 0:
            lines.append(f"{f['original_name']}: file missing, skipped")
            continue

        try:
            job_id = await printer.async_submit_job(
                print_path,
                f["original_name"],
                f["settings"],
                is_image=f.get("is_image", False),
            )
        except Exception as e:
            lines.append(f"{f['original_name']}: failed ({e})")
            continue

        lines.append(f"#{job_id} \u2014 {f['original_name']}")
        summary = _build_settings_summary(f["settings"], is_image=f.get("is_image", False))

        context.bot_data.setdefault("active_jobs", {})[job_id] = {
            "chat_id": update.effective_chat.id,
            "message_id": query.message.message_id,
            "original_name": f["original_name"],
            "summary": summary,
            "user_id": update.effective_user.id,
            "file_path": str(f["file_path"]),
            "pdf_path": str(f["pdf_path"]) if f.get("pdf_path") else None,
            "settings": dict(f["settings"]),
            "is_image": f.get("is_image", False),
            "last_state": None,
        }

    await query.edit_message_text(
        f"Printing {len(batch['files'])} files:\n" + "\n".join(lines)
    )
    context.user_data.pop("batch", None)
    return ConversationHandler.END


async def handle_batch_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel entire batch."""
    query = update.callback_query
    await query.answer()

    batch = context.user_data.pop("batch", None)
    if batch:
        for f in batch["files"]:
            paths = [f["file_path"]]
            if f.get("pdf_path"):
                paths.append(f["pdf_path"])
            converter.cleanup_temp_files(*paths)

    context.user_data.pop("batch_pr_index", None)
    await query.edit_message_text(
        "Cancelled. Send another file anytime."
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Single-file settings handlers
# ---------------------------------------------------------------------------

async def handle_setting_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return SETTINGS

    _, field, value = parts
    job = context.user_data.get("job")
    if not job:
        return ConversationHandler.END

    s = job["settings"]

    if field == "copies":
        if value == "inc":
            s["copies"] = min(s["copies"] + 1, 99)
        elif value == "dec":
            s["copies"] = max(s["copies"] - 1, 1)
    elif field == "nup":
        nup_val = int(value)
        if nup_val in _VALID_NUP:
            s["nup"] = nup_val
    elif field == "page_range":
        if value == "all":
            s["page_range"] = value
    elif field in _VALID_SETTINGS:
        if value in _VALID_SETTINGS[field]:
            s[field] = value

    text, keyboard = build_settings_screen(job)
    await query.edit_message_text(text, reply_markup=keyboard)
    return SETTINGS


async def prompt_page_range(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Type page range (e.g. 1-3, 5, 8-10):"
    )
    return PAGE_RANGE


async def handle_page_range_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text.strip()

    job = context.user_data.get("job")
    if not job:
        return ConversationHandler.END

    error = _validate_page_range(text, job.get("page_count"))
    if error:
        await update.message.reply_text(
            f"{error}\nTry again (e.g. 1-3, 5, 8-10):"
        )
        return PAGE_RANGE

    job["settings"]["page_range"] = text.replace(" ", "")
    msg_text, keyboard = build_settings_screen(job)
    msg = await update.message.reply_text(msg_text, reply_markup=keyboard)
    job["message_id"] = msg.message_id
    return SETTINGS


# ---------------------------------------------------------------------------
# Print action (single file)
# ---------------------------------------------------------------------------

async def handle_print(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    job = context.user_data.get("job")
    if not job:
        await query.edit_message_text("No file to print.")
        return ConversationHandler.END

    s = job["settings"]
    if s["page_range"] != "all":
        error = _validate_page_range(s["page_range"], job.get("page_count"))
        if error:
            await query.answer(error, show_alert=True)
            return SETTINGS

    print_path = job.get("pdf_path") or job["file_path"]

    if not Path(print_path).exists() or Path(print_path).stat().st_size == 0:
        await query.edit_message_text("File is empty or missing.")
        return ConversationHandler.END

    summary = _build_settings_summary(job["settings"], is_image=job.get("is_image", False))

    try:
        job_id = await printer.async_submit_job(
            print_path,
            job["original_name"],
            job["settings"],
            is_image=job.get("is_image", False),
        )
    except Exception as e:
        logger.error("Print submission failed: %s", e)
        await query.edit_message_text(f"Print failed: {e}")
        return ConversationHandler.END

    job["cups_job_id"] = job_id

    status_text = (
        f"{job['original_name']} \u2014 Job #{job_id}\n"
        f"{summary}\n\n"
        "Status: Queued\u2026"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Cancel Job", callback_data=f"job:cancel:{job_id}"
                )
            ]
        ]
    )
    await query.edit_message_text(status_text, reply_markup=keyboard)

    context.bot_data.setdefault("active_jobs", {})[job_id] = {
        "chat_id": update.effective_chat.id,
        "message_id": query.message.message_id,
        "original_name": job["original_name"],
        "summary": summary,
        "user_id": update.effective_user.id,
        "file_path": str(job["file_path"]),
        "pdf_path": str(job["pdf_path"]) if job.get("pdf_path") else None,
        "settings": dict(job["settings"]),
        "is_image": job.get("is_image", False),
        "last_state": None,
    }

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Cancel from settings (single file)
# ---------------------------------------------------------------------------

async def handle_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    job = context.user_data.pop("job", None)
    if job:
        paths = [job["file_path"]]
        if job.get("pdf_path"):
            paths.append(job["pdf_path"])
        converter.cleanup_temp_files(*paths)

    await query.edit_message_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Main menu handlers (outside conversation)
# ---------------------------------------------------------------------------

@authorized
async def handle_printer_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    try:
        status = await printer.async_get_status()
    except Exception as e:
        await query.edit_message_text(f"Cannot reach printer: {e}")
        return

    online_icon = "\u2705" if status.is_online else "\u274c"
    lines = [
        f"\U0001f5a8 {status.name}",
        f"{online_icon} {'Online' if status.is_online else 'OFFLINE'} \u2014 {status.state}",
    ]
    if status.state_message:
        lines.append(f"{status.state_message}")

    if status.ink_levels:
        lines.append("")
        for name, level in status.ink_levels.items():
            if level > 50:
                bar = "\U0001f7e9"
            elif level > 15:
                bar = "\U0001f7e8"
            else:
                bar = "\U0001f7e5"
            lines.append(f"  {bar} {name}: {level}%")

    back_kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\U0001f4cb Print Queue", callback_data="main:queue"
                ),
                InlineKeyboardButton(
                    "\u2b05 Back", callback_data="main:back"
                ),
            ]
        ]
    )
    await query.edit_message_text(
        "\n".join(lines), reply_markup=back_kb
    )


@authorized
async def handle_print_queue(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    try:
        jobs = await printer.async_get_all_jobs()
    except Exception as e:
        await query.edit_message_text(f"Cannot reach CUPS: {e}")
        return

    if not jobs:
        back_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "\U0001f5a8 Printer Status",
                        callback_data="main:status",
                    ),
                    InlineKeyboardButton(
                        "\u2b05 Back", callback_data="main:back"
                    ),
                ]
            ]
        )
        await query.edit_message_text(
            "\U0001f4cb Print queue is empty.",
            reply_markup=back_kb,
        )
        return

    # State emoji mapping
    state_icons = {
        "Queued": "\u23f3",
        "Printing": "\U0001f504",
        "Done": "\u2705",
        "Failed": "\u274c",
        "Cancelled": "\u2716",
    }

    lines = ["\U0001f4cb Print Queue:"]
    buttons: list[InlineKeyboardButton] = []
    for j in jobs:
        icon = state_icons.get(j.state_text, "\u2022")
        lines.append(
            f"  {icon} #{j.job_id} \u2014 {j.title} \u2014 {j.state_text}"
        )
        buttons.append(
            InlineKeyboardButton(
                f"\u2716 #{j.job_id}",
                callback_data=f"q:cancel:{j.job_id}",
            )
        )

    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton(
                "\u2716 Cancel All", callback_data="q:cancelall"
            ),
            InlineKeyboardButton(
                "\u2b05 Back", callback_data="main:back"
            ),
        ]
    )

    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
    )


@authorized
async def handle_job_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    job_id = int(query.data.rsplit(":", 1)[1])
    await printer.async_cancel_job(job_id)
    context.bot_data.get("active_jobs", {}).pop(job_id, None)

    await query.edit_message_text(f"Job #{job_id} cancelled.")


@authorized
async def handle_cancel_all(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    count = await printer.async_cancel_all_jobs()
    context.bot_data["active_jobs"] = {}

    await query.edit_message_text(f"Cancelled {count} job(s).")


async def handle_main_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Return to the main welcome screen."""
    query = update.callback_query
    await query.answer()

    lines = [
        "Welcome to PrintBot!",
        "",
        "Send me a file or photo and I'll print it.",
    ]
    if config.GEMINI_API_KEY:
        lines.append("You can also send a voice note with instructions.")
    lines += [
        "",
        "Supported formats:",
        "PDF, DOCX, PPTX, JPG, PNG, GIF, BMP, TIFF, WEBP",
    ]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\U0001f5a8 Printer Status", callback_data="main:status"
                ),
                InlineKeyboardButton(
                    "\U0001f4cb Print Queue", callback_data="main:queue"
                ),
            ]
        ]
    )
    await query.edit_message_text(
        "\n".join(lines), reply_markup=keyboard
    )


@authorized
async def handle_retry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    job_id = int(query.data.rsplit(":", 1)[1])
    failed = context.bot_data.get("failed_jobs", {}).get(job_id)

    if not failed:
        await query.edit_message_text("Job info no longer available.")
        return

    print_path = failed.get("pdf_path") or failed["file_path"]

    try:
        new_id = await printer.async_submit_job(
            Path(print_path),
            failed["original_name"],
            failed["settings"],
            is_image=failed.get("is_image", False),
        )
    except Exception as e:
        await query.edit_message_text(f"Retry failed: {e}")
        return

    context.bot_data.setdefault("active_jobs", {})[new_id] = {
        **failed,
        "last_state": None,
    }
    context.bot_data["failed_jobs"].pop(job_id, None)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Cancel Job", callback_data=f"job:cancel:{new_id}"
                )
            ]
        ]
    )
    await query.edit_message_text(
        f"Resubmitted as Job #{new_id}\n"
        f"{failed['summary']}\n\n"
        "Status: Queued\u2026",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Background CUPS polling
# ---------------------------------------------------------------------------

async def poll_cups_status(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    active_jobs: dict = context.bot_data.get("active_jobs", {})

    finished_ids: list[int] = []

    for job_id, info in list(active_jobs.items()):
        try:
            job_info = await printer.async_get_job_info(job_id)
        except Exception:
            continue

        if job_info is None:
            finished_ids.append(job_id)
            continue

        current_state = job_info.state
        if current_state == info.get("last_state"):
            continue

        info["last_state"] = current_state

        if current_state == printer.JOB_PROCESSING:
            progress = ""
            if job_info.pages_completed and job_info.total_pages:
                progress = (
                    f" (page {job_info.pages_completed}"
                    f" of {job_info.total_pages})"
                )
            status_line = f"Status: Printing\u2026{progress}"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Cancel Job",
                            callback_data=f"job:cancel:{job_id}",
                        )
                    ]
                ]
            )

        elif current_state == printer.JOB_COMPLETED:
            status_line = "Status: Done! \u2705"
            keyboard = None
            finished_ids.append(job_id)
            try:
                await context.bot.send_message(
                    info["chat_id"],
                    f"\u2705 {info['original_name']} printed successfully!",
                )
            except Exception:
                pass

        elif current_state == printer.JOB_ABORTED:
            status_line = "Status: Failed \u274c"
            retry_kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Retry",
                            callback_data=f"job:retry:{job_id}",
                        )
                    ]
                ]
            )
            keyboard = retry_kb
            finished_ids.append(job_id)
            context.bot_data.setdefault("failed_jobs", {})[job_id] = info
            try:
                await context.bot.send_message(
                    info["chat_id"],
                    f"Job #{job_id} ({info['original_name']}) failed.\n"
                    "Tap Retry to resubmit.",
                    reply_markup=retry_kb,
                )
            except Exception:
                pass

        elif current_state == printer.JOB_CANCELLED:
            status_line = "Status: Cancelled"
            keyboard = None
            finished_ids.append(job_id)

        elif current_state == printer.JOB_PENDING:
            status_line = "Status: Queued\u2026"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Cancel Job",
                            callback_data=f"job:cancel:{job_id}",
                        )
                    ]
                ]
            )

        else:
            status_line = f"Status: {job_info.state_text}"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Cancel Job",
                            callback_data=f"job:cancel:{job_id}",
                        )
                    ]
                ]
            )

        full_text = (
            f"{info['original_name']} \u2014 Job #{job_id}\n"
            f"{info['summary']}\n\n"
            f"{status_line}"
        )

        try:
            await context.bot.edit_message_text(
                full_text,
                chat_id=info["chat_id"],
                message_id=info["message_id"],
                reply_markup=keyboard,
            )
        except Exception:
            pass

    for job_id in finished_ids:
        info = active_jobs.pop(job_id, None)
        if info and info.get("last_state") == printer.JOB_COMPLETED:
            paths = [Path(info["file_path"])]
            if info.get("pdf_path"):
                paths.append(Path(info["pdf_path"]))
            converter.cleanup_temp_files(*paths)

    # Printer state monitoring
    try:
        status = await printer.async_get_status()
    except Exception:
        return

    prev_online = context.bot_data.get("printer_online", True)

    if status.is_online and not prev_online:
        for uid in config.ALLOWED_USER_IDS:
            try:
                await context.bot.send_message(
                    uid, "Printer is back online."
                )
            except Exception:
                pass
    elif not status.is_online and prev_online:
        for uid in config.ALLOWED_USER_IDS:
            try:
                await context.bot.send_message(
                    uid,
                    "Printer went offline. Check USB connection.",
                )
            except Exception:
                pass

    context.bot_data["printer_online"] = status.is_online

    # Ink level warnings
    if status.ink_levels:
        for color, level in status.ink_levels.items():
            key = f"ink_warned_{color}"
            if level < 15 and not context.bot_data.get(key):
                context.bot_data[key] = True
                for uid in config.ALLOWED_USER_IDS:
                    try:
                        await context.bot.send_message(
                            uid,
                            f"Low ink warning: {color} at {level}%",
                        )
                    except Exception:
                        pass
            elif level >= 15:
                context.bot_data.pop(key, None)


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

def main() -> None:
    application = Application.builder().token(config.BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Document.ALL, handle_document),
            MessageHandler(filters.PHOTO, handle_photo),
            MessageHandler(filters.VOICE, handle_voice),
        ],
        states={
            SETTINGS: [
                CallbackQueryHandler(
                    handle_setting_toggle, pattern=r"^set:"
                ),
                CallbackQueryHandler(
                    prompt_page_range, pattern=r"^pr:custom$"
                ),
                CallbackQueryHandler(
                    handle_print, pattern=r"^act:print$"
                ),
                CallbackQueryHandler(
                    handle_cancel, pattern=r"^act:cancel$"
                ),
                MessageHandler(
                    filters.VOICE, handle_voice_in_settings
                ),
            ],
            PAGE_RANGE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_page_range_input,
                ),
                CallbackQueryHandler(
                    handle_cancel, pattern=r"^act:cancel$"
                ),
            ],
            VOICE_PENDING: [
                MessageHandler(
                    filters.Document.ALL, handle_document
                ),
                MessageHandler(filters.PHOTO, handle_photo),
                MessageHandler(filters.VOICE, handle_voice),
            ],
            BATCH_COLLECTING: [
                MessageHandler(
                    filters.Document.ALL, handle_batch_file
                ),
                MessageHandler(filters.PHOTO, handle_batch_photo),
                MessageHandler(
                    filters.VOICE, handle_voice_in_batch
                ),
                CallbackQueryHandler(
                    handle_batch_done, pattern=r"^batch:done$"
                ),
            ],
            BATCH_SETTINGS: [
                CallbackQueryHandler(
                    handle_batch_setting_toggle, pattern=r"^bset:"
                ),
                CallbackQueryHandler(
                    handle_batch_file_setting_toggle,
                    pattern=r"^bfset:",
                ),
                CallbackQueryHandler(
                    handle_batch_file_expand, pattern=r"^bfile:\d+$"
                ),
                CallbackQueryHandler(
                    handle_batch_file_back, pattern=r"^bfile:back$"
                ),
                CallbackQueryHandler(
                    prompt_batch_page_range,
                    pattern=r"^bpr:custom:\d+$",
                ),
                CallbackQueryHandler(
                    handle_batch_print, pattern=r"^bact:print$"
                ),
                CallbackQueryHandler(
                    handle_batch_cancel, pattern=r"^bact:cancel$"
                ),
                MessageHandler(
                    filters.VOICE, handle_voice_in_batch_settings
                ),
            ],
            BATCH_PAGE_RANGE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_batch_page_range_input,
                ),
                CallbackQueryHandler(
                    handle_batch_cancel, pattern=r"^bact:cancel$"
                ),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
        ],
        per_user=True,
        per_chat=True,
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(conv_handler)

    # Main menu buttons
    application.add_handler(
        CallbackQueryHandler(
            handle_printer_status, pattern=r"^main:status$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_print_queue, pattern=r"^main:queue$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_main_back, pattern=r"^main:back$"
        )
    )

    # Job control
    application.add_handler(
        CallbackQueryHandler(
            handle_job_cancel, pattern=r"^job:cancel:\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_job_cancel, pattern=r"^q:cancel:\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_cancel_all, pattern=r"^q:cancelall$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(handle_retry, pattern=r"^job:retry:\d+$")
    )

    # Noop for copies display button
    application.add_handler(
        CallbackQueryHandler(
            lambda update, ctx: update.callback_query.answer(),
            pattern=r"^noop$",
        )
    )

    # Background CUPS polling
    application.job_queue.run_repeating(
        poll_cups_status,
        interval=config.CUPS_POLL_INTERVAL,
        first=5,
    )

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
