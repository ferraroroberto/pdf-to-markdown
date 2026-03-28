"""Tests for src/chunker.py — PDF splitting and markdown merging."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.chunker import _CHUNK_SEPARATOR, cleanup_chunks, merge_chunks, split_pdf


# ---------------------------------------------------------------------------
# merge_chunks
# ---------------------------------------------------------------------------


class TestMergeChunks:
    def test_single_chunk_no_separator(self):
        result = merge_chunks(["# Hello"])
        assert result == "# Hello"
        assert _CHUNK_SEPARATOR not in result

    def test_multiple_chunks_joined_with_separator(self):
        result = merge_chunks(["Part one", "Part two", "Part three"])
        assert result == f"Part one{_CHUNK_SEPARATOR}Part two{_CHUNK_SEPARATOR}Part three"

    def test_empty_strings_filtered_out(self):
        result = merge_chunks(["Part one", "", "Part three"])
        assert "Part one" in result
        assert "Part three" in result
        # Empty chunk should not introduce a double separator
        assert f"{_CHUNK_SEPARATOR}{_CHUNK_SEPARATOR}" not in result

    def test_whitespace_only_strings_filtered_out(self):
        result = merge_chunks(["Content", "   ", "\n\n"])
        assert result == "Content"

    def test_empty_list_returns_empty_string(self):
        assert merge_chunks([]) == ""

    def test_all_empty_returns_empty_string(self):
        assert merge_chunks(["", " ", "\t"]) == ""

    def test_strips_leading_trailing_whitespace_per_chunk(self):
        result = merge_chunks(["  chunk one  ", "  chunk two  "])
        parts = result.split(_CHUNK_SEPARATOR)
        assert parts[0] == "chunk one"
        assert parts[1] == "chunk two"

    def test_separator_is_horizontal_rule(self):
        # The separator should include "---" so it renders as a page break in Markdown
        assert "---" in _CHUNK_SEPARATOR


# ---------------------------------------------------------------------------
# split_pdf — validation
# ---------------------------------------------------------------------------


class TestSplitPdfValidation:
    def test_raises_for_zero_chunk_size(self, minimal_pdf):
        with pytest.raises(ValueError, match="chunk_size must be >= 1"):
            split_pdf(minimal_pdf, chunk_size=0)

    def test_raises_for_negative_chunk_size(self, minimal_pdf):
        with pytest.raises(ValueError, match="chunk_size must be >= 1"):
            split_pdf(minimal_pdf, chunk_size=-5)


# ---------------------------------------------------------------------------
# split_pdf — functional (uses minimal_pdf fixture = 5 pages)
# ---------------------------------------------------------------------------


class TestSplitPdfFunctional:
    def test_returns_list_of_tuples(self, minimal_pdf):
        chunks = split_pdf(minimal_pdf, chunk_size=3)
        assert isinstance(chunks, list)
        assert all(len(t) == 4 for t in chunks)

    def test_chunk_files_exist(self, minimal_pdf):
        chunks = split_pdf(minimal_pdf, chunk_size=3)
        for _, chunk_path, _, _ in chunks:
            assert chunk_path.exists()

    def test_chunk_files_are_pdfs(self, minimal_pdf):
        chunks = split_pdf(minimal_pdf, chunk_size=3)
        for _, chunk_path, _, _ in chunks:
            assert chunk_path.suffix == ".pdf"

    def test_five_pages_chunk2_produces_three_chunks(self, minimal_pdf):
        # 5 pages with chunk_size=2 → chunks covering 0-1, 2-3, 4-4
        chunks = split_pdf(minimal_pdf, chunk_size=2)
        assert len(chunks) == 3

    def test_chunk_size_larger_than_doc_produces_one_chunk(self, minimal_pdf):
        chunks = split_pdf(minimal_pdf, chunk_size=100)
        assert len(chunks) == 1

    def test_chunk_indices_are_sequential(self, minimal_pdf):
        chunks = split_pdf(minimal_pdf, chunk_size=2)
        indices = [c[0] for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_page_ranges_are_contiguous(self, minimal_pdf):
        # Without overlap: end of chunk N should be just before start of chunk N+1 content
        chunks = split_pdf(minimal_pdf, chunk_size=2, overlap=0)
        # start_page of chunk 1 should be end_page of chunk 0 + 1
        if len(chunks) >= 2:
            assert chunks[1][2] == chunks[0][3] + 1

    def test_overlap_extends_start_page(self, minimal_pdf):
        # With overlap=1, chunk 1 should start 1 page before chunk 0 ended
        chunks = split_pdf(minimal_pdf, chunk_size=2, overlap=1)
        if len(chunks) >= 2:
            _, _, start1, _ = chunks[1]
            _, _, _, end0 = chunks[0]
            assert start1 == end0  # overlaps by 1 page

    def test_chunks_dir_created_next_to_pdf(self, minimal_pdf):
        split_pdf(minimal_pdf, chunk_size=3)
        chunks_dir = minimal_pdf.parent / f"_chunks_{minimal_pdf.stem}"
        assert chunks_dir.exists()


# ---------------------------------------------------------------------------
# cleanup_chunks
# ---------------------------------------------------------------------------


class TestCleanupChunks:
    def test_no_op_when_chunks_dir_does_not_exist(self, tmp_path):
        fake_pdf = tmp_path / "nonexistent.pdf"
        # Should not raise even though the dir doesn't exist
        cleanup_chunks(fake_pdf)

    def test_removes_chunks_dir(self, minimal_pdf):
        split_pdf(minimal_pdf, chunk_size=3)
        chunks_dir = minimal_pdf.parent / f"_chunks_{minimal_pdf.stem}"
        assert chunks_dir.exists()

        cleanup_chunks(minimal_pdf)
        assert not chunks_dir.exists()

    def test_double_cleanup_is_safe(self, minimal_pdf):
        split_pdf(minimal_pdf, chunk_size=3)
        cleanup_chunks(minimal_pdf)
        # Second call should not raise
        cleanup_chunks(minimal_pdf)
