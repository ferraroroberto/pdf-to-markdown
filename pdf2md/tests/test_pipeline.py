"""Unit tests for pdf2md — pure-Python components only (no PDFs or ML backends)."""

from __future__ import annotations

import pytest

from pdf2md.models import ConversionResult, ValidationReport
from pdf2md.postprocess import postprocess
from pdf2md.validation import _strip_markdown, _count_tables, _check_table_row_consistency


# ======================================================================
# PostProcess tests
# ======================================================================


class TestStripPageNumbers:
    def test_strip_page_numbers(self):
        text = "Hello world\n42\nContent here\nPage 7\nMore content"
        result = postprocess(text, strip_headers_footers=False, fix_broken_paragraphs=False)
        assert "Hello world" in result
        assert "Content here" in result
        assert "More content" in result
        assert "\n42\n" not in result
        assert "Page 7" not in result


class TestNormalizeWhitespace:
    def test_normalize_whitespace(self):
        text = "Line 1\n\n\n\n\n\nLine 2\n\n\n\n\n\n\nLine 3"
        result = postprocess(text, strip_headers_footers=False,
                             strip_page_numbers=False,
                             fix_broken_paragraphs=False,
                             compact_tables=False)
        # 4+ newlines should collapse to 3
        assert "\n\n\n\n" not in result
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result


class TestStripRepeatedLines:
    def test_strip_repeated_lines(self):
        lines = ["COMPANY CONFIDENTIAL"] * 4 + ["Actual content paragraph."]
        text = "\n".join(lines)
        result = postprocess(text, strip_page_numbers=False,
                             fix_broken_paragraphs=False, compact_tables=False)
        assert "COMPANY CONFIDENTIAL" not in result
        assert "Actual content paragraph." in result


class TestCompactTables:
    def test_compact_tables(self):
        text = "|  Name   |  Value   |\n| --- | --- |\n|  Alice  |  100  |"
        result = postprocess(text, strip_headers_footers=False,
                             strip_page_numbers=False,
                             fix_broken_paragraphs=False,
                             normalize_whitespace=False)
        assert "| Name | Value |" in result
        assert "| Alice | 100 |" in result


class TestFixBrokenParagraphs:
    def test_fix_broken_paragraphs(self):
        text = "This is a sentence that was\nsplit across two lines."
        result = postprocess(text, strip_headers_footers=False,
                             strip_page_numbers=False,
                             compact_tables=False,
                             normalize_whitespace=False)
        assert "sentence that was split across two lines." in result


class TestPreservesHeadings:
    def test_preserves_headings(self):
        text = "# Title\n\nSome paragraph.\n\n## Subtitle\n\nAnother paragraph."
        result = postprocess(text)
        assert "# Title" in result
        assert "## Subtitle" in result


# ======================================================================
# StripMarkdown tests
# ======================================================================


class TestStripMarkdown:
    def test_heading_markers_removed(self):
        result = _strip_markdown("# Heading One\n## Heading Two")
        assert "Heading One" in result
        assert "Heading Two" in result
        assert "#" not in result

    def test_bold_markers_removed(self):
        result = _strip_markdown("This is **bold** text")
        assert "bold" in result
        assert "**" not in result

    def test_link_syntax_removed(self):
        result = _strip_markdown("Click [here](https://example.com) now")
        assert "here" in result
        assert "https://example.com" not in result
        assert "[" not in result

    def test_table_separators_removed(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = _strip_markdown(md)
        assert "---" not in result
        assert "A" in result
        assert "1" in result


# ======================================================================
# TableCounting tests
# ======================================================================


class TestTableCounting:
    def test_single_table(self):
        md = "Text before\n| A | B |\n|---|---|\n| 1 | 2 |\nText after"
        assert _count_tables(md) == 1

    def test_two_tables(self):
        md = (
            "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "Some text in between\n\n"
            "| X | Y |\n|---|---|\n| 3 | 4 |"
        )
        assert _count_tables(md) == 2

    def test_no_tables(self):
        md = "Just a paragraph.\nAnother line."
        assert _count_tables(md) == 0


# ======================================================================
# TableConsistency tests
# ======================================================================


class TestTableConsistency:
    def test_consistent_columns(self):
        md = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |"
        assert _check_table_row_consistency(md) is True

    def test_inconsistent_columns(self):
        md = "| A | B | C |\n|---|---|---|\n| 1 | 2 |\n| 4 | 5 | 6 |"
        assert _check_table_row_consistency(md) is False


# ======================================================================
# Model tests
# ======================================================================


class TestModels:
    def test_token_estimate(self):
        md = "x" * 400
        result = ConversionResult(
            source=__file__,
            markdown=md,
            backend_used="test",
        )
        assert result.token_estimate == 100

    def test_validation_passed(self):
        report = ValidationReport(
            char_similarity=0.95,
            source_char_count=1000,
            output_char_count=950,
            heading_count=5,
            table_count=2,
            list_item_count=10,
            table_row_consistency=True,
            warnings=[],
        )
        assert report.passed is True

    def test_validation_not_passed_low_similarity(self):
        report = ValidationReport(
            char_similarity=0.70,
            source_char_count=1000,
            output_char_count=700,
            heading_count=5,
            table_count=2,
            list_item_count=10,
            table_row_consistency=True,
            warnings=[],
        )
        assert report.passed is False

    def test_validation_not_passed_critical_warning(self):
        report = ValidationReport(
            char_similarity=0.95,
            source_char_count=1000,
            output_char_count=950,
            heading_count=5,
            table_count=2,
            list_item_count=10,
            table_row_consistency=True,
            warnings=["CRITICAL: Something went wrong"],
        )
        assert report.passed is False

    def test_validation_summary_contains_pass(self):
        report = ValidationReport(
            char_similarity=0.95,
            source_char_count=1000,
            output_char_count=950,
            heading_count=5,
            table_count=2,
            list_item_count=10,
            table_row_consistency=True,
            warnings=[],
        )
        summary = report.summary()
        assert "PASS" in summary
        assert "95" in summary
