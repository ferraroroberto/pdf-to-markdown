"""Execute tab — file selection, backend options, live log stream, result display."""

from __future__ import annotations

import io
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
from src.backends import list_available
from src.classifier import classify_pdf
from src.config import load_settings
from src.models import ConversionResult
from src.pipeline import Pipeline

_PROJECT_ROOT = Path(__file__).parent.parent

# Gemini model options shown in the UI (order = dropdown order)
_VAI_MODELS: list[str] = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
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
) -> None:
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _TeeStream(log_queue, orig_stdout)
    sys.stderr = _TeeStream(log_queue, orig_stderr)

    root = logging.getLogger()
    handler = _QueueHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(levelname)-8s  %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    try:
        pipe = Pipeline(backend=backend)
        kwargs = backend_kwargs or {}

        if chunk_size > 0:
            from src.chunker import cleanup_chunks, merge_chunks, split_pdf
            from src.logger_exec import append_row

            chunks = split_pdf(pdf_path, chunk_size=chunk_size, overlap=chunk_overlap)
            chunk_markdowns: list[str] = []
            combined_meta: dict = {}

            for chunk_idx, chunk_path, start_page, end_page in chunks:
                root.info(
                    "ℹ️ Chunk %d/%d — pages %d–%d",
                    chunk_idx + 1, len(chunks), start_page, end_page,
                )
                try:
                    r = pipe.convert(chunk_path, validate_output=False, **kwargs)
                    chunk_markdowns.append(r.markdown)
                    combined_meta = r.metadata
                    _log_steps(pdf_path, chunk_idx, f"{start_page}-{end_page}", r, append_row)
                except Exception as exc:  # noqa: BLE001
                    root.warning("⚠️ Chunk %d failed: %s — skipping", chunk_idx, exc)
                    chunk_markdowns.append(
                        f"\n\n> ⚠️ Chunk {chunk_idx + 1} (pages {start_page}–{end_page}) failed: {exc}\n\n"
                    )

            merged = merge_chunks(chunk_markdowns)
            result = ConversionResult(
                source=pdf_path,
                markdown=merged,
                backend_used=backend,
                metadata={
                    **combined_meta,
                    "chunks": len(chunks),
                    "chunk_size": chunk_size,
                },
            )
            try:
                cleanup_chunks(pdf_path)
            except Exception:  # noqa: BLE001
                pass
        else:
            from src.logger_exec import append_row
            result = pipe.convert(pdf_path, validate_output=False, **kwargs)
            _log_steps(pdf_path, 0, "all", result, append_row)

        result_queue.put(("ok", result))
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("error", str(exc)))
    finally:
        root.removeHandler(handler)
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        log_queue.put(None)


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
        "ex_verbose": False,
        # seeded from config on first load
        "ex_auth_mode": vai.auth_mode,
        "ex_chunk_size": proc.chunk_size,
        "ex_chunk_overlap": proc.chunk_overlap,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _clear_output() -> None:
    prev_output: Path | None = st.session_state.get("ex_output_path")
    if prev_output is not None:
        prev_output.unlink(missing_ok=True)
        for stale in prev_output.parent.glob(f"{prev_output.stem}.step_*.md"):
            stale.unlink(missing_ok=True)
        _corr = prev_output.with_suffix(".corrections.md")
        if _corr.exists():
            _corr.unlink()

    st.session_state.ex_logs = []
    st.session_state.ex_result = None
    st.session_state.ex_output_path = None
    st.session_state.ex_verbose = False


# ── Corrections report writer ───────────────────────────────────────────────────


