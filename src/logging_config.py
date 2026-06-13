"""Centralised logging configuration — dual-handler setup.

Two independent log streams:

* **Console / Streamlit**: ``INFO`` by default (or ``DEBUG`` when *verbose=True*).
  Shows high-level progress, API call timings, and results.
* **File**: Always ``DEBUG``.  Written to ``tmp/pdf2md_<timestamp>.log`` using a
  ``RotatingFileHandler`` so disk usage stays bounded.

The file log uses an extended format that includes timestamps, log level, module,
function name, line number, and a correlation ID (``run_id``) so every message
from a single execution can be grouped together.

Usage
-----
Call :func:`setup_logging` once at process start (CLI ``main()``, Streamlit
``app.py``, or the Execute-tab worker thread)::

    from src.logging_config import setup_logging
    setup_logging()                     # INFO console + DEBUG file
    setup_logging(verbose=True)         # DEBUG console + DEBUG file

The ``run_id`` is auto-generated (short UUID) and attached to every log record
via a custom filter so ``grep <run_id> pdf2md_*.log`` finds all messages from
one invocation.

API call timing is logged at **INFO** level (visible on both streams) using
:func:`log_api_timing`.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIGURED = False

# ── Public helpers ─────────────────────────────────────────────────────────────

_current_run_id: str = ""


def get_run_id() -> str:
    """Return the correlation ID for the current execution."""
    return _current_run_id


class _RunIdFilter(logging.Filter):
    """Inject ``run_id`` into every log record so the file formatter can use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _current_run_id  # type: ignore[attr-defined]
        return True


# ── Format strings ─────────────────────────────────────────────────────────────

# Console: compact — level + logger name + message
_CONSOLE_FMT = "%(levelname)-8s  %(name)s: %(message)s"

# File: full audit trail — ISO timestamp, level, run_id, module:function:line, message
_FILE_FMT = (
    "%(asctime)s | %(levelname)-8s | %(run_id)s | %(name)s.%(funcName)s:%(lineno)d | %(message)s"
)
_FILE_DATE_FMT = "%Y-%m-%dT%H:%M:%S%z"

# ── Rotating file fallback defaults ─────────────────────────────────────────────
# Used only when the configured ``LoggingSettings`` cannot be loaded; the live
# values come from config.json via ``LoggingSettings.log_max_bytes`` /
# ``log_backup_count`` (see ``src/config.py``).

_MAX_BYTES = 10 * 1024 * 1024   # 10 MB per file
_BACKUP_COUNT = 5                # keep up to 5 rotated files


def setup_logging(
    verbose: bool = False,
    log_dir: str | Path | None = None,
    log_max_bytes: int | None = None,
    log_backup_count: int | None = None,
) -> str:
    """Configure the root logger with console + rotating file handlers.

    Parameters
    ----------
    verbose:
        When *True* the console handler drops to ``DEBUG``; otherwise ``INFO``.
    log_dir:
        Directory for the log file.  Defaults to ``<project_root>/tmp``.
    log_max_bytes:
        Max size per log file before rotation.  When ``None`` the value is read
        from the configured ``LoggingSettings.log_max_bytes`` (config.json),
        falling back to :data:`_MAX_BYTES`.
    log_backup_count:
        Number of rotated log files to keep.  When ``None`` the value is read
        from the configured ``LoggingSettings.log_backup_count`` (config.json),
        falling back to :data:`_BACKUP_COUNT`.

    Returns
    -------
    str
        The ``run_id`` assigned to this execution (8-char hex).
    """
    global _CONFIGURED, _current_run_id  # noqa: PLW0603

    _current_run_id = uuid.uuid4().hex[:8]

    # Resolve rotation knobs from the configured LoggingSettings unless the
    # caller passed explicit values. Guarded so logging setup never fails on a
    # bad/missing config.json.
    if log_max_bytes is None or log_backup_count is None:
        log_cfg = None
        try:
            from src.config import load_settings

            log_cfg = load_settings().logging
        except Exception:  # noqa: BLE001
            pass
        if log_max_bytes is None:
            log_max_bytes = log_cfg.log_max_bytes if log_cfg else _MAX_BYTES
        if log_backup_count is None:
            log_backup_count = log_cfg.log_backup_count if log_cfg else _BACKUP_COUNT

    root = logging.getLogger()

    # If already configured (e.g. Streamlit re-runs), only update console level
    if _CONFIGURED:
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
                h.setLevel(logging.DEBUG if verbose else logging.INFO)
        return _current_run_id

    root.setLevel(logging.DEBUG)  # let handlers decide what to pass through

    # ── Console handler ────────────────────────────────────────────────────
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter(_CONSOLE_FMT))
    console_handler.addFilter(_RunIdFilter())
    root.addHandler(console_handler)

    # ── File handler ───────────────────────────────────────────────────────
    dest = Path(log_dir) if log_dir else _PROJECT_ROOT / "tmp"
    dest.mkdir(parents=True, exist_ok=True)

    log_filename = f"pdf2md_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"
    log_path = dest / log_filename

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=log_max_bytes,
        backupCount=log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_FILE_DATE_FMT))
    file_handler.addFilter(_RunIdFilter())
    root.addHandler(file_handler)

    # Reduce noise from third-party libraries
    for noisy in ("urllib3", "google", "grpc", "httpcore", "httpx", "PIL", "fitz"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root.debug("Logging initialised — run_id=%s, file=%s, console=%s",
               _current_run_id, log_path, "DEBUG" if verbose else "INFO")

    _CONFIGURED = True
    return _current_run_id


def get_file_handler() -> RotatingFileHandler | None:
    """Return the current file handler (if any) so callers can add it to worker loggers."""
    for h in logging.getLogger().handlers:
        if isinstance(h, RotatingFileHandler):
            return h
    return None


def reset() -> None:
    """Remove all handlers and reset state.  Useful in tests."""
    global _CONFIGURED, _current_run_id  # noqa: PLW0603
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    _CONFIGURED = False
    _current_run_id = ""


# ── API timing helper ──────────────────────────────────────────────────────────


def log_api_timing(
    logger: logging.Logger,
    *,
    step_label: str,
    latency_s: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model: str = "",
    extra: dict | None = None,
) -> None:
    """Log an API call's timing and token usage at INFO level.

    This message appears in both the Streamlit UI log stream and the file log,
    providing a quick performance audit trail.

    Parameters
    ----------
    logger:
        The module-level logger to emit the record on.
    step_label:
        Human-readable label, e.g. ``"Extraction"`` or ``"Refinement pass 2"``.
    latency_s:
        Wall-clock time in seconds.
    input_tokens, output_tokens:
        Token counts from the API response.
    model:
        Model identifier string.
    extra:
        Any additional key-value pairs to include in the DEBUG-level detail line.
    """
    total_tokens = input_tokens + output_tokens
    logger.info(
        "API %s completed in %.2fs — model=%s, tokens=%s (in=%s, out=%s)",
        step_label, latency_s, model,
        f"{total_tokens:,}", f"{input_tokens:,}", f"{output_tokens:,}",
    )
    if extra:
        logger.debug("API %s detail: %s", step_label, extra)
