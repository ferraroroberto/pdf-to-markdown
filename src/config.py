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
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("config")

_CONFIG_PATH = Path(__file__).parent / "config.json"

# Canonical Gemini model IDs offered in every UI dropdown. Single source of
# truth so config.json, the Execute/Batch/Settings selectors, and the pricing
# table never drift apart. IDs must match the strings the Vertex AI API accepts
# (the Gemini 3.x family carries a "-preview" suffix on the public pricing page).
GEMINI_MODELS: list[str] = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite-preview",
]

# Canonical default Gemini model id — the first entry of GEMINI_MODELS. Every
# ``.get(..., DEFAULT_MODEL)`` fallback in the UI and backend sources this single
# constant so scattered ``"gemini-2.5-pro"`` literals can no longer drift apart
# from config.json or the dropdown list. Change the model only here.
DEFAULT_MODEL: str = GEMINI_MODELS[0]

# Extraction backends selectable from the CLI / UI. ``hubgemini`` routes PDFs
# through the local LLM hub (default, issue #27); ``vertexai`` calls Google
# Vertex AI directly (fallback). Keep in sync with ``src.pipeline._BACKENDS``.
BACKENDS: list[str] = ["hubgemini", "vertexai"]
DEFAULT_BACKEND: str = "hubgemini"

# Stable hub model alias used when the backend is ``hubgemini``. NEVER a
# display name — the hub repoints display_name under this alias over time.
HUB_MODEL: str = "gemini_pro"


# ── Sub-settings dataclasses ────────────────────────────────────────────────────


@dataclass
class VertexAISettings:
    """Effective Vertex AI settings (resolved from the active machine profile).

    **The single declaration of the Vertex AI field set.** A machine profile is
    the same nine fields plus a name (:class:`MachineProfile`), the raw
    config.json loader coerces them through :func:`_resolve_vertexai_fields`,
    and :func:`save_settings` maps them back with :func:`dataclasses.replace` —
    so adding a Vertex AI setting means editing this class and nothing else.
    """

    project_id: str = ""
    location: str = "europe-west3"
    model: str = DEFAULT_MODEL
    auth_mode: str = "api"
    refine_iterations: int = 0
    clean_stop_max_errors: int = 0
    diminishing_returns_enabled: bool = True
    extraction_prompt: str = "prompts/extraction_rag.md"
    refinement_prompt: str = "prompts/refinement_rag.md"


@dataclass
class MachineProfile(VertexAISettings):
    """Per-machine Vertex AI settings profile — :class:`VertexAISettings` + a name.

    Inherits the nine Vertex AI fields rather than restating them, so a profile
    and the effective settings resolved from it cannot drift apart. ``name`` is
    declared last because inherited fields come first in a dataclass; it is
    re-hoisted to the front on serialisation (:func:`_machine_to_dict`) so
    config.json keeps its familiar shape.
    """

    name: str = "Default"


# Canonical Vertex AI field names, derived from the one dataclass that declares
# them. Also the key set of a ``load_settings`` ``vertexai`` override block, so
# the UI's Advanced-options helper collects exactly these.
VERTEXAI_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(VertexAISettings))


