"""Corrections-report generation — pure data→markdown, no Streamlit.

The Execute tab (``app/execute.py``) processes each chunk and the final merged
result through a refinement loop and then writes human-readable *corrections
reports* to disk next to the output markdown:

- ``{stem}.chunk_NNN.corrections.md`` — one per chunk, written immediately after
  the chunk finishes so an interrupted run can still be inspected.
- ``{stem}.corrections.md`` — the full report for the whole document.

These functions are pure (``metadata dict`` → markdown string / file) with no
dependency on Streamlit, so they live in ``src/`` where they can be unit-tested.
They also build the refinement *track table* and aggregate per-chunk Vertex AI
metadata, which the UI consumes directly.

The per-chunk writer and the full writer share their table headers, verdict-icon
logic, and per-correction detail blocks via the helpers below so the two cannot
drift apart.
"""

from __future__ import annotations

import itertools
import re
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "build_refinement_track_table",
    "aggregate_chunked_vertex_metadata",
    "format_correction",
    "save_chunk_corrections_report",
    "save_corrections_report",
]


# ── Shared rendering helpers ─────────────────────────────────────────────────


def _verdict_icon(verdict: str) -> str:
    """Return the leading status icon (with trailing space) for a verdict.

    ``CLEAN`` → ✅, anything else → ⚠️. Empty string for the non-verdict
    placeholders (``"—"`` / ``"N/A"``) so extraction rows stay icon-free.
    """
    if verdict in ("—", "N/A"):
        return ""
    return "✅ " if verdict == "CLEAN" else "⚠️ "


def _utc_now_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_refinement_track_table(
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


def aggregate_chunked_vertex_metadata(
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
    unique_pages: set[int] = set()
    fallback_page_count = 0

    for chunk_idx, pages, meta in chunk_metas:
        ci = chunk_idx + 1
        total_in += int(meta.get("total_input_tokens", 0) or 0)
        total_out += int(meta.get("total_output_tokens", 0) or 0)
        total_tok += int(meta.get("total_tokens", 0) or 0)
        fallback_page_count += int(meta.get("page_count", 0) or 0)
        track_table.extend(build_refinement_track_table(meta, ci, pages))
        m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(pages))
        if m:
            start = int(m.group(1))
            end = int(m.group(2))
            if end >= start:
                unique_pages.update(range(start, end + 1))
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
    total_pages = len(unique_pages) if unique_pages else fallback_page_count
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
        "page_count": total_pages if total_pages > 0 else None,
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


def format_correction(index: int, c: dict, found_step: int | None = None) -> list[str]:
    """Render one correction as a list of markdown lines."""
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


# ── Report writers ───────────────────────────────────────────────────────────


def save_chunk_corrections_report(
    meta: dict,
    output_dir: Path,
    stem: str,
    chunk_num: int,
    pages: str,
) -> Path | None:
    """Save an intermediate corrections log for a single chunk immediately after processing.

    Creates ``{stem}.chunk_NNN.corrections.md`` next to the chunk markdown so
    partial runs can be recovered and each chunk's quality history is preserved.
    Returns the written path, or ``None`` if nothing was written.
    """
    if not meta:
        return None
    track_record: list[dict] = meta.get("refinement_log", [])
    all_corrections: list[dict] = meta.get("all_corrections", [])
    if not track_record and not all_corrections:
        return None

    corr_path = output_dir / f"{stem}.chunk_{chunk_num:03d}.corrections.md"
    model = meta.get("model", "unknown")
    iters = meta.get("iterations_completed", 0)
    final_verdict = meta.get("final_verdict", "N/A")

    lines: list[str] = [
        f"# Chunk {chunk_num} Corrections (pages {pages})",
        "",
        f"- **Generated**: {_utc_now_label()}",
        f"- **Model**: {model}",
        f"- **Refinement pass(es)**: {iters}",
        f"- **Final verdict**: {final_verdict}",
        "",
        "---",
        "",
        "## Track Record",
        "",
        "| Step | Type | Iter | Errors | Crit. | Mod. | Minor | Verdict | In tok | Out tok |",
        "|------|------|------|--------|-------|------|-------|---------|--------|---------|",
    ]

    ext = meta.get("extraction_step", {})
    in0 = int(ext.get("step_input_tokens", meta.get("total_input_tokens", 0)) or 0)
    out0 = int(ext.get("step_output_tokens", meta.get("total_output_tokens", 0)) or 0)
    lines.append(f"| 0 | extraction | — | 0 | 0 | 0 | 0 | — | {in0:,} | {out0:,} |")

    for track in track_record:
        it = int(track.get("iteration", track.get("step", 0)) or 0)
        v = str(track.get("verdict", "N/A"))
        lines.append(
            f"| {track.get('step', it)} | refinement | {it} | "
            f"{int(track.get('errors_found', 0) or 0)} | "
            f"{int(track.get('critical', 0) or 0)} | "
            f"{int(track.get('moderate', 0) or 0)} | "
            f"{int(track.get('minor', 0) or 0)} | "
            f"{_verdict_icon(v)}{v} | "
            f"{int(track.get('step_input_tokens', 0) or 0):,} | "
            f"{int(track.get('step_output_tokens', 0) or 0):,} |"
        )

    if all_corrections:
        lines += ["", "---", "", "## Corrections", ""]
        for j, c in enumerate(all_corrections, 1):
            lines += format_correction(j, c, int(c.get("iteration", 0)))
    else:
        lines += ["", "*No individual correction details recorded.*"]

    corr_path.write_text("\n".join(lines), encoding="utf-8")
    return corr_path


def save_corrections_report(
    source_name: str,
    meta: dict,
    output_path: Path,
) -> Path | None:
    """Write the full ``{stem}.corrections.md`` report for a finished document.

    *source_name* is the original input file name (shown in the title); *meta* is
    the conversion result metadata. Returns the written path, or ``None`` when
    there is nothing worth reporting.
    """
    track_table: list[dict] | None = meta.get("refinement_track_table")
    track_record: list[dict] = meta.get("refinement_log", [])
    all_corrections: list[dict] = meta.get("all_corrections", [])

    has_refinement_rows = bool(
        track_table and any(r.get("step_type") == "refinement" for r in track_table),
    )
    if not track_record and not all_corrections and not has_refinement_rows:
        return None

    corrections_path = output_path.with_suffix(".corrections.md")
    model = meta.get("model", "unknown")
    final_verdict = meta.get("final_verdict", "N/A")
    iters = meta.get("iterations_completed", 0)
    summaries = meta.get("chunk_refine_summaries") or []

    lines: list[str] = [
        f"# Refinement Corrections — {source_name}",
        "",
        f"- **Generated**: {_utc_now_label()}",
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
            icon = _verdict_icon(v) if row.get("step_type") == "refinement" else ""
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
            v = str(row["verdict"])
            lines.append(
                f"| {row['iteration']} | {row['errors_found']} | "
                f"{row['critical']} | {row['moderate']} | {row['minor']} | "
                f"{_verdict_icon(v)}{v} |",
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
                    lines += format_correction(
                        idx, c, int(c.get("iteration", 0)) if has_steps else None,
                    )
        else:
            for j, c in enumerate(sorted_corrections, 1):
                lines += format_correction(
                    j, c, int(c.get("iteration", 0)) if has_steps else None,
                )
    else:
        lines += ["", "*No individual correction details were recorded.*"]

    corrections_path.write_text("\n".join(lines), encoding="utf-8")
    return corrections_path