def _save_corrections_report(result: ConversionResult, output_path: Path) -> Path | None:
    from datetime import datetime, timezone

    meta = result.metadata
    track_record: list[dict] = meta.get("refinement_log", [])
    all_corrections: list[dict] = meta.get("all_corrections", [])

    if not track_record and not all_corrections:
        return None

    corrections_path = output_path.with_suffix(".corrections.md")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    model = meta.get("model", "unknown")
    final_verdict = meta.get("final_verdict", "N/A")
    iters = meta.get("iterations_completed", 0)

    lines: list[str] = [
        f"# Refinement Corrections — {result.source.name}",
        "",
        f"- **Generated**: {now}",
        f"- **Model**: {model}",
        f"- **Iterations completed**: {iters}",
        f"- **Final verdict**: {final_verdict}",
        "",
        "---",
        "",
        "## Track Record",
        "",
        "| Iteration | Errors | Critical | Moderate | Minor | Verdict |",
        "|-----------|--------|----------|----------|-------|---------|",
    ]
    for row in track_record:
        verdict_icon = "✅" if row["verdict"] == "CLEAN" else "⚠️"
        lines.append(
            f"| {row['iteration']} | {row['errors_found']} | "
            f"{row['critical']} | {row['moderate']} | {row['minor']} | "
            f"{verdict_icon} {row['verdict']} |"
        )

    if all_corrections:
        lines += ["", "---", "", "## Detailed Corrections", ""]
        has_steps = not all(int(c.get("iteration", 0)) == 0 for c in all_corrections)
        sorted_corrections = (
            sorted(all_corrections, key=lambda c: int(c.get("iteration", 0)))
            if has_steps else all_corrections
        )
        for j, c in enumerate(sorted_corrections, 1):
            lines += _format_correction(j, c, int(c.get("iteration", 0)) if has_steps else None)
    else:
        lines += ["", "*No individual correction details were recorded.*"]

    corrections_path.write_text("\n".join(lines), encoding="utf-8")
    return corrections_path


