"""Convert File tab — result rendering.

The display half of the Convert File tab: it takes the worker's result payload
(``("ok", (result, artifacts))`` or ``("error", message)``) and renders the
success summary, the saved-artifacts list, the usage panel, the refinement
track record, and the markdown/corrections previews.

It only *reads* — every file was already written by the worker
(``src.execute_worker``), which hands back the exact list of artifact paths in
:class:`~src.execute_worker.ExecutionArtifacts`.  This module never re-scans the
output directory.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src import vertexai_pricing
from src.config import DEFAULT_MODEL
from src.execute_worker import GEMINI_STYLE_BACKENDS, ExecutionArtifacts
from src.models import ConversionResult


def render_result(result_payload: tuple, output_path: Path) -> None:
    """Render the conversion result block for the Convert File tab.

    *result_payload* is what the worker put on the result queue; *output_path*
    is the markdown destination (used only for display labels — the worker has
    already saved it).
    """
    status, payload = result_payload

    st.divider()

    if status == "error":
        st.error(f"Conversion failed:\n\n```\n{payload}\n```")
        return

    result: ConversionResult
    artifacts: ExecutionArtifacts
    result, artifacts = payload

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

    if artifacts.has_saved_artifacts:
        with st.expander("Saved artifacts"):
            if artifacts.step_md:
                st.caption(f"Intermediate steps ({len(artifacts.step_md)}): " +
                           ", ".join(f"`{p.name}`" for p in artifacts.step_md))
            if artifacts.raw_responses:
                st.caption(f"Raw AI responses ({len(artifacts.raw_responses)}): " +
                           ", ".join(f"`{p.name}`" for p in artifacts.raw_responses))
            if artifacts.chunk_pdfs:
                st.caption(f"Chunk PDFs ({len(artifacts.chunk_pdfs)}): " +
                           ", ".join(f"`{p.name}`" for p in artifacts.chunk_pdfs))
            if artifacts.chunk_md:
                st.caption(
                    f"Chunk markdowns ({len(artifacts.chunk_md)}) — "
                    "kept for resume: " +
                    ", ".join(f"`{p.name}`" for p in artifacts.chunk_md)
                )
            if artifacts.chunk_corrections:
                st.caption(
                    f"Per-chunk corrections ({len(artifacts.chunk_corrections)}): " +
                    ", ".join(f"`{p.name}`" for p in artifacts.chunk_corrections)
                )

    corrections_path = artifacts.corrections_report
    if corrections_path is not None:
        st.caption(f"Corrections log: `{corrections_path.name}`")

    if result.backend_used in GEMINI_STYLE_BACKENDS:
        meta = result.metadata
        total_in = meta.get("total_input_tokens", 0)
        total_out = meta.get("total_output_tokens", 0)
        total_tok = meta.get("total_tokens", 0)
        model_used: str = meta.get("model", DEFAULT_MODEL)
        iters_done: int = meta.get("iterations_completed", 0)
        final_verdict: str = meta.get("final_verdict", "N/A")

        if result.backend_used == "hubgemini":
            # The hub's Gemini (agy) path does not surface token counts,
            # so cost can't be estimated for this backend.
            st.markdown("#### Hub Gemini Usage")
            st.caption(
                f"**Model**: {model_used} (via local LLM hub) · "
                "**Tokens**: not reported by the hub Gemini path"
            )
        else:
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
