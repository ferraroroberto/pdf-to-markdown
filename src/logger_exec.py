"""Per-execution structured logger — appends JSONL rows to tmp/exec_log.jsonl.

Format: one JSON object per line (JSONL).  Append-only; survives partial writes.
Load for analysis::

    import pandas as pd
    df = pd.read_json("tmp/exec_log.jsonl", lines=True)

Row schema (all keys always present) — ONE row per API call
--------------------------------------------------------------
timestamp           ISO-8601 UTC string
file                source PDF path
chunk_idx           int  (0 for non-chunked)
chunk_pages         str  e.g. "0-9" or "all"
step                int  0 = extraction call, 1..N = refinement pass N
step_type           str  "extraction" | "refinement" | "dry_run"
model               str
auth_mode           str  "api" | "gcloud"
input_tokens        int  tokens for this step only
output_tokens       int  tokens for this step only
total_tokens        int  tokens for this step only
cost_label          str  e.g. "$0.042"  (cost for this step only)
errors              int  (refinement steps only; 0 for extraction)
critical            int
moderate            int
minor               int
verdict             str  ("N/A" for extraction steps)
error               str | None  — set when the conversion failed
extraction_prompt_hash  str  8-char SHA-256 hex
refinement_prompt_hash  str  8-char SHA-256 hex
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("logger_exec")

_PROJECT_ROOT = Path(__file__).parent.parent

# Required row keys with their default values
_ROW_DEFAULTS: dict[str, Any] = {
    "timestamp": "",
    "file": "",
    "chunk_idx": 0,
    "chunk_pages": "all",
    "step": 0,
    "step_type": "extraction",
    "model": "",
    "auth_mode": "",
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "cost_label": "",
    "errors": 0,
    "critical": 0,
    "moderate": 0,
    "minor": 0,
    "verdict": "N/A",
    "error": None,
    "extraction_prompt_hash": "",
    "refinement_prompt_hash": "",
}


def append_row(row: dict[str, Any]) -> None:
    """Append *row* as a single JSON line to the execution log.

    Missing keys are filled with defaults from ``_ROW_DEFAULTS``.
    Errors writing to disk are logged but never re-raised (logging must not
    crash the pipeline).
    """
    from src.config import load_settings

    try:
        settings = load_settings()
        log_dir = _PROJECT_ROOT / settings.logging.exec_log_dir
        log_file = log_dir / settings.logging.exec_log_file
    except Exception:  # noqa: BLE001
        log_dir = _PROJECT_ROOT / "tmp"
        log_file = log_dir / "exec_log.jsonl"

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        normalised = {**_ROW_DEFAULTS, **row}
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(normalised, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Could not write to exec log: %s", exc)


def load_log() -> list[dict[str, Any]]:
    """Load all rows from the execution log as a list of dicts.

    Returns an empty list if the log file does not exist or is unreadable.
    """
    from src.config import load_settings

    try:
        settings = load_settings()
        log_path = (
            _PROJECT_ROOT / settings.logging.exec_log_dir / settings.logging.exec_log_file
        )
    except Exception:  # noqa: BLE001
        log_path = _PROJECT_ROOT / "tmp" / "exec_log.jsonl"

    if not log_path.exists():
        return []

    rows: list[dict] = []
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Could not read exec log: %s", exc)

    return rows
