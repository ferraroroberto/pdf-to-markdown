"""Execute tab — file selection, backend options, live log stream, result display."""

from __future__ import annotations

import io
import itertools
import logging
import os
import queue
import sys
import time
import threading
import tkinter as tk
from tkinter import filedialog
from pathlib import Path

import streamlit as st

from src import vertexai_pricing
from src.classifier import classify_pdf
from src.config import load_settings
from src.logging_config import get_file_handler
from src.models import ConversionResult
from src.pipeline import Pipeline

_PROJECT_ROOT = Path(__file__).parent.parent


def _list_prompts() -> list[str]:
    """Return all .md files in prompts/ as paths relative to the project root."""
    return sorted(
        str(p.relative_to(_PROJECT_ROOT))
        for p in (_PROJECT_ROOT / "prompts").glob("*.md")
    )


# Gemini model options shown in the UI (order = dropdown order)
_VAI_MODELS: list[str] = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite-preview",
]


# ── Stream tee ─────────────────────────────────────────────────────────────────


class _TeeStream(io.TextIOBase):
    def __init__(self, log_queue: queue.Queue, original: io.TextIOBase) -> None:
        self._q = log_queue
        self._orig = original
        self._buf = ""

    def write(self, s: str) -> int:
        try:
            self._orig.write(s)
            self._orig.flush()
        except Exception:  # noqa: BLE001
            pass
        self._buf += s
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            clean = line.rstrip("\r").strip()
            if clean:
                self._q.put(clean)
        return len(s)

    def flush(self) -> None:
        try:
            self._orig.flush()
        except Exception:  # noqa: BLE001
            pass
        if self._buf.strip():
            self._q.put(self._buf.rstrip("\r").strip())
            self._buf = ""

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return getattr(self._orig, "encoding", "utf-8") or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._orig, "errors", "replace") or "replace"


# ── Logging helper ──────────────────────────────────────────────────────────────


class _QueueHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue) -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(self.format(record))


# ── Prior artifact cleanup (Execute tab) ────────────────────────────────────────


def _erase_prior_execution_artifacts(
    parent: Path,
    stem: str,
    *,
    protect_resolved: frozenset[Path] | None = None,
    logger: logging.Logger | None = None,
    log_removals: bool = False,
) -> None:
    """Remove files from a previous run that share *stem* (main output basename).

    Matches ``{stem}.*``, ``{stem}_chunk_*``, and the ``_chunks_{stem}/`` temp
    directory. Paths whose resolved path is in *protect_resolved* are skipped
    (typically the current source PDF so it is never deleted).
    """
    import shutil as _shutil

    protect = protect_resolved or frozenset()

    def _unlink(p: Path) -> None:
        if not p.is_file():
            return
        try:
            if p.resolve() in protect:
                return
        except OSError:
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

    chunks_dir = parent / f"_chunks_{stem}"
    if chunks_dir.is_dir():
        try:
            _shutil.rmtree(chunks_dir, ignore_errors=True)
            if log_removals and logger is not None:
                logger.info("ℹ️ Removed prior chunk temp dir: %s", chunks_dir.name)
        except OSError as exc:
            if logger is not None:
                logger.warning("⚠️ Could not remove %s: %s", chunks_dir, exc)


# ── Conversion worker ───────────────────────────────────────────────────────────


