"""Tests for src/corrections_report.py — pure data→markdown report generation.

These functions were extracted from app/execute.py (issue #22) so the corrections
report format is unit-testable without importing Streamlit. The tests pin the
shared verdict-icon logic and the delegation between the chunk writer and the
full writer.
"""

from __future__ import annotations

from pathlib import Path

from src.corrections_report import (
    aggregate_chunked_vertex_metadata,
    build_refinement_track_table,
    format_correction,
    save_chunk_corrections_report,
    save_corrections_report,
)


def _meta_with_refinement() -> dict:
    return {
        "model": "gemini-2.5-pro",
        "iterations_completed": 2,
        "final_verdict": "CLEAN",
        "extraction_step": {"step_input_tokens": 100, "step_output_tokens": 50},
        "refinement_log": [
            {
                "step": 1, "iteration": 1, "verdict": "NEEDS ANOTHER PASS",
                "errors_found": 3, "critical": 1, "moderate": 1, "minor": 1,
                "step_input_tokens": 120, "step_output_tokens": 60,
            },
            {
                "step": 2, "iteration": 2, "verdict": "CLEAN",
                "errors_found": 0, "critical": 0, "moderate": 0, "minor": 0,
                "step_input_tokens": 130, "step_output_tokens": 40,
            },
        ],
        "all_corrections": [
            {
                "severity": "critical", "category": "number",
                "location": "p1", "pdf_says": "42", "markdown_had": "43",
                "corrected_to": "42", "risk": "wrong total", "iteration": 1,
            },
        ],
    }


class TestBuildTrackTable:
    def test_extraction_row_is_first_and_iconless(self):
        rows = build_refinement_track_table(_meta_with_refinement(), 1, "0-4")
        assert rows[0]["step_type"] == "extraction"
        assert rows[0]["iteration"] == "—"
        assert rows[0]["verdict"] == "—"

    def test_one_row_per_api_call(self):
        rows = build_refinement_track_table(_meta_with_refinement(), 1, "0-4")
        # 1 extraction + 2 refinement passes
        assert len(rows) == 3
        assert [r["step_type"] for r in rows[1:]] == ["refinement", "refinement"]


class TestAggregate:
    def test_empty_returns_empty(self):
        assert aggregate_chunked_vertex_metadata([]) == {}

    def test_tokens_and_pages_aggregate(self):
        m = _meta_with_refinement()
        m["total_input_tokens"] = 1000
        m["total_output_tokens"] = 500
        m["total_tokens"] = 1500
        merged = aggregate_chunked_vertex_metadata([(0, "1-5", m), (1, "6-10", m)])
        assert merged["total_input_tokens"] == 2000
        assert merged["total_output_tokens"] == 1000
        # 1-5 + 6-10 = 10 unique pages
        assert merged["page_count"] == 10
        assert merged["final_verdict"] == "ALL CLEAN"


class TestFormatCorrection:
    def test_includes_severity_and_location(self):
        c = {"severity": "minor", "category": "text", "location": "p2",
             "pdf_says": "a", "markdown_had": "b", "corrected_to": "a", "risk": "low"}
        out = "\n".join(format_correction(1, c))
        assert "MINOR" in out
        assert "p2" in out
        assert "**Corrected to**" in out

    def test_found_step_switches_label(self):
        c = {"severity": "minor", "category": "text", "corrected_to": "x"}
        out = "\n".join(format_correction(1, c, found_step=2))
        assert "Found in step**: 02" in out
        assert "Corrected in step 03 to" in out


class TestChunkWriter:
    def test_writes_file_with_clean_icon(self, tmp_path: Path):
        path = save_chunk_corrections_report(
            _meta_with_refinement(), tmp_path, "doc", 1, "0-4",
        )
        assert path is not None and path.exists()
        text = path.read_text(encoding="utf-8")
        assert path.name == "doc.chunk_001.corrections.md"
        assert "✅" in text  # the CLEAN refinement row
        assert "⚠️" in text  # the NEEDS ANOTHER PASS row
        assert "## Track Record" in text
        assert "## Corrections" in text

    def test_empty_meta_writes_nothing(self, tmp_path: Path):
        assert save_chunk_corrections_report({}, tmp_path, "doc", 1, "0-4") is None


class TestFullWriter:
    def test_writes_file_with_title(self, tmp_path: Path):
        meta = _meta_with_refinement()
        out = tmp_path / "doc.md"
        path = save_corrections_report("doc.pdf", meta, out)
        assert path is not None and path.exists()
        text = path.read_text(encoding="utf-8")
        assert path.name == "doc.corrections.md"
        assert "# Refinement Corrections — doc.pdf" in text
        assert "## Detailed Corrections" in text

    def test_returns_none_when_no_refinement(self, tmp_path: Path):
        out = tmp_path / "doc.md"
        assert save_corrections_report("doc.pdf", {"model": "x"}, out) is None
