from __future__ import annotations

import logging
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
import printer

# Valid fields and their allowed values for setting toggles
_VALID_SETTINGS = {
    "color": {"color", "bw"},
    "sides": {"one", "long", "short"},
    "orientation": {"portrait", "landscape"},
}
_VALID_NUP = set(config.NUP_OPTIONS)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
SETTINGS = 0
PAGE_RANGE = 1


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
) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Printer Status", callback_data="main:status"
                ),
                InlineKeyboardButton(
                    "Print Queue", callback_data="main:queue"
                ),
            ]
        ]
    )
    await update.message.reply_text(
        "Welcome to PrinterBot!\n"
        "Just send me a file or photo and I'll print it.\n\n"
        "Supported: PDF, DOCX, PPTX, JPG, PNG, GIF, BMP, TIFF, WEBP",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Settings screen builder
# ---------------------------------------------------------------------------

def _mark(label: str, settings: dict, field: str, value) -> str:
    """Prefix with checkmark if this option is currently selected."""
    return f"\u2713 {label}" if settings[field] == value else label


def build_settings_screen(
    job: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the print-settings message text and inline keyboard."""
    s = job["settings"]
    name = job["original_name"]

    if job["is_image"]:
        header = f"{name} \u2014 image"
    elif job["page_count"]:
        header = f"{name} \u2014 {job['page_count']} pages"
    else:
        header = name

    text = f"{header}\nReady to print. Choose your settings:"

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

    if not job["is_image"]:
        # Pages per sheet
        rows.append(
            [
                InlineKeyboardButton(
                    _mark(str(n), s, "nup", n),
                    callback_data=f"set:nup:{n}",
                )
                for n in config.NUP_OPTIONS
            ]
        )

        # Page range
        if s["page_range"] == "all":
            rows.append(
                [
                    InlineKeyboardButton(
                        "\u2713 All", callback_data="set:page_range:all"
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
                        "All", callback_data="set:page_range:all"
                    ),
                    InlineKeyboardButton(
                        f"\u2713 Pages: {s['page_range']}",
                        callback_data="pr:custom",
                    ),
                ]
            )

    # Copies
    rows.append(
        [
            InlineKeyboardButton("\u2212", callback_data="set:copies:dec"),
            InlineKeyboardButton(str(s["copies"]), callback_data="noop"),
            InlineKeyboardButton("+", callback_data="set:copies:inc"),
        ]
    )

    # Actions
    rows.append(
        [
            InlineKeyboardButton("\U0001f5a8 Print", callback_data="act:print"),
            InlineKeyboardButton("Cancel", callback_data="act:cancel"),
        ]
    )

    return text, InlineKeyboardMarkup(rows)


def _build_settings_summary(settings: dict) -> str:
    """One-line summary of print settings."""
    parts = [
        "Color" if settings["color"] == "color" else "B\u200a&\u200aW",
        {
            "one": "One-sided",
            "long": "Long edge",
            "short": "Short edge",
        }[settings["sides"]],
        settings["orientation"].title(),
        f"{settings['nup']}/sheet",
        f"Pages: {settings['page_range']}",
        f"{settings['copies']} copy"
        if settings["copies"] == 1
        else f"{settings['copies']} copies",
    ]
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# File handlers (conversation entry points)
# ---------------------------------------------------------------------------

@authorized
async def handle_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle incoming document file."""
    doc = update.message.document
    file_name = doc.file_name or "document"
    ext = Path(file_name).suffix.lower()

    if ext not in config.SUPPORTED_EXTENSIONS:
        await update.message.reply_text(
            f"Can't print {ext} files.\n"
            "Supported: PDF, DOCX, PPTX, JPG, PNG, GIF, BMP, TIFF, WEBP"
        )
        return ConversationHandler.END

    # Download
    tg_file = await doc.get_file()
    local_path = (
        config.TEMP_DIR / f"{update.effective_user.id}_{doc.file_unique_id}{ext}"
    )
    await tg_file.download_to_drive(local_path)

    # Check for empty file
    if local_path.stat().st_size == 0:
        await update.message.reply_text("File is empty.")
        converter.cleanup_temp_files(local_path)
        return ConversationHandler.END

    # Init job data
    job = {
        "file_path": local_path,
        "pdf_path": None,
        "original_name": file_name,
        "is_image": converter.is_image(ext),
        "page_count": None,
        "settings": dict(config.DEFAULT_SETTINGS),
        "message_id": None,
        "cups_job_id": None,
    }
    context.user_data["job"] = job

    if converter.needs_conversion(ext):
        msg = await update.message.reply_text(
            f"Converting {file_name} to PDF\u2026"
        )
        try:
            pdf_path = await converter.convert_to_pdf(local_path)
            job["pdf_path"] = pdf_path
        except Exception as e:
            logger.error("Conversion failed: %s", e)
            await msg.edit_text(
                "Conversion failed. The file may be corrupted."
            )
            converter.cleanup_temp_files(local_path)
            return ConversationHandler.END

        job["page_count"] = await converter.get_pdf_page_count(pdf_path)
        text, keyboard = build_settings_screen(job)
        await msg.edit_text(text, reply_markup=keyboard)
        job["message_id"] = msg.message_id

    elif ext == ".pdf":
        job["page_count"] = await converter.get_pdf_page_count(local_path)
        text, keyboard = build_settings_screen(job)
        msg = await update.message.reply_text(text, reply_markup=keyboard)
        job["message_id"] = msg.message_id

    else:
        # Image sent as document
        job["is_image"] = True
        text, keyboard = build_settings_screen(job)
        msg = await update.message.reply_text(text, reply_markup=keyboard)
        job["message_id"] = msg.message_id

    return SETTINGS


@authorized
async def handle_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle incoming photo (compressed image)."""
    photo = update.message.photo[-1]  # Largest resolution
    tg_file = await photo.get_file()
    local_path = (
        config.TEMP_DIR
        / f"{update.effective_user.id}_{photo.file_unique_id}.jpg"
    )
    await tg_file.download_to_drive(local_path)

    job = {
        "file_path": local_path,
        "pdf_path": None,
        "original_name": f"photo_{photo.file_unique_id}.jpg",
        "is_image": True,
        "page_count": None,
        "settings": dict(config.DEFAULT_SETTINGS),
        "message_id": None,
        "cups_job_id": None,
    }
    context.user_data["job"] = job

    text, keyboard = build_settings_screen(job)
    msg = await update.message.reply_text(text, reply_markup=keyboard)
    job["message_id"] = msg.message_id
    return SETTINGS


# ---------------------------------------------------------------------------
# Settings handlers
# ---------------------------------------------------------------------------

async def handle_setting_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle any set:FIELD:VALUE callback — toggle setting, re-render."""
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
    """Ask user to type a custom page range."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Type page range (e.g. 1-3, 5, 8-10):"
    )
    return PAGE_RANGE


def _validate_page_range(text: str, total_pages: int | None) -> str | None:
    """Validate a page range string. Returns error message or None if valid.

    Rules:
    - Only digits, commas, dashes, spaces allowed
    - Each segment is either a single page or a range (start-end)
    - All pages must be >= 1
    - In ranges, start must be <= end
    - If total_pages is known, all pages must be <= total_pages
    """
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
        return f"Document only has {total_pages} pages, but you requested up to page {max_page}."

    return None


async def handle_page_range_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive typed page range, validate, update settings."""
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
# Print action
# ---------------------------------------------------------------------------

async def handle_print(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Submit the job to CUPS and start live status tracking."""
    query = update.callback_query
    await query.answer()

    job = context.user_data.get("job")
    if not job:
        await query.edit_message_text("No file to print.")
        return ConversationHandler.END

    # Validate page range one more time before printing
    s = job["settings"]
    if s["page_range"] != "all":
        error = _validate_page_range(s["page_range"], job.get("page_count"))
        if error:
            await query.answer(error, show_alert=True)
            return SETTINGS

    print_path = job["pdf_path"] or job["file_path"]

    # Check file is not empty
    if not Path(print_path).exists() or Path(print_path).stat().st_size == 0:
        await query.edit_message_text("File is empty or missing.")
        return ConversationHandler.END

    summary = _build_settings_summary(job["settings"])

    try:
        job_id = await printer.async_submit_job(
            print_path, job["original_name"], job["settings"],
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

    # Register for background polling
    context.bot_data.setdefault("active_jobs", {})[job_id] = {
        "chat_id": update.effective_chat.id,
        "message_id": query.message.message_id,
        "original_name": job["original_name"],
        "summary": summary,
        "user_id": update.effective_user.id,
        "file_path": str(job["file_path"]),
        "pdf_path": str(job["pdf_path"]) if job["pdf_path"] else None,
        "settings": dict(job["settings"]),
        "is_image": job.get("is_image", False),
        "last_state": None,
    }

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Cancel from settings
# ---------------------------------------------------------------------------

async def handle_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel print setup, clean up temp files."""
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
    """Show printer status."""
    query = update.callback_query
    await query.answer()

    try:
        status = await printer.async_get_status()
    except Exception as e:
        await query.edit_message_text(f"Cannot reach printer: {e}")
        return

    lines = [
        f"Printer: {status.name}",
        f"Status: {status.state}",
        f"{'Online' if status.is_online else 'OFFLINE'}",
    ]
    if status.state_message:
        lines.append(f"Message: {status.state_message}")

    if status.ink_levels:
        lines.append("")
        lines.append("Ink levels:")
        for name, level in status.ink_levels.items():
            lines.append(f"  {name}: {level}%")

    await query.edit_message_text("\n".join(lines))


@authorized
async def handle_print_queue(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show print queue with cancel buttons."""
    query = update.callback_query
    await query.answer()

    try:
        jobs = await printer.async_get_all_jobs()
    except Exception as e:
        await query.edit_message_text(f"Cannot reach CUPS: {e}")
        return

    if not jobs:
        await query.edit_message_text("Print queue is empty.")
        return

    lines = ["Print Queue:"]
    buttons: list[InlineKeyboardButton] = []
    for j in jobs:
        lines.append(f"#{j.job_id} \u2014 {j.title} \u2014 {j.state_text}")
        buttons.append(
            InlineKeyboardButton(
                f"Cancel #{j.job_id}",
                callback_data=f"q:cancel:{j.job_id}",
            )
        )

    # Arrange cancel buttons in rows of 2, plus Cancel All
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [InlineKeyboardButton("Cancel All", callback_data="q:cancelall")]
    )

    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
    )


@authorized
async def handle_job_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Cancel a specific print job."""
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
    """Cancel all print jobs."""
    query = update.callback_query
    await query.answer()

    count = await printer.async_cancel_all_jobs()
    context.bot_data["active_jobs"] = {}

    await query.edit_message_text(f"Cancelled {count} job(s).")


@authorized
async def handle_retry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Retry a failed print job with the same settings."""
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
            Path(print_path), failed["original_name"], failed["settings"],
            is_image=failed.get("is_image", False),
        )
    except Exception as e:
        await query.edit_message_text(f"Retry failed: {e}")
        return

    # Track new job
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
    """Background job: poll CUPS for job & printer state changes."""
    active_jobs: dict = context.bot_data.get("active_jobs", {})

    # --- Job monitoring ---
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
            continue  # No change

        info["last_state"] = current_state

        # Build updated status
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
            # Proactive notification
            try:
                await context.bot.send_message(
                    info["chat_id"],
                    f"Job #{job_id} ({info['original_name']}) "
                    "finished printing.",
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
            pass  # Message may have been deleted

    # Clean up finished jobs and temp files
    for job_id in finished_ids:
        info = active_jobs.pop(job_id, None)
        if info and info.get("last_state") == printer.JOB_COMPLETED:
            paths = [Path(info["file_path"])]
            if info.get("pdf_path"):
                paths.append(Path(info["pdf_path"]))
            converter.cleanup_temp_files(*paths)

    # --- Printer state monitoring ---
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

    # --- Ink level warnings ---
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

    # Conversation: file → settings → print
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Document.ALL, handle_document),
            MessageHandler(filters.PHOTO, handle_photo),
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
