"""Batch processing — folder discovery and orchestration.

Each file in the folder is processed independently (single-file or chunked pipeline).
No cross-file merging.  Results are collected into a ``BatchResult``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from src.chunker import cleanup_chunks, merge_chunks, split_pdf
from src.config import Settings
from src.models import BatchResult, ChunkResult, ConversionResult
from src.pipeline import Pipeline

logger = logging.getLogger("batch")


def discover(
    folder: Path,
    recursive: bool = True,
    extensions: list[str] | None = None,
) -> list[Path]:
    """Return all matching files under *folder*.

    Parameters
    ----------
    folder:
        Root directory to search.
    recursive:
        If True, recurse into sub-directories.
    extensions:
        File extensions to match (lower-case, with dot), e.g. ``[".pdf"]``.
        Defaults to ``[".pdf"]``.
    """
    exts = {e.lower() for e in (extensions or [".pdf"])}
    pattern = "**/*" if recursive else "*"
    found = [
        p for p in folder.glob(pattern)
        if p.is_file() and p.suffix.lower() in exts
    ]
    return sorted(found)


def run_batch(
    folder: Path,
    output_dir: Path | None,
    settings: Settings,
    validate_output: bool = False,
    dry_run: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> list[ChunkResult]:
    """Process all PDFs found under *folder* according to *settings*.

    Parameters
    ----------
    folder:
        Source directory.
    output_dir:
        If given, each converted file is saved as ``<stem>.md`` here.
    settings:
        Resolved ``Settings`` object.
    validate_output:
        Run validation on every conversion.
    dry_run:
        Skip API calls; return token estimates only.
    on_progress:
        Optional callback called with a human-readable progress string.

    Returns
    -------
    List of ``ChunkResult`` — one per file (or per chunk if chunking is enabled).
    """
    def _progress(msg: str) -> None:
        logger.info("ℹ️ %s", msg)
        if on_progress:
            on_progress(msg)

    pdfs = discover(
        folder,
        recursive=settings.batch.recursive,
        extensions=settings.batch.extensions,
    )

    if not pdfs:
        _progress(f"No PDFs found in {folder}")
        return []

    _progress(f"Found {len(pdfs)} PDF(s) in {folder}")

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    all_results: list[ChunkResult] = []
    backend_name = settings.processing.backend
    chunk_size = settings.processing.chunk_size
    chunk_overlap = settings.processing.chunk_overlap
    workers = settings.processing.workers

    from src.logger_exec import append_row
    from src.vertexai_pricing import calculate_cost, load_pricing

    pricing_data = load_pricing()

    pipe = Pipeline(backend=backend_name)
    backend_kwargs = _build_backend_kwargs(settings, dry_run)

    for file_idx, pdf_path in enumerate(pdfs, 1):
        _progress(f"[{file_idx}/{len(pdfs)}] {pdf_path.name}")

        if chunk_size > 0:
            file_results = _process_chunked(
                pdf_path, pipe, backend_kwargs, chunk_size, chunk_overlap,
                validate_output, output_dir, settings, pricing_data,
                append_row, _progress,
            )
        else:
            file_results = _process_single(
                pdf_path, pipe, backend_kwargs, validate_output, output_dir,
                settings, pricing_data, append_row, _progress,
            )

        all_results.extend(file_results)

    _progress(f"Batch finished — {len(all_results)} result(s) from {len(pdfs)} file(s)")
    return all_results


# ── Internal helpers ─────────────────────────────────────────────────────────


def _process_single(
    pdf_path: Path,
    pipe: Pipeline,
    backend_kwargs: dict,
    validate_output: bool,
    output_dir: Path | None,
    settings: Settings,
    pricing_data: dict,
    append_row,
    progress,
) -> list[ChunkResult]:
    from src.vertexai_pricing import calculate_cost

    try:
        result = pipe.convert(pdf_path, validate_output=validate_output, **backend_kwargs)
    except Exception as exc:
        logger.error("❌ Failed to convert %s: %s", pdf_path.name, exc)
        cr = ChunkResult(
            source=pdf_path,
            chunk_idx=0,
            chunk_pages="all",
            markdown="",
            backend_used=settings.processing.backend,
            metadata={},
            error=str(exc),
        )
        return [cr]

    if output_dir:
        out_path = output_dir / (pdf_path.stem + ".md")
        result.save(out_path)
        progress(f"  Saved → {out_path.name}")

    meta = result.metadata
    total_in = meta.get("total_input_tokens", 0)
    total_out = meta.get("total_output_tokens", 0)
    cost_label, _ = calculate_cost(meta.get("model", ""), total_in, total_out, pricing_data)

    refinement_log: list[dict] = meta.get("refinement_log", [])
    last_row = refinement_log[-1] if refinement_log else {}

    cr = ChunkResult(
        source=pdf_path,
        chunk_idx=0,
        chunk_pages="all",
        markdown=result.markdown,
        backend_used=result.backend_used,
        metadata=meta,
        iteration=meta.get("iterations_completed", 0),
        errors=last_row.get("errors_found", 0),
        critical=last_row.get("critical", 0),
        moderate=last_row.get("moderate", 0),
        minor=last_row.get("minor", 0),
        verdict=meta.get("final_verdict", "N/A"),
        cost_label=cost_label,
    )

    _log_steps(pdf_path, cr, meta, settings, append_row, pricing_data)
    return [cr]


def _process_chunked(
    pdf_path: Path,
    pipe: Pipeline,
    backend_kwargs: dict,
    chunk_size: int,
    chunk_overlap: int,
    validate_output: bool,
    output_dir: Path | None,
    settings: Settings,
    pricing_data: dict,
    append_row,
    progress,
) -> list[ChunkResult]:
    from src.vertexai_pricing import calculate_cost

    try:
        chunks = split_pdf(pdf_path, chunk_size=chunk_size, overlap=chunk_overlap)
    except Exception as exc:
        logger.error("❌ Failed to split %s: %s", pdf_path.name, exc)
        return [ChunkResult(
            source=pdf_path, chunk_idx=0, chunk_pages="all",
            markdown="", backend_used=settings.processing.backend,
            metadata={}, error=str(exc),
        )]

    chunk_results: list[ChunkResult] = []

    for chunk_idx, chunk_path, start_page, end_page in chunks:
        pages_label = f"{start_page}–{end_page}"
        progress(f"  Chunk {chunk_idx + 1}/{len(chunks)} (pages {pages_label})")

        try:
            result = pipe.convert(chunk_path, validate_output=validate_output, **backend_kwargs)
            error_msg = None
        except Exception as exc:
            logger.warning("⚠️ Chunk %d of %s failed: %s — skipping", chunk_idx, pdf_path.name, exc)
            error_msg = str(exc)
            result = None

        meta = result.metadata if result else {}
        total_in = meta.get("total_input_tokens", 0)
        total_out = meta.get("total_output_tokens", 0)
        cost_label, _ = calculate_cost(meta.get("model", ""), total_in, total_out, pricing_data)
        refinement_log: list[dict] = meta.get("refinement_log", [])
        last_row = refinement_log[-1] if refinement_log else {}

        cr = ChunkResult(
            source=pdf_path,
            chunk_idx=chunk_idx,
            chunk_pages=pages_label,
            markdown=result.markdown if result else "",
            backend_used=result.backend_used if result else settings.processing.backend,
            metadata=meta,
            iteration=meta.get("iterations_completed", 0),
            errors=last_row.get("errors_found", 0),
            critical=last_row.get("critical", 0),
            moderate=last_row.get("moderate", 0),
            minor=last_row.get("minor", 0),
            verdict=meta.get("final_verdict", "N/A"),
            cost_label=cost_label,
            error=error_msg,
        )
        chunk_results.append(cr)
        _log_steps(pdf_path, cr, meta, settings, append_row, pricing_data)

    # Save merged output
    if output_dir and chunk_results:
        merged = merge_chunks([cr.markdown for cr in chunk_results if not cr.error])
        if merged:
            out_path = output_dir / (pdf_path.stem + ".md")
            out_path.write_text(merged, encoding="utf-8")
            progress(f"  Merged → {out_path.name}")

    try:
        cleanup_chunks(pdf_path)
    except Exception:  # noqa: BLE001
        pass

    return chunk_results


def _build_backend_kwargs(settings: Settings, dry_run: bool = False) -> dict:
    vai = settings.vertexai
    return {
        "project_id": vai.project_id or os.getenv("PROJECT_ID", ""),
        "location": vai.location,
        "model_id": vai.model,
        "auth_mode": vai.auth_mode,
        "refine_iterations": vai.refine_iterations,
        "clean_stop_max_errors": vai.clean_stop_max_errors,
        "extraction_prompt_file": vai.extraction_prompt,
        "refinement_prompt_file": vai.refinement_prompt,
        "dry_run": dry_run,
    }


def _log_steps(
    pdf_path: Path,
    cr: ChunkResult,
    meta: dict,
    settings: Settings,
    append_row,
    pricing_data: dict,
) -> None:
    """Write one log row per API call (step 0 = extraction, step N = refinement N)."""
    from datetime import datetime, timezone
    from src.vertexai_pricing import calculate_cost

    model = meta.get("model", settings.vertexai.model)
    auth_mode = meta.get("auth_mode", settings.vertexai.auth_mode)
    ext_hash = meta.get("extraction_prompt_hash", "")
    ref_hash = meta.get("refinement_prompt_hash", "")
    ts = datetime.now(timezone.utc).isoformat()

    def _row(step: int, step_type: str, in_tok: int, out_tok: int,
             errors: int = 0, critical: int = 0, moderate: int = 0,
             minor: int = 0, verdict: str = "N/A", error: str | None = None) -> None:
        cost_label, _ = calculate_cost(model, in_tok, out_tok, pricing_data)
        append_row({
            "timestamp": ts,
            "file": str(pdf_path),
            "chunk_idx": cr.chunk_idx,
            "chunk_pages": cr.chunk_pages,
            "step": step,
            "step_type": step_type,
            "model": model,
            "auth_mode": auth_mode,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
            "cost_label": cost_label,
            "errors": errors,
            "critical": critical,
            "moderate": moderate,
            "minor": minor,
            "verdict": verdict,
            "error": error,
            "extraction_prompt_hash": ext_hash,
            "refinement_prompt_hash": ref_hash,
        })

    if cr.error:
        # Failed chunk: log a single error row
        _row(step=0, step_type="extraction", in_tok=0, out_tok=0,
             verdict="ERROR", error=cr.error)
        return

    # Step 0: extraction
    extraction_step = meta.get("extraction_step", {})
    _row(
        step=0,
        step_type="extraction",
        in_tok=extraction_step.get("step_input_tokens",
               meta.get("total_input_tokens", 0)),
        out_tok=extraction_step.get("step_output_tokens",
                meta.get("total_output_tokens", 0)),
    )

    # Steps 1..N: one row per refinement pass
    for track in meta.get("refinement_log", []):
        _row(
            step=track.get("step", track.get("iteration", 0)),
            step_type="refinement",
            in_tok=track.get("step_input_tokens", 0),
            out_tok=track.get("step_output_tokens", 0),
            errors=track.get("errors_found", 0),
            critical=track.get("critical", 0),
            moderate=track.get("moderate", 0),
            minor=track.get("minor", 0),
            verdict=track.get("verdict", "N/A"),
        )