def _run_conversion(
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
    import shutil as _shutil
    import tempfile as _tempfile

    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _TeeStream(log_queue, orig_stdout)
    sys.stderr = _TeeStream(log_queue, orig_stderr)

    root = logging.getLogger()
    handler = _QueueHandler(log_queue)
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

    try:
        pipe = Pipeline(backend=backend)
        kwargs = dict(backend_kwargs or {})

        output_dir = pdf_path.parent
        output_stem = pdf_path.stem

        _protect = frozenset({pdf_path.resolve()}) if pdf_path.exists() else frozenset()
        _erase_prior_execution_artifacts(
            output_dir,
            output_stem,
            protect_resolved=_protect,
            logger=root,
            log_removals=verbose,
        )

        # In verbose mode, pass save dir to backend so raw AI responses are
        # written to disk immediately after each API call.
        if verbose and backend == "vertexai":
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
            if chunk_size > 0:
                from src.chunker import cleanup_chunks, merge_chunks, split_pdf
                from src.logger_exec import append_row

                all_chunk_list = split_pdf(working_pdf, chunk_size=chunk_size, overlap=chunk_overlap)
                total_available = len(all_chunk_list)

                if max_chunks > 0 and max_chunks < total_available:
                    root.info(
                        "ℹ️ Processing first %d of %d chunk(s) (max_chunks=%d)",
                        max_chunks, total_available, max_chunks,
                    )
                    chunks = all_chunk_list[:max_chunks]
                else:
                    chunks = all_chunk_list

                chunk_markdowns: list[str] = []
                chunk_metas: list[tuple[int, str, dict]] = []

                for chunk_idx, chunk_path, start_page, end_page in chunks:
                    root.info(
                        "ℹ️ Chunk %d/%d — pages %d–%d",
                        chunk_idx + 1, len(chunks), start_page, end_page,
                    )

                    chunk_kwargs = dict(kwargs)
                    if verbose and backend == "vertexai":
                        chunk_stem = f"{output_stem}_chunk_{chunk_idx + 1:03d}"
                        chunk_kwargs["verbose_save_dir"] = output_dir
                        chunk_kwargs["verbose_file_stem"] = chunk_stem

                    try:
                        r = pipe.convert(chunk_path, validate_output=False, **chunk_kwargs)
                        chunk_markdowns.append(r.markdown)
                        pages_label = f"{start_page}-{end_page}"
                        chunk_metas.append((chunk_idx, pages_label, r.metadata))

                        # Save chunk markdown + PDF slice immediately so they survive partial failures
                        if verbose:
                            chunk_md_path = output_dir / f"{output_stem}.chunk_{chunk_idx + 1:03d}.md"
                            chunk_md_path.write_text(r.markdown, encoding="utf-8")
                            chunk_pdf_path = output_dir / f"{output_stem}.chunk_{chunk_idx + 1:03d}.pdf"
                            _shutil.copy2(chunk_path, chunk_pdf_path)
                            root.debug(
                                "Saved chunk %d markdown → %s, PDF → %s",
                                chunk_idx + 1, chunk_md_path.name, chunk_pdf_path.name,
                            )

                        _log_steps(pdf_path, chunk_idx, pages_label, r, append_row)
                    except Exception as exc:  # noqa: BLE001
                        root.warning("⚠️ Chunk %d failed: %s — skipping", chunk_idx, exc)
                        chunk_markdowns.append(
                            f"\n\n> ⚠️ Chunk {chunk_idx + 1} (pages {start_page}–{end_page}) failed: {exc}\n\n"
                        )

                merged = merge_chunks(chunk_markdowns, chunk_overlap=chunk_overlap)
                if backend == "vertexai" and chunk_metas:
                    combined_meta = {
                        **_aggregate_chunked_vertex_metadata(chunk_metas),
                        "chunks": len(chunks),
                        "chunk_size": chunk_size,
                    }
                else:
                    combined_meta = {
                        **(chunk_metas[-1][2] if chunk_metas else {}),
                        "chunks": len(chunks),
                        "chunk_size": chunk_size,
                    }
                result = ConversionResult(
                    source=pdf_path,
                    markdown=merged,
                    backend_used=backend,
                    metadata=combined_meta,
                )
                try:
                    cleanup_chunks(working_pdf)
                except Exception:  # noqa: BLE001
                    pass
            else:
                from src.logger_exec import append_row
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
                if result.backend_used == "vertexai":
                    result.metadata["refinement_track_table"] = _build_refinement_track_table(
                        result.metadata, 1, "all",
                    )
                _log_steps(pdf_path, 0, "all", result, append_row)

        finally:
            if _tmp_conv_dir is not None:
                _shutil.rmtree(_tmp_conv_dir, ignore_errors=True)

        result_queue.put(("ok", result))
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("error", str(exc)))
    finally:
        root.removeHandler(handler)
        if _added_file_handler and file_handler:
            root.removeHandler(file_handler)
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        log_queue.put(None)


def _build_refinement_track_table(
    meta: dict,
    chunk_index: int,
    chunk_pages: str,
) -> list[dict]:
    """One row per API call (extraction + refinement passes), log-viewer-shaped."""
    rows: list[dict] = []
    ext = meta.get("extraction_step", {})
    in0 = int(ext.get("step_input_tokens", meta.get("total_input_tokens", 0)) or 0)
    out0 = int(ext.get("step_output_tokens", meta.get("total_output_tokens", 0)) or 0)
    rows.append({
        "chunk": chunk_index,
        "pages": chunk_pages,
        "step": 0,
        "step_type": "extraction",
        "iteration": "—",
        "errors": 0,
        "critical": 0,
        "moderate": 0,
        "minor": 0,
        "verdict": "—",
        "in_tok": in0,
        "out_tok": out0,
    })
    for track in meta.get("refinement_log", []):
        it = int(track.get("iteration", track.get("step", 0)) or 0)
        rows.append({
            "chunk": chunk_index,
            "pages": chunk_pages,
            "step": int(track.get("step", it)),
            "step_type": "refinement",
            "iteration": it,
            "errors": int(track.get("errors_found", 0) or 0),
            "critical": int(track.get("critical", 0) or 0),
            "moderate": int(track.get("moderate", 0) or 0),
            "minor": int(track.get("minor", 0) or 0),
            "verdict": str(track.get("verdict", "N/A")),
            "in_tok": int(track.get("step_input_tokens", 0) or 0),
            "out_tok": int(track.get("step_output_tokens", 0) or 0),
        })
    return rows


