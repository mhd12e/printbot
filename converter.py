import asyncio
import subprocess
from pathlib import Path

from config import CONVERTIBLE_EXTENSIONS, IMAGE_EXTENSIONS

# Serialize LibreOffice conversions — it can't handle concurrent runs
_conversion_lock = asyncio.Semaphore(1)


async def convert_to_pdf(input_path: Path) -> Path:
    """Convert DOCX/PPTX to PDF via LibreOffice headless.

    Returns path to the generated PDF.
    Raises subprocess.CalledProcessError on failure.
    """
    async with _conversion_lock:
        output_dir = input_path.parent
        proc = await asyncio.create_subprocess_exec(
            "libreoffice",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(input_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, "libreoffice", stderr
            )

        pdf_path = input_path.with_suffix(".pdf")
        if not pdf_path.exists():
            raise FileNotFoundError(
                f"LibreOffice did not produce {pdf_path}"
            )
        return pdf_path


async def get_pdf_page_count(file_path: Path) -> int | None:
    """Get page count from a PDF using pdfinfo."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pdfinfo",
            str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode().splitlines():
            if line.startswith("Pages:"):
                return int(line.split(":")[1].strip())
    except Exception:
        pass
    return None


def needs_conversion(extension: str) -> bool:
    """Check if a file extension requires conversion to PDF."""
    return extension.lower() in CONVERTIBLE_EXTENSIONS


def is_image(extension: str) -> bool:
    """Check if extension is an image type."""
    return extension.lower() in IMAGE_EXTENSIONS


def cleanup_temp_files(*paths: Path) -> None:
    """Delete temporary files, silently ignoring errors."""
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
