"""
Tee Python `logging` records from app modules into the active job's
activity log so the frontend Activity Log mirrors `docker compose logs`.

A ContextVar tracks the current job for the running pipeline. The handler
inspects each emitted record, filters to app loggers, formats it as the
same {timestamp, level, message} dict used by `MapGenerationJob.add_log`,
and appends to that job's logs list. Because ContextVar is propagated by
asyncio tasks and `asyncio.to_thread`/anyio worker threads, log records
from synchronous code dispatched off the event loop also reach the right job.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from services.map_generator import MapGenerationJob


# Logger name prefixes whose records should be teed into job.logs.
# Excludes uvicorn, urllib3, asyncio, rasterio internals, etc.
_APP_LOGGER_PREFIXES = ("main", "services.", "middleware.")
_APP_LOGGER_EXACT = {"main", "services", "middleware"}

# Hard cap on per-job log entries to prevent runaway memory if a service
# spins in a tight logging loop. Jobs are short-lived (cleaned up after 10
# minutes); this just protects against pathological cases.
MAX_LOGS_PER_JOB = 5000

# Map Python logging levels to the frontend's level names (info/success/warning/error).
_LEVEL_MAP = {
    logging.DEBUG: "info",
    logging.INFO: "info",
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}


current_job_var: ContextVar[Optional["MapGenerationJob"]] = ContextVar(
    "current_job", default=None
)


def _is_app_logger(name: str) -> bool:
    if name in _APP_LOGGER_EXACT:
        return True
    return any(name.startswith(prefix) for prefix in _APP_LOGGER_PREFIXES)


class JobLogHandler(logging.Handler):
    """Append matching log records to the current job's activity log."""

    def emit(self, record: logging.LogRecord) -> None:
        job = current_job_var.get()
        if job is None:
            return
        if not _is_app_logger(record.name):
            return
        try:
            message = record.getMessage()
        except Exception:
            return

        # Strip the `[job_id_prefix] ` tag that orchestrator log lines include —
        # it's redundant in the per-job activity log.
        job_id_tag = f"[{job.job_id}] "
        if message.startswith(job_id_tag):
            message = message[len(job_id_tag):]
        else:
            short_tag = f"[{job.job_id[:8]}"
            if message.startswith(short_tag):
                end = message.find("] ")
                if end != -1:
                    message = message[end + 2:]

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": _LEVEL_MAP.get(record.levelno, "info"),
            "message": message,
        }
        logs = job.logs
        logs.append(entry)
        overflow = len(logs) - MAX_LOGS_PER_JOB
        if overflow > 0:
            del logs[:overflow]


_installed = False


def install_job_log_handler(level: int = logging.INFO) -> JobLogHandler:
    """Attach the handler to the root logger. Idempotent."""
    global _installed
    handler = JobLogHandler(level=level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    if not _installed:
        logging.getLogger().addHandler(handler)
        _installed = True
    return handler