def _aggregate_chunked_vertex_metadata(
    chunk_metas: list[tuple[int, str, dict]],
) -> dict:
    """Merge Vertex *metadata* dicts from each chunk (tokens, corrections, track rows)."""
    if not chunk_metas:
        return {}
    first = chunk_metas[0][2]
    total_in = 0
    total_out = 0
    total_tok = 0
    track_table: list[dict] = []
    merged_corrections: list[dict] = []
    chunk_summaries: list[dict] = []

    for chunk_idx, pages, meta in chunk_metas:
        ci = chunk_idx + 1
        total_in += int(meta.get("total_input_tokens", 0) or 0)
        total_out += int(meta.get("total_output_tokens", 0) or 0)
        total_tok += int(meta.get("total_tokens", 0) or 0)
        track_table.extend(_build_refinement_track_table(meta, ci, pages))
        for c in meta.get("all_corrections", []):
            cc = dict(c)
            cc["chunk_index"] = ci
            cc["chunk_pages"] = pages
            merged_corrections.append(cc)
        rc = len(meta.get("refinement_log", []))
        chunk_summaries.append({
            "chunk": ci,
            "pages": pages,
            "iterations_completed": rc,
            "final_verdict": str(meta.get("final_verdict", "N/A")),
        })

    verdicts = [s["final_verdict"] for s in chunk_summaries]
    all_clean = bool(verdicts) and all(v == "CLEAN" for v in verdicts)
    any_refined = any(s["iterations_completed"] > 0 for s in chunk_summaries)
    overall = (
        "ALL CLEAN" if all_clean and any_refined else (
            "MIXED / SEE PER CHUNK" if verdicts and any_refined else "N/A"
        )
    )
    refinement_passes_total = sum(s["iterations_completed"] for s in chunk_summaries)
    by_chunk_txt = "; ".join(
        f"Chunk {s['chunk']} ({s['pages']}): {s['final_verdict']}"
        for s in chunk_summaries
    )

    return {
        "backend": first.get("backend", "vertexai"),
        "model": first.get("model", ""),
        "auth_mode": first.get("auth_mode", ""),
        "extraction_prompt_hash": first.get("extraction_prompt_hash", ""),
        "refinement_prompt_hash": first.get("refinement_prompt_hash", ""),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_tokens": total_tok,
        "iterations_completed": refinement_passes_total,
        "final_verdict": overall,
        "final_verdict_by_chunk": verdicts,
        "chunk_final_verdicts_text": by_chunk_txt,
        "chunk_refine_summaries": chunk_summaries,
        "refinement_track_table": track_table,
        "all_corrections": merged_corrections,
        "refinement_log": [],
        "iteration_markdowns": [],
        "raw_responses": [],
    }


