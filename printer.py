from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import cups

from config import (
    COLOR_OPTIONS,
    ORIENTATION_OPTIONS,
    PRINTER_NAME,
    SIDES_OPTIONS,
)

# CUPS IPP job states
JOB_PENDING = 3
JOB_HELD = 4
JOB_PROCESSING = 5
JOB_STOPPED = 6
JOB_CANCELLED = 7
JOB_ABORTED = 8
JOB_COMPLETED = 9

_JOB_STATE_TEXT = {
    JOB_PENDING: "Queued",
    JOB_HELD: "Held",
    JOB_PROCESSING: "Printing",
    JOB_STOPPED: "Stopped",
    JOB_CANCELLED: "Cancelled",
    JOB_ABORTED: "Failed",
    JOB_COMPLETED: "Done",
}

# CUPS printer states
PRINTER_IDLE = 3
PRINTER_PROCESSING = 4
PRINTER_STOPPED = 5

_PRINTER_STATE_TEXT = {
    PRINTER_IDLE: "Idle",
    PRINTER_PROCESSING: "Processing",
    PRINTER_STOPPED: "Stopped",
}


@dataclass
class PrintJobInfo:
    job_id: int
    title: str
    state: int
    state_text: str
    pages_completed: int
    total_pages: int | None


@dataclass
class PrinterStatus:
    name: str
    state: str
    state_message: str
    is_online: bool
    ink_levels: dict[str, int] = field(default_factory=dict)


class CupsPrinter:
    """Wrapper around pycups for all printing operations."""

    def __init__(self, printer_name: str = PRINTER_NAME):
        self.printer_name = printer_name
        self._conn = cups.Connection()

    def _reconnect(self) -> None:
        self._conn = cups.Connection()

    def get_status(self) -> PrinterStatus:
        """Get printer status including online/offline and ink levels."""
        try:
            attrs = self._conn.getPrinterAttributes(self.printer_name)
        except cups.IPPError:
            self._reconnect()
            attrs = self._conn.getPrinterAttributes(self.printer_name)

        state_code = attrs.get("printer-state", PRINTER_IDLE)
        state_text = _PRINTER_STATE_TEXT.get(state_code, "Unknown")
        state_message = attrs.get("printer-state-message", "")
        is_accepting = attrs.get("printer-is-accepting-jobs", True)
        is_online = state_code != PRINTER_STOPPED and is_accepting

        # Parse ink/toner levels from HPLIP marker attributes
        ink_levels: dict[str, int] = {}
        marker_names = attrs.get("marker-names", [])
        marker_levels = attrs.get("marker-levels", [])

        if isinstance(marker_names, str):
            marker_names = [marker_names]
        if isinstance(marker_levels, int):
            marker_levels = [marker_levels]

        for name, level in zip(marker_names, marker_levels):
            ink_levels[name] = level

        return PrinterStatus(
            name=self.printer_name,
            state=state_text,
            state_message=state_message,
            is_online=is_online,
            ink_levels=ink_levels,
        )

    def submit_job(
        self, file_path: Path, title: str, settings: dict
    ) -> int:
        """Submit a print job. Returns CUPS job ID."""
        options: dict[str, str] = {}
        options["print-color-mode"] = COLOR_OPTIONS[settings["color"]]
        options["sides"] = SIDES_OPTIONS[settings["sides"]]
        options["orientation-requested"] = ORIENTATION_OPTIONS[
            settings["orientation"]
        ]
        options["number-up"] = str(settings["nup"])
        options["copies"] = str(settings["copies"])

        if settings["page_range"] != "all":
            options["page-ranges"] = settings["page_range"]

        try:
            job_id = self._conn.printFile(
                self.printer_name, str(file_path), title, options
            )
        except cups.IPPError:
            self._reconnect()
            job_id = self._conn.printFile(
                self.printer_name, str(file_path), title, options
            )
        return job_id

    def get_job_info(self, job_id: int) -> PrintJobInfo | None:
        """Get info for a specific job. Returns None if not found."""
        try:
            attrs = self._conn.getJobAttributes(job_id)
        except cups.IPPError:
            try:
                self._reconnect()
                attrs = self._conn.getJobAttributes(job_id)
            except cups.IPPError:
                return None

        state = attrs.get("job-state", JOB_PENDING)
        return PrintJobInfo(
            job_id=job_id,
            title=attrs.get("job-name", "Unknown"),
            state=state,
            state_text=_JOB_STATE_TEXT.get(state, "Unknown"),
            pages_completed=attrs.get("job-media-sheets-completed", 0),
            total_pages=attrs.get("job-media-sheets", None),
        )

    def get_all_jobs(self) -> list[PrintJobInfo]:
        """Get all active (not completed/cancelled) jobs."""
        try:
            jobs = self._conn.getJobs(which="not-completed")
        except cups.IPPError:
            self._reconnect()
            jobs = self._conn.getJobs(which="not-completed")

        result = []
        for job_id, attrs in jobs.items():
            state = attrs.get("job-state", JOB_PENDING)
            result.append(
                PrintJobInfo(
                    job_id=job_id,
                    title=attrs.get("job-name", "Unknown"),
                    state=state,
                    state_text=_JOB_STATE_TEXT.get(state, "Unknown"),
                    pages_completed=attrs.get(
                        "job-media-sheets-completed", 0
                    ),
                    total_pages=attrs.get("job-media-sheets", None),
                )
            )
        return result

    def cancel_job(self, job_id: int) -> bool:
        """Cancel a job. Returns True if successful."""
        try:
            self._conn.cancelJob(job_id)
            return True
        except cups.IPPError:
            try:
                self._reconnect()
                self._conn.cancelJob(job_id)
                return True
            except cups.IPPError:
                return False

    def cancel_all_jobs(self) -> int:
        """Cancel all active jobs. Returns count cancelled."""
        jobs = self.get_all_jobs()
        count = 0
        for job in jobs:
            if self.cancel_job(job.job_id):
                count += 1
        return count


