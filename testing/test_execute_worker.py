"""Tests for Convert File worker artifact cleanup."""

from __future__ import annotations

from src.execute_worker import erase_prior_execution_artifacts


def test_cleanup_preserves_resume_markdown_but_removes_stale_chunk_pdfs(tmp_path):
    stem = "report"
    chunk_pdf = tmp_path / f"{stem}.chunk_001.pdf"
    chunk_md = tmp_path / f"{stem}.chunk_001.md"
    chunk_corrections = tmp_path / f"{stem}.chunk_001.corrections.md"
    raw_response = tmp_path / f"{stem}.raw_step_00.txt"

    chunk_pdf.write_bytes(b"stale")
    chunk_md.write_text("# already converted", encoding="utf-8")
    chunk_corrections.write_text("corrections", encoding="utf-8")
    raw_response.write_text("old raw response", encoding="utf-8")

    erase_prior_execution_artifacts(tmp_path, stem)

    assert not chunk_pdf.exists()
    assert chunk_md.exists()
    assert chunk_corrections.exists()
    assert not raw_response.exists()