def _log_steps(
    pdf_path: Path,
    chunk_idx: int,
    chunk_pages: str,
    result: ConversionResult,
    append_row,
) -> None:
    """Write one log row per API call: step 0 = extraction, step N = refinement pass N."""
    from datetime import datetime, timezone
    from src.vertexai_pricing import calculate_cost, load_pricing

    meta = result.metadata
    pricing = load_pricing()
    model = meta.get("model", "")
    auth_mode = meta.get("auth_mode", "")
    ext_hash = meta.get("extraction_prompt_hash", "")
    ref_hash = meta.get("refinement_prompt_hash", "")
    ts = datetime.now(timezone.utc).isoformat()

    def _row(step: int, step_type: str, in_tok: int, out_tok: int,
             errors: int = 0, critical: int = 0, moderate: int = 0,
             minor: int = 0, verdict: str = "N/A") -> None:
        total = in_tok + out_tok
        cost_label, _ = calculate_cost(model, in_tok, out_tok, pricing)
        append_row({
            "timestamp": ts,
            "file": str(pdf_path),
            "chunk_idx": chunk_idx,
            "chunk_pages": chunk_pages,
            "step": step,
            "step_type": step_type,
            "model": model,
            "auth_mode": auth_mode,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": total,
            "cost_label": cost_label,
            "errors": errors,
            "critical": critical,
            "moderate": moderate,
            "minor": minor,
            "verdict": verdict,
            "extraction_prompt_hash": ext_hash,
            "refinement_prompt_hash": ref_hash,
        })

    # Step 0: extraction call
    extraction_step = meta.get("extraction_step", {})
    _row(
        step=0,
        step_type="extraction",
        in_tok=extraction_step.get("step_input_tokens", meta.get("total_input_tokens", 0)),
        out_tok=extraction_step.get("step_output_tokens", meta.get("total_output_tokens", 0)),
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


# ── Session state bootstrap ─────────────────────────────────────────────────────


def _init_state() -> None:
    cfg = load_settings()
    vai = cfg.vertexai
    proc = cfg.processing
    defaults = {
        "ex_running": False,
        "ex_logs": [],
        "ex_result": None,
        "ex_log_q": None,
        "ex_result_q": None,
        "ex_output_path": None,
        "ex_source_path": None,   # original input file path (for cleanup of converted PDF)
        "ex_verbose": False,
        # seeded from config on first load
        "ex_auth_mode": vai.auth_mode,
        "ex_chunk_size": proc.chunk_size,
        "ex_chunk_overlap": proc.chunk_overlap,
        "ex_max_chunks": 0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _clear_output() -> None:
    """Reset Execute tab session state only — does not delete files on disk."""
    st.session_state.ex_logs = []
    st.session_state.ex_result = None
    st.session_state.ex_output_path = None
    st.session_state.ex_source_path = None
    st.session_state.ex_verbose = False


# ── Corrections report writer ───────────────────────────────────────────────────


def _save_corrections_report(result: ConversionResult, output_path: Path) -> Path | None:
    from datetime import datetime, timezone

    meta = result.metadata
    track_table: list[dict] | None = meta.get("refinement_track_table")
    track_record: list[dict] = meta.get("refinement_log", [])
    all_corrections: list[dict] = meta.get("all_corrections", [])

    has_refinement_rows = bool(
        track_table and any(r.get("step_type") == "refinement" for r in track_table),
    )
    if not track_record and not all_corrections and not has_refinement_rows:
        return None

    corrections_path = output_path.with_suffix(".corrections.md")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    model = meta.get("model", "unknown")
    final_verdict = meta.get("final_verdict", "N/A")
    iters = meta.get("iterations_completed", 0)
    summaries = meta.get("chunk_refine_summaries") or []

    lines: list[str] = [
        f"# Refinement Corrections — {result.source.name}",
        "",
        f"- **Generated**: {now}",
        f"- **Model**: {model}",
        f"- **Refinement pass(es) (total across chunks)**: {iters}",
        f"- **Overall final verdict**: {final_verdict}",
    ]
    if summaries:
        lines.append("- **Per chunk**:")
        for s in summaries:
            lines.append(
                f"  - Chunk {s['chunk']} (pages {s['pages']}): "
                f"{s['iterations_completed']} pass(es), verdict **{s['final_verdict']}**",
            )
    lines += ["", "---", "", "## Track Record", ""]

    if track_table:
        lines += [
            "| Chunk | Pages | Step | Type | Iter. | Errors | Crit. | Mod. | Minor | Verdict | In tok | Out tok |",
            "|-------|-------|------|------|-------|--------|-------|------|-------|---------|--------|---------|",
        ]
        for row in track_table:
            vit = row.get("iteration", "—")
            vit_s = str(vit) if vit != "—" else "—"
            v = str(row.get("verdict", "—"))
            icon = ""
            if row.get("step_type") == "refinement" and v not in ("—", "N/A"):
                icon = "✅ " if v == "CLEAN" else "⚠️ "
            lines.append(
                f"| {row['chunk']} | {row['pages']} | {row['step']} | {row['step_type']} | {vit_s} | "
                f"{row['errors']} | {row['critical']} | {row['moderate']} | {row['minor']} | "
                f"{icon}{v} | {row['in_tok']:,} | {row['out_tok']:,} |",
            )
    else:
        lines += [
            "| Iteration | Errors | Critical | Moderate | Minor | Verdict |",
            "|-----------|--------|----------|----------|-------|---------|",
        ]
        for row in track_record:
            verdict_icon = "✅" if row["verdict"] == "CLEAN" else "⚠️"
            lines.append(
                f"| {row['iteration']} | {row['errors_found']} | "
                f"{row['critical']} | {row['moderate']} | {row['minor']} | "
                f"{verdict_icon} {row['verdict']} |",
            )

    if all_corrections:
        lines += ["", "---", "", "## Detailed Corrections", ""]
        has_steps = not all(int(c.get("iteration", 0)) == 0 for c in all_corrections)
        chunk_keys = {int(c["chunk_index"]) for c in all_corrections if c.get("chunk_index") is not None}
        multi_chunk = len(chunk_keys) > 1

        def _corr_sort_key(c: dict) -> tuple:
            return (
                int(c.get("chunk_index", 0) or 0),
                int(c.get("iteration", 0) or 0),
            )

        sorted_corrections = sorted(all_corrections, key=_corr_sort_key)

        if multi_chunk:
            idx = 0
            for ck, group_it in itertools.groupby(
                sorted_corrections, key=lambda c: int(c.get("chunk_index", 0) or 0),
            ):
                group = list(group_it)
                if ck <= 0:
                    continue
                first = group[0]
                pages_l = first.get("chunk_pages", "?")
                lines += [f"### Chunk {ck} (PDF pages {pages_l})", ""]
                for c in group:
                    idx += 1
                    lines += _format_correction(
                        idx, c, int(c.get("iteration", 0)) if has_steps else None,
                    )
        else:
            for j, c in enumerate(sorted_corrections, 1):
                lines += _format_correction(
                    j, c, int(c.get("iteration", 0)) if has_steps else None,
                )
    else:
        lines += ["", "*No individual correction details were recorded.*"]

    corrections_path.write_text("\n".join(lines), encoding="utf-8")
    return corrections_path


def _format_correction(index: int, c: dict, found_step: int | None = None) -> list[str]:
    severity = c.get("severity", "unknown").upper()
    category = c.get("category", "unknown")
    result = [f"#### Error {index} — {severity} · {category}", ""]
    if c.get("chunk_index") is not None:
        result.append(
            f"- **Chunk**: {c['chunk_index']} (PDF pages {c.get('chunk_pages', 'N/A')})",
        )
    if found_step is not None:
        result.append(f"- **Found in step**: {found_step:02d}")
    result += [
        f"- **Location**: {c.get('location', 'N/A')}",
        f"- **PDF says**: `{c.get('pdf_says', 'N/A')}`",
        f"- **Markdown had**: `{c.get('markdown_had', 'N/A')}`",
    ]
    if found_step is not None:
        result.append(f"- **Corrected in step {found_step + 1:02d} to**: `{c.get('corrected_to', 'N/A')}`")
    else:
        result.append(f"- **Corrected to**: `{c.get('corrected_to', 'N/A')}`")
    result += [f"- **Risk**: {c.get('risk', 'N/A')}", ""]
    return result


# ── Tab UI ──────────────────────────────────────────────────────────────────────


def run() -> None:
    """Render the Execute tab."""
    _init_state()
    cfg = load_settings()
    vai_cfg = cfg.vertexai
    proc_cfg = cfg.processing

    running: bool = st.session_state.ex_running

    # ── 1. File selection ───────────────────────────────────────────────────
    st.subheader("Select File")

    col_input, col_browse = st.columns([5, 1])

    with col_browse:
        st.markdown("<div style='padding-top:1.9rem'>", unsafe_allow_html=True)
        if st.button("Browse...", width="stretch", key="browse_btn", disabled=running):
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", 1)
            chosen = filedialog.askopenfilename(
                title="Select a file",
                filetypes=[
                    ("All supported files", "*.pdf *.docx *.doc *.pptx *.ppt *.xlsx *.xls *.jpg *.jpeg *.png *.bmp *.tiff *.tif *.webp *.gif"),
                    ("PDF files", "*.pdf"),
                    ("Word documents", "*.docx *.doc"),
                    ("PowerPoint presentations", "*.pptx *.ppt"),
                    ("Images", "*.jpg *.jpeg *.png *.bmp *.tiff *.tif *.webp *.gif"),
                    ("All files", "*.*"),
                ],
            )
            root.destroy()
            if chosen:
                st.session_state.file_path_input = chosen
        st.markdown("</div>", unsafe_allow_html=True)

    with col_input:
        file_path_str = st.text_input(
            "File path",
            placeholder=r"C:\path\to\document.pdf",
            help="Paste the full local path to a PDF, Word, PowerPoint, or image file, or use Browse to pick one.",
            key="file_path_input",
            disabled=running,
        )

    pdf_path: Path | None = None

    if file_path_str:
        from src.file_converter import SUPPORTED_EXTENSIONS, needs_conversion

        p = Path(file_path_str.strip().strip('"'))
        if not p.exists():
            st.error(f"File not found: `{p}`")
        elif p.suffix.lower() not in {".pdf"} | SUPPORTED_EXTENSIONS:
            st.error(f"Unsupported file type: `{p.suffix}`. Supported: PDF, Word, PowerPoint, Excel, images.")
        else:
            pdf_path = p
            size_kb = p.stat().st_size / 1024

            if p.suffix.lower() == ".pdf":
                pdf_info = None
                with st.spinner("Inspecting PDF…"):
                    try:
                        pdf_info = classify_pdf(pdf_path)
                    except Exception:  # noqa: BLE001
                        pass

                cols = st.columns(5)
                cols[0].metric("Size", f"{size_kb:,.1f} KB")
                cols[1].metric("Pages", pdf_info.page_count if pdf_info else "—")
                cols[2].metric("Classification", pdf_info.classification if pdf_info else "—")
                cols[3].metric("Avg chars/page", f"{pdf_info.avg_chars_per_page:.0f}" if pdf_info else "—")
                cols[4].metric("Scanned", "Yes" if pdf_info and pdf_info.is_scanned else ("No" if pdf_info else "—"))
            else:
                from src.file_converter import OFFICE_EXTENSIONS, IMAGE_EXTENSIONS
                suffix = p.suffix.lower()
                if suffix in OFFICE_EXTENSIONS:
                    file_type = "Office document"
                else:
                    file_type = "Image"
                cols = st.columns(3)
                cols[0].metric("Size", f"{size_kb:,.1f} KB")
                cols[1].metric("Type", file_type)
                cols[2].metric("Format", p.suffix.upper())
                st.info("ℹ️ This file will be converted to PDF before extraction. **Vertex AI backend required.**")

    st.divider()

    # ── 2. Options ──────────────────────────────────────────────────────────
    # Backend and auth mode are set in the sidebar (shared across tabs)
    backend_choice: str = st.session_state.get("global_backend", cfg.processing.backend)
    auth_mode: str = st.session_state.get("global_auth_mode", vai_cfg.auth_mode)

    verbose: bool = st.checkbox(
        "Verbose",
        help="Show DEBUG-level log messages and save intermediate artifacts.",
        key="verbose_check",
        disabled=running,
    )

    # ── Advanced options (chunking + Vertex AI) ──────────────────────────────
    with st.expander("Advanced options", expanded=False):
        # Chunking
        st.markdown("##### Chunking")
        col_chunk, col_overlap, col_max_chunks = st.columns([2, 2, 2])
        with col_chunk:
            chunk_size: int = st.number_input(
                "Chunk size (pages, 0 = disabled)",
                min_value=0,
                value=proc_cfg.chunk_size,
                step=5,
                help="Split the document into chunks of this many pages and process each independently. 0 disables chunking.",
                key="ex_chunk_size_input",
                disabled=running,
            )
        with col_overlap:
            chunk_overlap: int = st.number_input(
                "Chunk overlap (pages)",
                min_value=0,
                value=proc_cfg.chunk_overlap,
                step=1,
                help="Trailing pages from the previous chunk to include at the start of the next, for context continuity.",
                key="ex_chunk_overlap_input",
                disabled=running,
            )
        with col_max_chunks:
            max_chunks: int = st.number_input(
                "Max chunks (0 = all)",
                min_value=0,
                value=st.session_state.get("ex_max_chunks", 0),
                step=1,
                help="Stop after processing this many chunks. 0 means process all chunks.",
                key="ex_max_chunks_input",
                disabled=running,
            )

        # Vertex AI configuration
        if backend_choice == "vertexai":
            st.markdown("##### Vertex AI Configuration")
            vai_col1, vai_col2, vai_col3 = st.columns([2, 2, 2])

            with vai_col1:
                st.text_input(
                    "Project ID",
                    value=vai_cfg.project_id or os.getenv("PROJECT_ID", ""),
                    help="Your Google Cloud project ID.",
                    key="vai_project_id",
                    disabled=running,
                )
            with vai_col2:
                st.text_input(
                    "Location",
                    value=vai_cfg.location,
                    help="Vertex AI region, e.g. europe-west3.",
                    key="vai_location",
                    disabled=running,
                )
            with vai_col3:
                _env_model = os.getenv("MODEL_ID", vai_cfg.model)
                _model_idx = _VAI_MODELS.index(_env_model) if _env_model in _VAI_MODELS else 0
                st.selectbox(
                    "Model",
                    _VAI_MODELS,
                    index=_model_idx,
                    help="Gemini model to use for extraction.",
                    key="vai_model_id",
                    disabled=running,
                )

            _cache_info = vertexai_pricing.get_cache_info()
            _cache_status = (
                f"Cached {_cache_info['fetched_at']} · {_cache_info['num_models']} models"
                if _cache_info["cached"]
                else f"Built-in fallback · {_cache_info['num_models']} models"
            )
            _upd_col, _status_col = st.columns([1, 4])
            with _upd_col:
                if st.button("Update cost table", key="update_pricing_btn", disabled=running):
                    with st.spinner("Fetching Vertex AI pricing..."):
                        try:
                            vertexai_pricing.fetch_and_cache()
                            st.success("Pricing table updated.")
                        except Exception as _e:
                            st.error(f"Fetch failed: {_e}")
                    st.rerun()
            with _status_col:
                st.caption(_cache_status)

            vai_col4, vai_col5, vai_col6 = st.columns([2, 2, 2])

            with vai_col4:
                st.slider(
                    "Refinement passes (0 = extraction only)",
                    min_value=0,
                    max_value=10,
                    value=vai_cfg.refine_iterations,
                    key="vai_refine_iterations",
                    disabled=running,
                )
            _prompts = _list_prompts()
            with vai_col5:
                _ext_default = vai_cfg.extraction_prompt
                st.selectbox(
                    "Extraction prompt",
                    _prompts,
                    index=_prompts.index(_ext_default) if _ext_default in _prompts else 0,
                    key="vai_extraction_prompt_file",
                    disabled=running,
                )
            with vai_col6:
                if st.session_state.get("vai_refine_iterations", 0) > 0:
                    _ref_default = vai_cfg.refinement_prompt
                    st.selectbox(
                        "Refinement prompt",
                        _prompts,
                        index=_prompts.index(_ref_default) if _ref_default in _prompts else 0,
                        key="vai_refinement_prompt_file",
                        disabled=running,
                    )

            if st.session_state.get("vai_refine_iterations", 0) > 0:
                vai_col7, _, __ = st.columns([2, 2, 2])
                with vai_col7:
                    st.number_input(
                        "Max errors to accept as CLEAN",
                        min_value=-1,
                        value=vai_cfg.clean_stop_max_errors,
                        step=1,
                        help=(
                            "Early-stop threshold. Stop only if errors <= this value. "
                            "**-1**: stop on any CLEAN. **0**: only when 0 errors remain."
                        ),
                        key="vai_clean_stop_max_errors",
                        disabled=running,
                    )

    if pdf_path is not None and not running:
        st.caption(f"Output will be saved to: `{pdf_path.with_suffix('.md')}`")

    st.divider()

    # ── 3. Execute button ────────────────────────────────────────────────────
    if not running:
        _btn_col, _dry_col = st.columns([4, 2])

        with _dry_col:
            st.markdown('<div style="margin-top: 0.35rem;"></div>', unsafe_allow_html=True)
            dry_run_check = st.toggle(
                "Dry run (estimate only)",
                key="dry_run_check",
                help="Count pages and estimate token cost without calling the API.",
            )

        _execute_clicked = _btn_col.button(
            "Convert",
            type="primary",
            disabled=(pdf_path is None),
            width="stretch",
            key="execute_btn",
        )

        if _execute_clicked:
            if pdf_path is None:
                st.warning("Please select a valid PDF file first.")
                st.stop()

            _clear_output()

            extra_kwargs: dict = {}
            if backend_choice == "vertexai":
                extra_kwargs = {
                    "project_id": st.session_state.get("vai_project_id", ""),
                    "location": st.session_state.get("vai_location", "europe-west3"),
                    "model_id": st.session_state.get("vai_model_id", "gemini-2.5-pro"),
                    "auth_mode": auth_mode,
                    "refine_iterations": st.session_state.get("vai_refine_iterations", 0),
                    "clean_stop_max_errors": st.session_state.get("vai_clean_stop_max_errors", 0),
                    "extraction_prompt_file": st.session_state.get(
                        "vai_extraction_prompt_file", "prompts/extraction.md"
                    ),
                    "refinement_prompt_file": st.session_state.get(
                        "vai_refinement_prompt_file", "prompts/refinement.md"
                    ),
                    "dry_run": dry_run_check,
                }

            log_q: queue.Queue = queue.Queue()
            result_q: queue.Queue = queue.Queue()

            thread = threading.Thread(
                target=_run_conversion,
                args=(pdf_path, backend_choice, verbose, result_q, log_q),
                kwargs={
                    "backend_kwargs": extra_kwargs,
                    "chunk_size": chunk_size,
                    "chunk_overlap": chunk_overlap,
                    "max_chunks": max_chunks,
                },
                daemon=True,
            )
            thread.start()

            st.session_state.ex_running = True
            st.session_state.ex_log_q = log_q
            st.session_state.ex_result_q = result_q
            st.session_state.ex_output_path = pdf_path.with_suffix(".md")
            st.session_state.ex_source_path = str(pdf_path)
            st.session_state.ex_max_chunks = max_chunks
            st.session_state.ex_verbose = verbose
            st.rerun()

    # ── 4. Poll log queue ───────────────────────────────────────────────────
    if st.session_state.ex_running:
        log_q = st.session_state.ex_log_q
        result_q = st.session_state.ex_result_q

        finished = False
        while True:
            try:
                msg = log_q.get_nowait()
            except queue.Empty:
                break
            if msg is None:
                finished = True
                break
            st.session_state.ex_logs.append(msg)

        if finished:
            st.session_state.ex_running = False
            if not result_q.empty():
                st.session_state.ex_result = result_q.get_nowait()
            st.rerun()

    # ── 5. Render logs ──────────────────────────────────────────────────────
    if st.session_state.ex_logs:
        import html as _html
        log_html = _html.escape("\n".join(st.session_state.ex_logs))
        _log_id = "ex_log_box"
        st.markdown(
            f"""<div style="margin-bottom:1rem">
                <div style="font-size:1.1rem;font-weight:600;margin-bottom:0.5rem">Execution Log</div>
                <div id="{_log_id}" style="height:320px;overflow:auto;background:#0d1117;border:1px solid #30363d;
                    border-radius:6px;padding:12px 16px;font-family:'SFMono-Regular',Consolas,monospace;
                    font-size:0.78rem;line-height:1.55;white-space:pre;color:#e6edf3">{log_html}</div>
            </div>
            <script>
                var el = document.getElementById("{_log_id}");
                if (el) el.scrollTop = el.scrollHeight;
            </script>""",
            unsafe_allow_html=True,
        )

    # ── 6. Show result ──────────────────────────────────────────────────────
    result_payload = st.session_state.ex_result
    if result_payload is not None and not st.session_state.ex_running:
        status, payload = result_payload
        output_path: Path = st.session_state.ex_output_path

        st.divider()

        if status == "error":
            st.error(f"Conversion failed:\n\n```\n{payload}\n```")
        else:
            result = payload
            result.save(output_path)

            _step_paths: list[Path] = []
            _raw_paths: list[Path] = []
            _chunk_pdf_paths: list[Path] = []
            if st.session_state.get("ex_verbose", False) and result.backend_used == "vertexai":
                # Save processed markdown snapshots after each step
                for _idx, _iter_md in enumerate(result.metadata.get("iteration_markdowns", []), 1):
                    _step_path = output_path.with_name(f"{output_path.stem}.step_{_idx:02d}.md")
                    _step_path.write_text(_iter_md, encoding="utf-8")
                    _step_paths.append(_step_path)
                # Collect raw response files already written by the backend
                for stale in sorted(output_path.parent.glob(f"{output_path.stem}.raw_step_*.txt")):
                    _raw_paths.append(stale)
                for stale in sorted(output_path.parent.glob(f"{output_path.stem}_chunk_*.raw_step_*.txt")):
                    _raw_paths.append(stale)
            if st.session_state.get("ex_verbose", False) and result.metadata.get("chunks", 0) > 1:
                for p in sorted(output_path.parent.glob(f"{output_path.stem}.chunk_*.pdf")):
                    _chunk_pdf_paths.append(p)

            corrections_path: Path | None = None
            if result.backend_used == "vertexai":
                corrections_path = _save_corrections_report(result, output_path)

            st.subheader("Result")

            # Summary line instead of 4 metric cards
            _pages = result.page_count if result.page_count is not None else "?"
            st.success(
                f"Converted **{_pages} pages** using **{result.backend_used}** "
                f"({len(result.markdown):,} chars, ~{result.token_estimate:,} tokens). "
                f"Saved to `{output_path.name}`"
            )

            # Download button for the markdown
            st.download_button(
                label="Download Markdown",
                data=result.markdown,
                file_name=output_path.name,
                mime="text/markdown",
                key="download_md_btn",
            )

            if _step_paths or _raw_paths or _chunk_pdf_paths:
                with st.expander("Saved artifacts"):
                    if _step_paths:
                        st.caption(f"Intermediate steps ({len(_step_paths)}): " +
                                   ", ".join(f"`{p.name}`" for p in _step_paths))
                    if _raw_paths:
                        st.caption(f"Raw AI responses ({len(_raw_paths)}): " +
                                   ", ".join(f"`{p.name}`" for p in _raw_paths))
                    if _chunk_pdf_paths:
                        st.caption(f"Chunk PDFs ({len(_chunk_pdf_paths)}): " +
                                   ", ".join(f"`{p.name}`" for p in _chunk_pdf_paths))
            if corrections_path is not None:
                st.caption(f"Corrections log: `{corrections_path.name}`")

            if result.backend_used == "vertexai":
                meta = result.metadata
                total_in = meta.get("total_input_tokens", 0)
                total_out = meta.get("total_output_tokens", 0)
                total_tok = meta.get("total_tokens", 0)
                model_used: str = meta.get("model", "gemini-2.5-pro")
                iters_done: int = meta.get("iterations_completed", 0)
                final_verdict: str = meta.get("final_verdict", "N/A")

                _pricing_data = vertexai_pricing.load_pricing()
                cost_label, _ = vertexai_pricing.calculate_cost(
                    model_used, total_in, total_out, _pricing_data
                )

                st.markdown("#### Vertex AI Usage")
                st.caption(
                    f"**Model**: {model_used} · "
                    f"**Tokens**: {total_in:,} in / {total_out:,} out ({total_tok:,} total) · "
                    f"**Est. cost**: {cost_label}"
                )

                track_table: list[dict] = meta.get("refinement_track_table") or []
                chunk_summaries: list[dict] = meta.get("chunk_refine_summaries") or []
                refinement_log: list[dict] = meta.get("refinement_log", [])
                if track_table or refinement_log:
                    st.markdown("#### Refinement Track Record")
                    if chunk_summaries and len(chunk_summaries) > 1:
                        bullets = "  \n".join(
                            f"- **Chunk {s['chunk']}** (pages {s['pages']}): "
                            f"{s['iterations_completed']} refinement pass(es), verdict **{s['final_verdict']}**"
                            for s in chunk_summaries
                        )
                        st.info(
                            f"**{len(chunk_summaries)} chunks** — **{iters_done}** refinement pass(es) in total "
                            f"(sum across chunks). **Overall**: **{final_verdict}**  \n{bullets}",
                        )
                    else:
                        st.info(
                            f"**{iters_done}** refinement pass(es) — final verdict: **{final_verdict}**",
                        )
                    if track_table:
                        # Coerce every cell to str so PyArrow never sees mixed types per column
                        # (e.g. extraction uses "—" for iteration, refinements use int).
                        def _track_cell(v: object) -> str:
                            return "—" if v is None else str(v)

                        display_rows = []
                        for row in track_table:
                            display_rows.append({
                                "Chunk": _track_cell(row["chunk"]),
                                "Pages": _track_cell(row["pages"]),
                                "Step": _track_cell(row["step"]),
                                "Type": _track_cell(row["step_type"]),
                                "Iter.": _track_cell(row["iteration"]),
                                "Errors": _track_cell(row["errors"]),
                                "Crit.": _track_cell(row["critical"]),
                                "Mod.": _track_cell(row["moderate"]),
                                "Minor": _track_cell(row["minor"]),
                                "Verdict": _track_cell(row["verdict"]),
                                "In tok": _track_cell(row["in_tok"]),
                                "Out tok": _track_cell(row["out_tok"]),
                            })
                        st.dataframe(display_rows, width="stretch")
                    else:
                        rows_md = (
                            "| Iteration | Errors | Critical | Moderate | Minor | Verdict |\n"
                            "|-----------|--------|----------|----------|-------|---------|"
                        )
                        for row in refinement_log:
                            icon = "✅" if row["verdict"] == "CLEAN" else (
                                "⚠️" if row["verdict"] == "NEEDS ANOTHER PASS" else "❓"
                            )
                            rows_md += (
                                f"\n| {row['iteration']} | {row['errors_found']} | "
                                f"{row['critical']} | {row['moderate']} | {row['minor']} | {icon} {row['verdict']} |"
                            )
                        st.markdown(rows_md)
                else:
                    st.info("Extraction only — no refinement passes were run.")

            with st.expander("Markdown preview", expanded=True):
                st.markdown(
                    f"""<div style="max-height:500px;overflow:auto;background:#161b22;
                        border:1px solid #30363d;border-radius:6px;padding:16px;
                        font-size:0.85rem;line-height:1.6;color:#e6edf3">
                        {result.markdown[:20000]}
                    </div>""",
                    unsafe_allow_html=True,
                )
                if len(result.markdown) > 20000:
                    st.caption("Showing first 20,000 characters. Download or view raw for full content.")

            with st.expander("Raw Markdown (copy-ready)"):
                st.code(result.markdown, language="markdown")

            if corrections_path is not None and corrections_path.exists():
                _corrections_text = corrections_path.read_text(encoding="utf-8")
                with st.expander("Corrections Preview"):
                    st.markdown(_corrections_text)
                with st.expander("Corrections Raw (copy-ready)"):
                    st.code(_corrections_text, language="markdown")

    # ── 7. Keep polling while running ────────────────────────────────────────
    if st.session_state.ex_running:
        time.sleep(0.3)
        st.rerun()
