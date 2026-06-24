"""Convert File tab — conversion worker and artifact orchestration.

This module holds the *non-UI* half of the Convert File tab (``app/execute.py``):
the prior-artifact cleanup helper, the threaded conversion worker, and the
bookkeeping of every file the worker writes.  It makes no Streamlit calls and
imports nothing from ``app/`` — it is pure logic that runs on a background
thread and communicates with the UI only through two queues, so it can be unit
tested without Streamlit the way ``corrections_report`` and ``logger_exec``
already are.

The worker returns the exact list of artifacts it wrote (``ExecutionArtifacts``)
through the result queue, so the UI renders a known set of paths instead of
re-globbing the output directory and hoping the patterns still match the
writer's naming (see ``ferraroroberto/pdf-to-markdown#43``).
"""

from __future__ import annotations

import logging
import queue
import sys
from dataclasses import dataclass, field
from pathlib import Path

from src.chunk_runner import ChunkOutcome, ChunkSpec, convert_chunked
from src.corrections_report import (
    aggregate_chunked_vertex_metadata,
    build_refinement_track_table,
    save_chunk_corrections_report,
    save_corrections_report,
)
from src.log_streaming import QueueHandler, TeeStream
from src.logger_exec import log_conversion_steps
from src.logging_config import get_file_handler
from src.models import ConversionResult
from src.pipeline import Pipeline

# Both the Vertex AI and hub Gemini backends emit the same rich metadata
# contract (extraction_step, refinement_log, raw_responses, token usage), so
# the verbose-artifact, usage-panel, and refinement-track UI applies to both.
GEMINI_STYLE_BACKENDS = frozenset({"vertexai", "hubgemini"})


@dataclass
class ExecutionArtifacts:
    """The files a single conversion run wrote, collected by the worker.

    Passed back through the result queue so the UI renders a known list of
    paths rather than re-scanning the output directory.  Empty lists mean
    "nothing of that kind was written this run".
    """

    step_md: list[Path] = field(default_factory=list)
    raw_responses: list[Path] = field(default_factory=list)
    chunk_pdfs: list[Path] = field(default_factory=list)
    chunk_md: list[Path] = field(default_factory=list)
    chunk_corrections: list[Path] = field(default_factory=list)
    corrections_report: Path | None = None

    @property
    def has_saved_artifacts(self) -> bool:
        """True when any per-step/per-chunk artifact was written this run."""
        return bool(
            self.step_md
            or self.raw_responses
            or self.chunk_pdfs
            or self.chunk_md
            or self.chunk_corrections
        )


# ── Prior artifact cleanup ───────────────────────────────────────────────────


def erase_prior_execution_artifacts(
    parent: Path,
    stem: str,
    *,
    protect_resolved: frozenset[Path] | None = None,
    logger: logging.Logger | None = None,
    log_removals: bool = False,
    preserve_chunk_files: bool = True,
) -> None:
    """Remove files from a previous run that share *stem* (main output basename).

    Matches ``{stem}.*`` and ``{stem}_chunk_*``, and removes the legacy
    ``_chunks_{stem}/`` temp directory.  Paths whose resolved path is in
    *protect_resolved* are skipped (typically the current source PDF).

    When *preserve_chunk_files* is ``True`` (the default), files matching
    ``{stem}.chunk_*.pdf``, ``{stem}.chunk_*.md``, and
    ``{stem}.chunk_*.corrections.md`` are **kept** so a subsequent run can
    resume from where the previous one left off.  Set to ``False`` to force a
    full clean restart (e.g. when the user explicitly requests it).
    """
    import shutil as _shutil

    protect = protect_resolved or frozenset()

    # Patterns that belong to resumable chunk artifacts (flat-layout naming)
    _CHUNK_SUFFIXES = (".pdf", ".md", ".corrections.md")

    def _is_chunk_artifact(p: Path) -> bool:
        """Return True if *p* is a resumable chunk file (e.g. stem.chunk_001.md)."""
        for sfx in _CHUNK_SUFFIXES:
            if p.name.endswith(sfx):
                inner = p.name[: -len(sfx)]
                # Check if remainder looks like "{stem}.chunk_NNN"
                if inner.startswith(f"{stem}.chunk_"):
                    tail = inner[len(f"{stem}.chunk_"):]
                    if tail.isdigit():
                        return True
        return False

    def _unlink(p: Path) -> None:
        if not p.is_file():
            return
        try:
            if p.resolve() in protect:
                return
        except OSError:
            return
        if preserve_chunk_files and _is_chunk_artifact(p):
            return
        try:
            p.unlink()
            if log_removals and logger is not None:
                logger.info("ℹ️ Removed prior artifact: %s", p.name)
        except OSError as exc:
            if logger is not None:
                logger.warning("⚠️ Could not remove %s: %s", p.name, exc)

    seen: set[Path] = set()
    for pattern in (f"{stem}.*", f"{stem}_chunk_*"):
        for p in parent.glob(pattern):
            if not p.is_file():
                continue
            try:
                key = p.resolve()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            _unlink(p)

    # Legacy temp subdir created by old-style split_pdf (always removed — not resumable)
    chunks_dir = parent / f"_chunks_{stem}"
    if chunks_dir.is_dir():
        try:
            _shutil.rmtree(chunks_dir, ignore_errors=True)
            if log_removals and logger is not None:
                logger.info("ℹ️ Removed prior chunk temp dir: %s", chunks_dir.name)
        except OSError as exc:
            if logger is not None:
                logger.warning("⚠️ Could not remove %s: %s", chunks_dir, exc)


