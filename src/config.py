"""Centralised configuration loader and Settings dataclass.

Hierarchy (highest → lowest priority):
    CLI flags / UI selections  >  active machine  >  config.json  >  hardcoded defaults

Usage
-----
    from src.config import load_settings, save_settings, Settings

    settings = load_settings()                          # loads active machine settings
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
class MachineProfile:
    """Per-machine Vertex AI settings profile."""

    name: str = "Default"
    project_id: str = ""
    location: str = "europe-west3"
    model: str = "gemini-2.5-pro"
    auth_mode: str = "api"
    refine_iterations: int = 0
    clean_stop_max_errors: int = 0
    diminishing_returns_enabled: bool = True
    extraction_prompt: str = "prompts/extraction_rag.md"
    refinement_prompt: str = "prompts/refinement_rag.md"


@dataclass
class VertexAISettings:
    """Effective Vertex AI settings (resolved from the active machine profile)."""

    project_id: str = ""
    location: str = "europe-west3"
    model: str = "gemini-2.5-pro"
    auth_mode: str = "api"
    refine_iterations: int = 0
    clean_stop_max_errors: int = 0
    diminishing_returns_enabled: bool = True
    extraction_prompt: str = "prompts/extraction_rag.md"
    refinement_prompt: str = "prompts/refinement_rag.md"


@dataclass
class ProcessingSettings:
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
    machines: list[MachineProfile] = field(default_factory=lambda: [MachineProfile()])
    active_machine: str = "Default"
    vertexai: VertexAISettings = field(default_factory=VertexAISettings)
    processing: ProcessingSettings = field(default_factory=ProcessingSettings)
    batch: BatchSettings = field(default_factory=BatchSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)


# ── Public API ──────────────────────────────────────────────────────────────────


def load_settings(overrides: dict[str, Any] | None = None) -> Settings:
    """Load settings from config.json, resolve the active machine, and apply *overrides*.

    *overrides* mirrors the config.json structure (vertexai, processing, batch, logging).
    Only keys present in *overrides* are replaced; everything else retains the value from
    the active machine profile or the hardcoded default.
    """
    raw: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        try:
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Could not read config.json: %s — using defaults", exc)

    # ── Load machines ────────────────────────────────────────────────────────
    raw_machines = raw.get("machines", [])
    machines: list[MachineProfile] = []
    for m in raw_machines:
        machines.append(MachineProfile(
            name=str(m.get("name", "Default")),
            project_id=str(m.get("project_id", "")),
            location=str(m.get("location", "europe-west3")),
            model=str(m.get("model", "gemini-2.5-pro")),
            auth_mode=str(m.get("auth_mode", "api")),
            refine_iterations=int(m.get("refine_iterations", 0)),
            clean_stop_max_errors=int(m.get("clean_stop_max_errors", 0)),
            diminishing_returns_enabled=bool(m.get("diminishing_returns_enabled", True)),
            extraction_prompt=str(m.get("extraction_prompt", "prompts/extraction_rag.md")),
            refinement_prompt=str(m.get("refinement_prompt", "prompts/refinement_rag.md")),
        ))

    if not machines:
        machines = [MachineProfile()]

    active_machine_name = str(raw.get("active_machine", machines[0].name))
    active_machine = next(
        (m for m in machines if m.name == active_machine_name),
        machines[0],
    )

    # ── Build effective VertexAI settings from active machine ────────────────
    vai_overrides = (overrides or {}).get("vertexai", {})
    vai = VertexAISettings(
        project_id=str(vai_overrides.get("project_id", active_machine.project_id)),
        location=str(vai_overrides.get("location", active_machine.location)),
        model=str(vai_overrides.get("model", active_machine.model)),
        auth_mode=str(vai_overrides.get("auth_mode", active_machine.auth_mode)),
        refine_iterations=int(vai_overrides.get("refine_iterations", active_machine.refine_iterations)),
        clean_stop_max_errors=int(vai_overrides.get("clean_stop_max_errors", active_machine.clean_stop_max_errors)),
        diminishing_returns_enabled=bool(
            vai_overrides.get("diminishing_returns_enabled", active_machine.diminishing_returns_enabled)
        ),
        extraction_prompt=str(vai_overrides.get("extraction_prompt", active_machine.extraction_prompt)),
        refinement_prompt=str(vai_overrides.get("refinement_prompt", active_machine.refinement_prompt)),
    )

    # ── Remaining sections ───────────────────────────────────────────────────
    proc_raw = _deep_merge(raw.get("processing", {}), (overrides or {}).get("processing", {}))
    batch_raw = _deep_merge(raw.get("batch", {}), (overrides or {}).get("batch", {}))
    log_raw = _deep_merge(raw.get("logging", {}), (overrides or {}).get("logging", {}))

    return Settings(
        machines=machines,
        active_machine=active_machine_name,
        vertexai=vai,
        processing=ProcessingSettings(
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
    """Serialise *settings* back to config.json.

    The active machine's fields are updated from ``settings.vertexai`` so edits
    in the Settings tab are persisted to the correct machine profile.
    """
    # Update the active machine with current vertexai values
    updated_machines = []
    for m in settings.machines:
        if m.name == settings.active_machine:
            updated_machines.append(MachineProfile(
                name=m.name,
                project_id=settings.vertexai.project_id,
                location=settings.vertexai.location,
                model=settings.vertexai.model,
                auth_mode=settings.vertexai.auth_mode,
                refine_iterations=settings.vertexai.refine_iterations,
                clean_stop_max_errors=settings.vertexai.clean_stop_max_errors,
                diminishing_returns_enabled=settings.vertexai.diminishing_returns_enabled,
                extraction_prompt=settings.vertexai.extraction_prompt,
                refinement_prompt=settings.vertexai.refinement_prompt,
            ))
        else:
            updated_machines.append(m)

    data = {
        "active_machine": settings.active_machine,
        "machines": [asdict(m) for m in updated_machines],
        "processing": asdict(settings.processing),
        "batch": asdict(settings.batch),
        "logging": asdict(settings.logging),
    }
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