# Async wrappers — run blocking CUPS calls in executor

async def async_get_status(
    printer_name: str = PRINTER_NAME,
) -> PrinterStatus:
    loop = asyncio.get_event_loop()
    printer = CupsPrinter(printer_name)
    return await loop.run_in_executor(None, printer.get_status)


async def async_submit_job(
    file_path: Path,
    title: str,
    settings: dict,
    printer_name: str = PRINTER_NAME,
) -> int:
    loop = asyncio.get_event_loop()
    printer = CupsPrinter(printer_name)
    return await loop.run_in_executor(
        None, partial(printer.submit_job, file_path, title, settings)
    )


async def async_get_job_info(
    job_id: int, printer_name: str = PRINTER_NAME
) -> PrintJobInfo | None:
    loop = asyncio.get_event_loop()
    printer = CupsPrinter(printer_name)
    return await loop.run_in_executor(
        None, partial(printer.get_job_info, job_id)
    )


async def async_get_all_jobs(
    printer_name: str = PRINTER_NAME,
) -> list[PrintJobInfo]:
    loop = asyncio.get_event_loop()
    printer = CupsPrinter(printer_name)
    return await loop.run_in_executor(None, printer.get_all_jobs)


async def async_cancel_job(
    job_id: int, printer_name: str = PRINTER_NAME
) -> bool:
    loop = asyncio.get_event_loop()
    printer = CupsPrinter(printer_name)
    return await loop.run_in_executor(
        None, partial(printer.cancel_job, job_id)
    )


async def async_cancel_all_jobs(
    printer_name: str = PRINTER_NAME,
) -> int:
    loop = asyncio.get_event_loop()
    printer = CupsPrinter(printer_name)
    return await loop.run_in_executor(None, printer.cancel_all_jobs)
