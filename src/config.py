"""Centralised configuration loader and Settings dataclass.

Hierarchy (highest → lowest priority):
    CLI flags / UI selections  >  config.json  >  hardcoded defaults

Usage
-----
    from src.config import load_settings, save_settings, Settings

    settings = load_settings()                          # pure config.json
    settings = load_settings({"vertexai": {"model": "gemini-2.5-flash"}})  # with overrides
    save_settings(settings)                             # write back to config.json
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("config")

_CONFIG_PATH = Path(__file__).parent / "config.json"


# ── Sub-settings dataclasses ────────────────────────────────────────────────────


@dataclass
class VertexAISettings:
    project_id: str = ""
    location: str = "europe-west3"
    model: str = "gemini-2.5-pro"
    auth_mode: str = "api"
    refine_iterations: int = 0
    clean_stop_max_errors: int = 0
    diminishing_returns_enabled: bool = True
    extraction_prompt: str = "prompts/extraction.md"
    refinement_prompt: str = "prompts/refinement.md"


@dataclass
class ProcessingSettings:
    backend: str = "vertexai"
    chunk_size: int = 0
    chunk_overlap: int = 1
    workers: int = 1
    validate_after_convert: bool = False


@dataclass
class BatchSettings:
    recursive: bool = True
    extensions: list[str] = field(default_factory=lambda: [".pdf"])


@dataclass
class LoggingSettings:
    exec_log_dir: str = "tmp"
    exec_log_file: str = "exec_log.jsonl"
    log_dir: str = "tmp"
    log_max_bytes: int = 10 * 1024 * 1024  # 10 MB per file
    log_backup_count: int = 5


@dataclass
class Settings:
    vertexai: VertexAISettings = field(default_factory=VertexAISettings)
    processing: ProcessingSettings = field(default_factory=ProcessingSettings)
    batch: BatchSettings = field(default_factory=BatchSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)


# ── Public API ──────────────────────────────────────────────────────────────────


def load_settings(overrides: dict[str, Any] | None = None) -> Settings:
    """Load settings from config.json and apply *overrides* on top.

    *overrides* mirrors the config.json structure, e.g.::

        {"vertexai": {"model": "gemini-2.5-flash"}, "processing": {"chunk_size": 10}}

    Only keys present in *overrides* are replaced; everything else retains
    the value from config.json (or the hardcoded default if not in the file).
    """
    raw: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        try:
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Could not read config.json: %s — using defaults", exc)

    merged = _deep_merge(raw, overrides or {})

    vai_raw = merged.get("vertexai", {})
    proc_raw = merged.get("processing", {})
    batch_raw = merged.get("batch", {})
    log_raw = merged.get("logging", {})

    return Settings(
        vertexai=VertexAISettings(
            project_id=str(vai_raw.get("project_id", "")),
            location=str(vai_raw.get("location", "europe-west3")),
            model=str(vai_raw.get("model", "gemini-2.5-pro")),
            auth_mode=str(vai_raw.get("auth_mode", "api")),
            refine_iterations=int(vai_raw.get("refine_iterations", 0)),
            clean_stop_max_errors=int(vai_raw.get("clean_stop_max_errors", 0)),
            diminishing_returns_enabled=bool(vai_raw.get("diminishing_returns_enabled", True)),
            extraction_prompt=str(vai_raw.get("extraction_prompt", "prompts/extraction.md")),
            refinement_prompt=str(vai_raw.get("refinement_prompt", "prompts/refinement.md")),
        ),
        processing=ProcessingSettings(
            backend=str(proc_raw.get("backend", "vertexai")),
            chunk_size=int(proc_raw.get("chunk_size", 0)),
            chunk_overlap=int(proc_raw.get("chunk_overlap", 1)),
            workers=int(proc_raw.get("workers", 1)),
            validate_after_convert=bool(proc_raw.get("validate_after_convert", False)),
        ),
        batch=BatchSettings(
            recursive=bool(batch_raw.get("recursive", True)),
            extensions=list(batch_raw.get("extensions", [".pdf"])),
        ),
        logging=LoggingSettings(
            exec_log_dir=str(log_raw.get("exec_log_dir", "tmp")),
            exec_log_file=str(log_raw.get("exec_log_file", "exec_log.jsonl")),
            log_dir=str(log_raw.get("log_dir", "tmp")),
            log_max_bytes=int(log_raw.get("log_max_bytes", 10 * 1024 * 1024)),
            log_backup_count=int(log_raw.get("log_backup_count", 5)),
        ),
    )


def save_settings(settings: Settings) -> None:
    """Serialise *settings* back to config.json."""
    data = asdict(settings)
    _CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("ℹ️ Settings saved to %s", _CONFIG_PATH)


# ── Internal helpers ────────────────────────────────────────────────────────────


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result