# ── Exec-log helper ──────────────────────────────────────────────────────────


def _log_conversion_steps(
    file: str,
    chunk_idx: int,
    chunk_pages: str,
    meta: dict,
    pricing_data: dict,
) -> None:
    """Write exec-log rows for conversion *meta* via the shared logger_exec helper."""
    log_conversion_steps(
        file=file,
        chunk_idx=chunk_idx,
        chunk_pages=chunk_pages,
        meta=meta,
        pricing_data=pricing_data,
        model=meta.get("model", ""),
        auth_mode=meta.get("auth_mode", ""),
        extraction_prompt_hash=meta.get("extraction_prompt_hash", ""),
        refinement_prompt_hash=meta.get("refinement_prompt_hash", ""),
    )


# ── Conversion worker ────────────────────────────────────────────────────────


def run_execute_conversion(
    pdf_path: Path,
    backend: str,
    verbose: bool,
    result_queue: queue.Queue,
    log_queue: queue.Queue,
    backend_kwargs: dict | None = None,
    chunk_size: int = 0,
    chunk_overlap: int = 1,
    max_chunks: int = 0,
) -> None:
    """Convert *pdf_path* on a worker thread, streaming logs to *log_queue*.

    On success puts ``("ok", (result, artifacts))`` on *result_queue*, where
    *result* is a :class:`ConversionResult` and *artifacts* an
    :class:`ExecutionArtifacts` listing every file written.  On failure puts
    ``("error", message)``.
    """
    import shutil as _shutil
    import tempfile as _tempfile

    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = TeeStream(log_queue, orig_stdout)
    sys.stderr = TeeStream(log_queue, orig_stderr)

    root = logging.getLogger()
    handler = QueueHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(levelname)-8s  %(name)s: %(message)s"))
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)  # let handlers decide

    # Ensure the rotating file handler (DEBUG level) is also present in this thread
    file_handler = get_file_handler()
    _added_file_handler = False
    if file_handler and file_handler not in root.handlers:
        root.addHandler(file_handler)
        _added_file_handler = True

    artifacts = ExecutionArtifacts()

    try:
        pipe = Pipeline(backend=backend)
        kwargs = dict(backend_kwargs or {})

        output_dir = pdf_path.parent
        output_stem = pdf_path.stem
        output_path = output_dir / f"{output_stem}.md"

        _protect = frozenset({pdf_path.resolve()}) if pdf_path.exists() else frozenset()
        erase_prior_execution_artifacts(
            output_dir,
            output_stem,
            protect_resolved=_protect,
            logger=root,
            log_removals=verbose,
        )

        _is_gemini = backend in GEMINI_STYLE_BACKENDS

        # In verbose mode, pass save dir to backend so raw AI responses are
        # written to disk immediately after each API call.
        if verbose and _is_gemini:
            kwargs["verbose_save_dir"] = output_dir
            kwargs["verbose_file_stem"] = output_stem

        from src.file_converter import needs_conversion
        needs_conv = needs_conversion(pdf_path)

        # For non-PDF files: convert to PDF upfront when chunking is requested
        # or verbose mode is on (so the converted PDF is saved for inspection).
        # Otherwise the pipeline handles conversion internally via ensure_pdf().
        _tmp_conv_dir: Path | None = None
        working_pdf = pdf_path

        if needs_conv and (chunk_size > 0 or verbose):
            from src.file_converter import convert_to_pdf
            if verbose:
                # Save converted PDF permanently next to the source file
                working_pdf = convert_to_pdf(pdf_path, output_dir)
                root.info(
                    "ℹ️ Converted %s → %s (saved for inspection)",
                    pdf_path.name, working_pdf.name,
                )
            else:
                # Temporary directory — cleaned up in finally block
                _tmp_conv_dir = Path(_tempfile.mkdtemp(prefix="pdf2md_conv_"))
                working_pdf = convert_to_pdf(pdf_path, _tmp_conv_dir)

        try:
            from src.vertexai_pricing import load_pricing
            pricing_data = load_pricing()

            if chunk_size > 0:
                # Chunks are written directly to output_dir with consistent naming
                # ({stem}.chunk_NNN.pdf) so they persist for resume and inspection.
                processed: list[ChunkSpec] = []
                chunk_metas: list[tuple[int, str, dict]] = []
                # Artifact paths accumulated per chunk (kept separate so the
                # chunk-PDF/MD/corrections lists are only surfaced for true
                # multi-chunk runs, mirroring the prior `chunks > 1` gate).
                _acc_chunk_md: list[Path] = []
                _acc_chunk_corr: list[Path] = []
                _acc_raw: list[Path] = []

                def _chunk_md_path(spec: ChunkSpec) -> Path:
                    return output_dir / f"{output_stem}.chunk_{spec.num:03d}.md"

                def _is_resumable(spec: ChunkSpec) -> bool:
                    p = _chunk_md_path(spec)
                    return p.exists() and p.stat().st_size > 0

                def _on_split(specs: list[ChunkSpec], total_available: int) -> None:
                    processed[:] = specs
                    if max_chunks > 0 and max_chunks < total_available:
                        root.info(
                            "ℹ️ Processing first %d of %d chunk(s) (max_chunks=%d)",
                            max_chunks, total_available, max_chunks,
                        )
                    # Detect already-completed chunks from a prior (interrupted) run.
                    resumable = {s.idx for s in specs if _is_resumable(s)}
                    if resumable:
                        root.info(
                            "ℹ️ Resume detected: %d/%d chunk(s) already complete — "
                            "skipping those and continuing from chunk %d.",
                            len(resumable), len(specs),
                            min(set(range(len(specs))) - resumable, default=len(specs)) + 1,
                        )

                def _resume_lookup(spec: ChunkSpec) -> str | None:
                    if not _is_resumable(spec):
                        return None
                    existing_md = _chunk_md_path(spec).read_text(encoding="utf-8")
                    root.info(
                        "ℹ️ Chunk %d/%d — pages %s — ✅ resuming from saved file",
                        spec.num, len(processed), spec.pages_label,
                    )
                    return existing_md

                def _on_chunk_start(spec: ChunkSpec) -> None:
                    root.info(
                        "ℹ️ Chunk %d/%d — pages %s",
                        spec.num, len(processed), spec.pages_label,
                    )

                def _chunk_kwargs(spec: ChunkSpec) -> dict:
                    if _is_gemini and verbose:
                        return {
                            "verbose_save_dir": output_dir,
                            "verbose_file_stem": f"{output_stem}.chunk_{spec.num:03d}",
                        }
                    return {}

                def _failed_markdown(spec: ChunkSpec, exc: Exception) -> str:
                    return (
                        f"\n\n> ⚠️ Chunk {spec.num} (pages {spec.start_page}–{spec.end_page}) "
                        f"failed: {exc}\n\n"
                    )

                def _collect_chunk_artifacts(spec: ChunkSpec, meta: dict) -> None:
                    """Record every file already written to disk for *spec*."""
                    md_path = _chunk_md_path(spec)
                    if md_path.exists():
                        _acc_chunk_md.append(md_path)
                    corr_path = output_dir / f"{output_stem}.chunk_{spec.num:03d}.corrections.md"
                    if corr_path.exists():
                        _acc_chunk_corr.append(corr_path)
                    if verbose and _is_gemini:
                        for entry in meta.get("raw_responses", []):
                            raw_path = (
                                output_dir
                                / f"{output_stem}.chunk_{spec.num:03d}.raw_step_{int(entry.get('step', 0)):02d}.txt"
                            )
                            if raw_path.exists():
                                _acc_raw.append(raw_path)

                def _on_chunk(outcome: ChunkOutcome) -> None:
                    spec = outcome.spec
                    if outcome.error:
                        root.warning("⚠️ Chunk %d failed: %s — skipping", spec.idx, outcome.error)
                        return
                    if not outcome.resumed:
                        # Persist markdown + corrections + exec log immediately.
                        # Resumed chunks already have these on disk, so skip.
                        md_path = _chunk_md_path(spec)
                        md_path.write_text(outcome.markdown, encoding="utf-8")
                        root.debug("Saved chunk %d markdown → %s", spec.num, md_path.name)

                        corr = save_chunk_corrections_report(
                            outcome.metadata, output_dir, output_stem, spec.num, spec.pages_label,
                        )
                        if corr:
                            root.debug("Saved chunk %d corrections → %s", spec.num, corr.name)

                        _log_conversion_steps(
                            str(pdf_path), spec.idx, spec.pages_label, outcome.metadata, pricing_data,
                        )
                    chunk_metas.append((spec.idx, spec.pages_label, outcome.metadata))
                    _collect_chunk_artifacts(spec, outcome.metadata)

                outcomes, merged = convert_chunked(
                    working_pdf,
                    pipe,
                    kwargs,
                    chunk_size,
                    chunk_overlap,
                    validate_output=False,
                    max_chunks=max_chunks,
                    output_dir=output_dir,
                    file_stem=output_stem,
                    pages_dash="-",
                    failed_markdown=_failed_markdown,
                    resume_lookup=_resume_lookup,
                    chunk_kwargs=_chunk_kwargs,
                    on_split=_on_split,
                    on_chunk_start=_on_chunk_start,
                    on_chunk=_on_chunk,
                    merge_fallback=True,
                )

                # Raw verbose responses are surfaced whenever they exist; the
                # chunk PDF/MD/corrections lists only for true multi-chunk runs.
                artifacts.raw_responses.extend(sorted(_acc_raw, key=lambda p: p.name))
                if len(outcomes) > 1:
                    artifacts.chunk_pdfs.extend(
                        sorted(
                            (o.spec.path for o in outcomes if o.spec.path.exists()),
                            key=lambda p: p.name,
                        )
                    )
                    artifacts.chunk_md.extend(sorted(_acc_chunk_md, key=lambda p: p.name))
                    artifacts.chunk_corrections.extend(
                        sorted(_acc_chunk_corr, key=lambda p: p.name)
                    )

                if _is_gemini and chunk_metas:
                    combined_meta = {
                        **aggregate_chunked_vertex_metadata(chunk_metas),
                        "chunks": len(outcomes),
                        "chunk_size": chunk_size,
                    }
                else:
                    combined_meta = {
                        **(chunk_metas[-1][2] if chunk_metas else {}),
                        "chunks": len(outcomes),
                        "chunk_size": chunk_size,
                    }
                result = ConversionResult(
                    source=pdf_path,
                    markdown=merged,
                    backend_used=backend,
                    metadata=combined_meta,
                )
            else:
                # If we pre-converted (verbose + non-PDF), use the saved PDF path
                # directly so the pipeline skips internal conversion.
                r = pipe.convert(working_pdf, validate_output=False, **kwargs)
                # Always report source as the original input file
                if working_pdf != pdf_path:
                    result = ConversionResult(
                        source=pdf_path,
                        markdown=r.markdown,
                        backend_used=r.backend_used,
                        metadata=r.metadata,
                        validation=r.validation,
                    )
                else:
                    result = r
                if result.backend_used in GEMINI_STYLE_BACKENDS:
                    result.metadata["refinement_track_table"] = build_refinement_track_table(
                        result.metadata, 1, "all",
                    )
                _log_conversion_steps(str(pdf_path), 0, "all", result.metadata, pricing_data)

                # Verbose raw responses for the whole-document run.
                if verbose and result.backend_used in GEMINI_STYLE_BACKENDS:
                    for entry in result.metadata.get("raw_responses", []):
                        raw_path = (
                            output_dir
                            / f"{output_stem}.raw_step_{int(entry.get('step', 0)):02d}.txt"
                        )
                        if raw_path.exists():
                            artifacts.raw_responses.append(raw_path)

        finally:
            if _tmp_conv_dir is not None:
                _shutil.rmtree(_tmp_conv_dir, ignore_errors=True)

        # ── Persist final document + post-run artifacts ──────────────────────
        result.save(output_path)

        # Verbose per-step markdown snapshots.
        if verbose and result.backend_used in GEMINI_STYLE_BACKENDS:
            for _idx, _iter_md in enumerate(result.metadata.get("iteration_markdowns", []), 1):
                step_path = output_dir / f"{output_stem}.step_{_idx:02d}.md"
                step_path.write_text(_iter_md, encoding="utf-8")
                artifacts.step_md.append(step_path)

        # Full corrections report for the finished document.
        if result.backend_used in GEMINI_STYLE_BACKENDS:
            artifacts.corrections_report = save_corrections_report(
                result.source.name, result.metadata, output_path,
            )

        result_queue.put(("ok", (result, artifacts)))
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("error", str(exc)))
    finally:
        root.removeHandler(handler)
        if _added_file_handler and file_handler:
            root.removeHandler(file_handler)
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        log_queue.put(None)