def _format_correction(index: int, c: dict, found_step: int | None = None) -> list[str]:
    severity = c.get("severity", "unknown").upper()
    category = c.get("category", "unknown")
    result = [f"#### Error {index} — {severity} · {category}", ""]
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
    st.subheader("📂 Select PDF File")

    col_input, col_browse = st.columns([5, 1])

    with col_browse:
        st.markdown("<div style='padding-top:1.9rem'>", unsafe_allow_html=True)
        if st.button("🗂️  Browse…", width="stretch", key="browse_btn", disabled=running):
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", 1)
            chosen = filedialog.askopenfilename(
                title="Select a PDF file",
                filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
            )
            root.destroy()
            if chosen:
                st.session_state.file_path_input = chosen
        st.markdown("</div>", unsafe_allow_html=True)

    with col_input:
        file_path_str = st.text_input(
            "File path",
            placeholder=r"C:\path\to\document.pdf",
            help="Paste the full local path to the PDF file, or use Browse to pick one.",
            key="file_path_input",
            disabled=running,
        )

    pdf_path: Path | None = None

    if file_path_str:
        p = Path(file_path_str.strip().strip('"'))
        if not p.exists():
            st.error(f"File not found: `{p}`")
        elif p.suffix.lower() != ".pdf":
            st.error("Only `.pdf` files are supported.")
        else:
            pdf_path = p
            size_kb = p.stat().st_size / 1024
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

    st.divider()

    # ── 2. Options ──────────────────────────────────────────────────────────
    st.subheader("⚙️ Options")

    available_backends = list_available()
    if "vertexai" in available_backends:
        backend_options = ["vertexai"] + [b for b in available_backends if b != "vertexai"]
    else:
        backend_options = available_backends

    backend_labels: dict[str, str] = {
        "pdfplumber": "pdfplumber  (born-digital, fast)",
        "marker": "marker  (high accuracy, GPU optional)",
        "vertexai": "vertexai  (Gemini on Vertex AI, cloud)",
    }

    col_backend, col_auth, col_verbose = st.columns([3, 2, 1])

    with col_backend:
        cfg_backend = cfg.processing.backend
        cfg_backend_idx = backend_options.index(cfg_backend) if cfg_backend in backend_options else 0
        backend_choice: str = st.selectbox(
            "Backend",
            backend_options,
            index=cfg_backend_idx,
            format_func=lambda x: backend_labels.get(x, x),
            help="Select the extraction engine.",
            key="backend_select",
            disabled=running,
        )

    with col_auth:
        auth_idx = 0 if vai_cfg.auth_mode == "api" else 1
        auth_mode: str = st.selectbox(
            "Auth Mode",
            ["api", "gcloud"],
            index=auth_idx,
            help="**api**: uses GOOGLE_API_KEY env var (Express Mode).  **gcloud**: uses Application Default Credentials.",
            key="auth_mode_select",
            disabled=running,
        )

    with col_verbose:
        st.markdown('<div style="margin-top: 2.3rem;"></div>', unsafe_allow_html=True)
        verbose: bool = st.checkbox(
            "Verbose",
            help="Show DEBUG-level log messages.",
            key="verbose_check",
            disabled=running,
        )

    # ── Chunking options ────────────────────────────────────────────────────
    col_chunk, col_overlap, _ = st.columns([2, 2, 2])
    with col_chunk:
        chunk_size: int = st.number_input(
            "Chunk size (pages, 0 = disabled)",
            min_value=0,
            value=proc_cfg.chunk_size,
            step=5,
            help="Split PDF into chunks of this many pages and process each independently. 0 disables chunking.",
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

    # ── VertexAI-specific options ───────────────────────────────────────────
    if backend_choice == "vertexai":
        st.markdown("##### ☁️ Vertex AI Configuration")
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
            if st.button("🔄 Update cost table", key="update_pricing_btn", disabled=running):
                with st.spinner("Fetching Vertex AI pricing…"):
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
                "Iterative refinement passes (0 = extraction only)",
                min_value=0,
                max_value=10,
                value=vai_cfg.refine_iterations,
                key="vai_refine_iterations",
                disabled=running,
            )
        with vai_col5:
            st.text_input(
                "Extraction prompt file",
                value=vai_cfg.extraction_prompt,
                help="Path to the extraction prompt (relative to project root).",
                key="vai_extraction_prompt_file",
                disabled=running,
            )
        with vai_col6:
            if st.session_state.get("vai_refine_iterations", 0) > 0:
                st.text_input(
                    "Refinement prompt file",
                    value=vai_cfg.refinement_prompt,
                    help="Path to the refinement prompt (relative to project root).",
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
                        "Early-stop threshold. Stop only if errors ≤ this value. "
                        "**-1**: stop on any CLEAN. **0**: only when 0 errors remain."
                    ),
                    key="vai_clean_stop_max_errors",
                    disabled=running,
                )

    if pdf_path is not None and not running:
        st.caption(f"Output will be saved to: `{pdf_path.with_suffix('.md')}`")

    st.divider()

    # ── 3. Execute / Clean buttons ──────────────────────────────────────────
    if not running:
        _btn_col, _dry_col, _clean_col = st.columns([4, 2, 1])

        with _clean_col:
            if st.button("🧹 Clean", width="stretch", key="clean_btn",
                         help="Clear the execution log and result."):
                _clear_output()
                st.rerun()

        with _dry_col:
            dry_run_check = st.checkbox(
                "Dry run (estimate only)",
                key="dry_run_check",
                help="Count pages and estimate token cost without calling the API.",
            )

        _execute_clicked = _btn_col.button(
            "⚡  Execute",
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
                },
                daemon=True,
            )
            thread.start()

            st.session_state.ex_running = True
            st.session_state.ex_log_q = log_q
            st.session_state.ex_result_q = result_q
            st.session_state.ex_output_path = pdf_path.with_suffix(".md")
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
        log_html = _html.escape("\n".join(reversed(st.session_state.ex_logs)))
        with st.container(key="execution_log_container"):
            st.markdown(
                f"""<div style="margin-bottom:1rem">
                    <div style="font-size:1.1rem;font-weight:600;margin-bottom:0.5rem">📋 Execution Log</div>
                    <div style="height:320px;overflow:auto;background:#0d1117;border:1px solid #30363d;
                        border-radius:6px;padding:12px 16px;font-family:'SFMono-Regular',Consolas,monospace;
                        font-size:0.78rem;line-height:1.55;white-space:pre;color:#e6edf3">{log_html}</div>
                </div>""",
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
            if st.session_state.get("ex_verbose", False) and result.backend_used == "vertexai":
                for _idx, _iter_md in enumerate(result.metadata.get("iteration_markdowns", []), 1):
                    _step_path = output_path.with_name(f"{output_path.stem}.step_{_idx:02d}.md")
                    _step_path.write_text(_iter_md, encoding="utf-8")
                    _step_paths.append(_step_path)

            corrections_path: Path | None = None
            if result.backend_used == "vertexai":
                corrections_path = _save_corrections_report(result, output_path)

            st.subheader("✅ Result")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Backend", result.backend_used)
            m2.metric("Pages", result.page_count if result.page_count is not None else "—")
            m3.metric("Characters", f"{len(result.markdown):,}")
            m4.metric("Tokens (est.)", f"~{result.token_estimate:,}")

            st.success(f"Saved → `{output_path}`")
            if _step_paths:
                st.info(f"Intermediate steps saved ({len(_step_paths)}): " +
                        ", ".join(f"`{p.name}`" for p in _step_paths))
            if corrections_path is not None:
                st.success(f"Corrections log → `{corrections_path}`")

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

                st.markdown("#### ☁️ Vertex AI Usage")
                vc_model, vc1, vc2, vc3, vc4 = st.columns([4, 2, 2, 2, 2])
                vc_model.metric("Model", model_used)
                vc1.metric("Input tokens", f"{total_in:,}")
                vc2.metric("Output tokens", f"{total_out:,}")
                vc3.metric("Total tokens", f"{total_tok:,}")
                vc4.metric("Est. cost", cost_label)

                refinement_log: list[dict] = meta.get("refinement_log", [])
                if refinement_log:
                    st.markdown("#### 🔄 Refinement Track Record")
                    st.info(f"**{iters_done}** refinement pass(es) — final verdict: **{final_verdict}**")
                    rows_md = (
                        "| Iteration | Errors | Critical | Moderate | Minor | Verdict |\n"
                        "|-----------|--------|----------|----------|-------|---------|"
                    )
                    for row in refinement_log:
                        icon = "✅" if row["verdict"] == "CLEAN" else ("⚠️" if row["verdict"] == "NEEDS ANOTHER PASS" else "❓")
                        rows_md += (
                            f"\n| {row['iteration']} | {row['errors_found']} | "
                            f"{row['critical']} | {row['moderate']} | {row['minor']} | {icon} {row['verdict']} |"
                        )
                    st.markdown(rows_md)
                else:
                    st.info("Extraction only — no refinement passes were run.")

            with st.expander("📄 Markdown preview", expanded=True):
                preview = result.markdown[:6000]
                if len(result.markdown) > 6000:
                    preview += "\n\n*… (truncated — open the file for full content)*"
                st.markdown(preview)

            with st.expander("📋 Raw Markdown (copy-ready)"):
                st.code(result.markdown, language="markdown")

            if corrections_path is not None and corrections_path.exists():
                _corrections_text = corrections_path.read_text(encoding="utf-8")
                with st.expander("🔍 Corrections Preview"):
                    st.markdown(_corrections_text)
                with st.expander("📋 Corrections Raw (copy-ready)"):
                    st.code(_corrections_text, language="markdown")

    # ── 7. Keep polling while running ────────────────────────────────────────
    if st.session_state.ex_running:
        time.sleep(1)
        st.rerun()
