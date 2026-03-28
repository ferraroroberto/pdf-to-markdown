"""Tests for src/models.py — data model properties and behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.models import BatchResult, ChunkResult, ConversionResult, ValidationReport


# ---------------------------------------------------------------------------
# ValidationReport
# ---------------------------------------------------------------------------


def _make_report(**kwargs) -> ValidationReport:
    defaults = dict(
        char_similarity=0.90,
        source_char_count=1000,
        output_char_count=950,
        heading_count=3,
        table_count=1,
        list_item_count=5,
        table_row_consistency=True,
        warnings=[],
    )
    defaults.update(kwargs)
    return ValidationReport(**defaults)


class TestValidationReport:
    def test_passed_high_similarity_no_warnings(self):
        report = _make_report(char_similarity=0.90, warnings=[])
        assert report.passed is True

    def test_passed_exactly_at_threshold(self):
        report = _make_report(char_similarity=0.85, warnings=[])
        assert report.passed is True

    def test_failed_below_threshold(self):
        report = _make_report(char_similarity=0.84, warnings=[])
        assert report.passed is False

    def test_failed_when_critical_warning_present(self):
        report = _make_report(
            char_similarity=0.90,
            warnings=["CRITICAL: Output is only 20% of source characters"],
        )
        assert report.passed is False

    def test_has_critical_warnings_true(self):
        report = _make_report(warnings=["CRITICAL: something bad"])
        assert report.has_critical_warnings is True

    def test_has_critical_warnings_false_for_non_critical(self):
        report = _make_report(warnings=["Character similarity is 80%"])
        assert report.has_critical_warnings is False

    def test_has_critical_warnings_false_when_no_warnings(self):
        report = _make_report(warnings=[])
        assert report.has_critical_warnings is False

    def test_summary_contains_pass(self):
        report = _make_report(char_similarity=0.90, warnings=[])
        assert "PASS" in report.summary()

    def test_summary_contains_fail(self):
        report = _make_report(char_similarity=0.50, warnings=[])
        assert "FAIL" in report.summary()

    def test_summary_contains_similarity_percentage(self):
        report = _make_report(char_similarity=0.90, warnings=[])
        assert "90.0%" in report.summary()

    def test_summary_lists_warnings(self):
        report = _make_report(warnings=["Table row column counts are inconsistent"])
        assert "Table row" in report.summary()


# ---------------------------------------------------------------------------
# ConversionResult
# ---------------------------------------------------------------------------


class TestConversionResult:
    def test_token_estimate_is_chars_div_4(self):
        result = ConversionResult(
            source=Path("doc.pdf"),
            markdown="a" * 400,
            backend_used="pdfplumber",
        )
        assert result.token_estimate == 100

    def test_token_estimate_zero_for_empty(self):
        result = ConversionResult(
            source=Path("doc.pdf"),
            markdown="",
            backend_used="pdfplumber",
        )
        assert result.token_estimate == 0

    def test_page_count_from_metadata(self):
        result = ConversionResult(
            source=Path("doc.pdf"),
            markdown="text",
            backend_used="marker",
            metadata={"page_count": 12},
        )
        assert result.page_count == 12

    def test_page_count_none_when_not_in_metadata(self):
        result = ConversionResult(
            source=Path("doc.pdf"),
            markdown="text",
            backend_used="marker",
        )
        assert result.page_count is None

    def test_save_writes_markdown_to_file(self, tmp_path):
        out = tmp_path / "output.md"
        result = ConversionResult(
            source=Path("doc.pdf"),
            markdown="# Hello\n\nWorld",
            backend_used="pdfplumber",
        )
        saved = result.save(out)
        assert saved == out
        assert out.read_text(encoding="utf-8") == "# Hello\n\nWorld"

    def test_save_creates_parent_dirs(self, tmp_path):
        out = tmp_path / "deep" / "nested" / "output.md"
        result = ConversionResult(
            source=Path("doc.pdf"),
            markdown="content",
            backend_used="pdfplumber",
        )
        result.save(out)
        assert out.exists()

    def test_save_returns_path_object(self, tmp_path):
        out = tmp_path / "out.md"
        result = ConversionResult(source=Path("x.pdf"), markdown="x", backend_used="b")
        returned = result.save(out)
        assert isinstance(returned, Path)


# ---------------------------------------------------------------------------
# ChunkResult
# ---------------------------------------------------------------------------


def _make_chunk(**kwargs) -> ChunkResult:
    defaults = dict(
        source=Path("doc.pdf"),
        chunk_idx=0,
        chunk_pages="0-9",
        markdown="# Chunk",
        backend_used="vertexai",
        error=None,
    )
    defaults.update(kwargs)
    return ChunkResult(**defaults)


class TestChunkResult:
    def test_failed_false_when_no_error(self):
        chunk = _make_chunk(error=None)
        assert chunk.failed is False

    def test_failed_true_when_error_set(self):
        chunk = _make_chunk(error="API timeout")
        assert chunk.failed is True

    def test_failed_true_for_empty_string_error(self):
        # The failed check uses `is not None`, so even "" is treated as failed
        chunk = _make_chunk(error="")
        assert chunk.failed is True


# ---------------------------------------------------------------------------
# BatchResult
# ---------------------------------------------------------------------------


class TestBatchResult:
    def _make_batch(self) -> BatchResult:
        chunks = [
            _make_chunk(
                source=Path("a.pdf"),
                chunk_idx=0,
                metadata={"total_input_tokens": 100, "total_output_tokens": 50, "total_tokens": 150},
            ),
            _make_chunk(
                source=Path("a.pdf"),
                chunk_idx=1,
                metadata={"total_input_tokens": 200, "total_output_tokens": 80, "total_tokens": 280},
            ),
            _make_chunk(
                source=Path("b.pdf"),
                chunk_idx=0,
                metadata={"total_input_tokens": 300, "total_output_tokens": 120, "total_tokens": 420},
                error="extraction failed",
            ),
        ]
        return BatchResult(folder=Path("docs/"), results=chunks)

    def test_total_input_tokens(self):
        batch = self._make_batch()
        assert batch.total_input_tokens == 600

    def test_total_output_tokens(self):
        batch = self._make_batch()
        assert batch.total_output_tokens == 250

    def test_total_tokens(self):
        batch = self._make_batch()
        assert batch.total_tokens == 850

    def test_file_count_counts_unique_sources(self):
        batch = self._make_batch()
        assert batch.file_count == 2  # a.pdf and b.pdf

    def test_failed_count(self):
        batch = self._make_batch()
        assert batch.failed_count == 1

    def test_empty_batch(self):
        batch = BatchResult(folder=Path("."))
        assert batch.total_tokens == 0
        assert batch.file_count == 0
        assert batch.failed_count == 0