@dataclass
class ProcessingSettings:
    chunk_size: int = 0
    chunk_overlap: int = 1
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
    backend: str = DEFAULT_BACKEND
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
    _field_defaults = VertexAISettings()
    for m in raw_machines:
        machines.append(MachineProfile(
            name=str(m.get("name", "Default")),
            **_resolve_vertexai_fields(m, _field_defaults),
        ))

    if not machines:
        machines = [MachineProfile()]

    active_machine_name = str(raw.get("active_machine", machines[0].name))
    active_machine = next(
        (m for m in machines if m.name == active_machine_name),
        machines[0],
    )

    # Top-level backend selector (default = hub). Override via overrides["backend"].
    backend_name = str((overrides or {}).get("backend", raw.get("backend", DEFAULT_BACKEND)))
    if backend_name not in BACKENDS:
        logger.warning(
            "⚠️ Unknown backend %r in config — falling back to %r", backend_name, DEFAULT_BACKEND
        )
        backend_name = DEFAULT_BACKEND

    # ── Build effective VertexAI settings from active machine ────────────────
    vai_overrides = (overrides or {}).get("vertexai", {})
    vai = VertexAISettings(**_resolve_vertexai_fields(vai_overrides, active_machine))

    # ── Remaining sections ───────────────────────────────────────────────────
    proc_raw = _deep_merge(raw.get("processing", {}), (overrides or {}).get("processing", {}))
    batch_raw = _deep_merge(raw.get("batch", {}), (overrides or {}).get("batch", {}))
    log_raw = _deep_merge(raw.get("logging", {}), (overrides or {}).get("logging", {}))

    return Settings(
        machines=machines,
        active_machine=active_machine_name,
        backend=backend_name,
        vertexai=vai,
        processing=ProcessingSettings(
            chunk_size=int(proc_raw.get("chunk_size", 0)),
            chunk_overlap=int(proc_raw.get("chunk_overlap", 1)),
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
    # Update the active machine with current vertexai values. ``replace`` copies
    # the nine inherited fields across and keeps ``name`` — no field list here.
    updated_machines = [
        replace(m, **asdict(settings.vertexai)) if m.name == settings.active_machine else m
        for m in settings.machines
    ]

    _write_config(updated_machines, settings)


def save_all_machines(settings: Settings) -> None:
    """Serialise *settings* to config.json **without** remapping the active machine.

    Unlike :func:`save_settings`, the machine profiles are written exactly as
    they appear on ``settings.machines`` — no profile is overwritten from
    ``settings.vertexai``. The Settings tab edits a specific machine profile in
    place and must persist that machine verbatim, so it calls this instead of
    re-implementing the serialiser against the private ``_CONFIG_PATH``.
    """
    _write_config(settings.machines, settings)


def build_backend_kwargs(
    settings: Settings,
    dry_run: bool = False,
    *,
    project_id_env_fallback: bool = False,
) -> dict[str, Any]:
    """Build the kwargs dict the active backend's ``convert`` call expects.

    Single source of truth shared by ``src.cli`` and ``src.batch`` so the two
    entry points cannot drift apart. For the ``hubgemini`` backend the model id
    is the stable hub alias (:data:`HUB_MODEL`); the Vertex display-name model
    and Vertex auth are ignored by that backend but passed through harmlessly
    for interface parity.

    Parameters
    ----------
    settings:
        Resolved :class:`Settings`.
    dry_run:
        Pass ``dry_run`` straight through to the backend.
    project_id_env_fallback:
        When True, fall back to the ``PROJECT_ID`` env var if the resolved
        project id is empty (the batch orchestrator's behaviour).
    """
    vai = settings.vertexai
    model_id = HUB_MODEL if settings.backend == "hubgemini" else vai.model
    project_id = vai.project_id
    if project_id_env_fallback:
        project_id = project_id or os.getenv("PROJECT_ID", "")
    return {
        "project_id": project_id,
        "location": vai.location,
        "model_id": model_id,
        "auth_mode": vai.auth_mode,
        "refine_iterations": vai.refine_iterations,
        "clean_stop_max_errors": vai.clean_stop_max_errors,
        "diminishing_returns_enabled": vai.diminishing_returns_enabled,
        "extraction_prompt_file": vai.extraction_prompt,
        "refinement_prompt_file": vai.refinement_prompt,
        "dry_run": dry_run,
    }


# ── Internal helpers ────────────────────────────────────────────────────────────


def _resolve_vertexai_fields(
    source: Mapping[str, Any] | None,
    fallback: VertexAISettings,
) -> dict[str, Any]:
    """Resolve the Vertex AI field set from *source*, defaulting to *fallback*.

    Values present in *source* (a raw config.json machine block, or a
    ``vertexai`` override block) win; anything missing comes from *fallback*
    (the dataclass defaults, or the active machine profile). Each value is
    coerced with the constructor of its declared default's type — the same
    ``str()`` / ``int()`` / ``bool()`` calls the field list used to spell out
    one by one — so JSON strings still become ints.
    """
    source = source or {}
    return {
        f.name: type(f.default)(source.get(f.name, getattr(fallback, f.name)))
        for f in fields(VertexAISettings)
    }


def _machine_to_dict(machine: MachineProfile) -> dict[str, Any]:
    """Serialise a profile with ``name`` first.

    ``name`` is declared last on :class:`MachineProfile` (inherited dataclass
    fields come first), so re-hoist it rather than let config.json's machine
    blocks flip key order.
    """
    data = asdict(machine)
    return {"name": data.pop("name"), **data}


def _write_config(machines: list[MachineProfile], settings: Settings) -> None:
    """Single config.json serialiser shared by both public savers.

    Writes *machines* plus the non-machine sections of *settings*. The two
    public entry points differ only in which machine list they hand in:
    :func:`save_settings` remaps the active machine from ``settings.vertexai``
    first; :func:`save_all_machines` passes the profiles through untouched.
    """
    data = {
        "active_machine": settings.active_machine,
        "backend": settings.backend,
        "machines": [_machine_to_dict(m) for m in machines],
        "processing": asdict(settings.processing),
        "batch": asdict(settings.batch),
        "logging": asdict(settings.logging),
    }
    _CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("ℹ️ Settings saved to %s", _CONFIG_PATH)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result
