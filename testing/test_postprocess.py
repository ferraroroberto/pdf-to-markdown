"""Tests for src/postprocess.py — markdown cleaning pipeline."""

from __future__ import annotations

import pytest

from src.postprocess import (
    _compact_tables,
    _fix_broken_paragraphs,
    _normalize_whitespace,
    _strip_headers_footers,
    _strip_page_numbers,
    postprocess,
)


# ---------------------------------------------------------------------------
# _strip_headers_footers
# ---------------------------------------------------------------------------


class TestStripHeadersFooters:
    def test_removes_line_repeated_three_or_more_times(self):
        text = "Header\nContent A\nHeader\nContent B\nHeader\nContent C"
        result = _strip_headers_footers(text)
        assert "Header" not in result

    def test_keeps_line_repeated_fewer_than_three_times(self):
        text = "Title\nContent\nTitle\nMore content"
        result = _strip_headers_footers(text)
        assert result.count("Title") == 2

    def test_keeps_long_lines_even_if_repeated(self):
        # Lines >= 80 chars are never treated as headers/footers
        long_line = "A" * 80
        text = f"{long_line}\nContent\n{long_line}\nOther\n{long_line}\nFinal"
        result = _strip_headers_footers(text)
        assert result.count(long_line) == 3

    def test_removes_exact_blank_header_whitespace(self):
        # A line with only whitespace (stripped = "") has length 0, so it's excluded
        text = "\n\nContent\n\n"
        result = _strip_headers_footers(text)
        # Blank lines should remain (they're excluded from the repetition check)
        assert result == text

    def test_content_lines_preserved(self):
        repeated = "Company Name"
        text = "\n".join(
            [repeated, "Unique line 1", repeated, "Unique line 2", repeated, "Unique line 3"]
        )
        result = _strip_headers_footers(text)
        assert "Unique line 1" in result
        assert "Unique line 2" in result
        assert "Unique line 3" in result


# ---------------------------------------------------------------------------
# _strip_page_numbers
# ---------------------------------------------------------------------------


class TestStripPageNumbers:
    @pytest.mark.parametrize(
        "line",
        [
            "42",
            "  7  ",
            "Page 3",
            "page 10",
            "- 5 -",
            "3 of 10",
            "1 OF 20",
        ],
    )
    def test_removes_page_number_patterns(self, line):
        text = f"Before\n{line}\nAfter"
        result = _strip_page_numbers(text)
        lines = result.split("\n")
        assert all(l.strip() != line.strip() for l in lines)

    def test_keeps_regular_content(self):
        text = "Section 3: Results\nSee figure 2 on page 5.\n42 items found"
        result = _strip_page_numbers(text)
        assert "Section 3: Results" in result
        assert "See figure 2 on page 5." in result
        assert "42 items found" in result

    def test_removes_only_standalone_numbers(self):
        text = "There are 42 items\n42\nEnd"
        result = _strip_page_numbers(text)
        assert "There are 42 items" in result
        assert "\n42\n" not in result


# ---------------------------------------------------------------------------
# _fix_broken_paragraphs
# ---------------------------------------------------------------------------


class TestFixBrokenParagraphs:
    def test_joins_lines_split_mid_sentence(self):
        text = "This sentence was split\nacross two lines"
        result = _fix_broken_paragraphs(text)
        assert "split across two lines" in result

    def test_no_join_after_sentence_ending_punctuation(self):
        text = "First sentence.\nsecond sentence starts here"
        result = _fix_broken_paragraphs(text)
        # Should NOT join because the first ends with '.'
        assert "First sentence." in result
        assert "second sentence starts here" in result

    def test_no_join_heading_lines(self):
        text = "## Section title\ncontinued text"
        result = _fix_broken_paragraphs(text)
        assert "## Section title\ncontinued text" in result

    def test_no_join_table_rows(self):
        text = "| Cell A | Cell B |\ncontinued"
        result = _fix_broken_paragraphs(text)
        # Table row should not be joined with next line
        assert "| Cell A | Cell B |" in result

    def test_no_join_list_items(self):
        text = "- Item one\ncontinued item"
        result = _fix_broken_paragraphs(text)
        assert "- Item one\ncontinued item" in result

    def test_no_join_blank_line(self):
        text = "Paragraph one\n\nParagraph two"
        result = _fix_broken_paragraphs(text)
        assert "\n\n" in result

    def test_next_line_uppercase_not_joined(self):
        text = "First part of text\nSecond part starts uppercase"
        result = _fix_broken_paragraphs(text)
        # Next line starts with 'S' (uppercase) → should NOT be joined
        assert "\n" in result


# ---------------------------------------------------------------------------
# _compact_tables
# ---------------------------------------------------------------------------


class TestCompactTables:
    def test_trims_cell_whitespace(self):
        text = "|  Name  |  Value  |\n|  Alice  |  42  |"
        result = _compact_tables(text)
        assert "| Name | Value |" in result
        assert "| Alice | 42 |" in result

    def test_separator_rows_normalised(self):
        text = "|---|---|"
        result = _compact_tables(text)
        # Separators go through the same path; should not raise
        assert "|" in result

    def test_non_table_lines_unchanged(self):
        text = "Regular paragraph\n| A | B |\nAnother paragraph"
        result = _compact_tables(text)
        assert "Regular paragraph" in result
        assert "Another paragraph" in result


# ---------------------------------------------------------------------------
# _normalize_whitespace
# ---------------------------------------------------------------------------


class TestNormalizeWhitespace:
    def test_strips_trailing_spaces(self):
        text = "line one   \nline two  "
        result = _normalize_whitespace(text)
        for line in result.split("\n"):
            assert line == line.rstrip()

    def test_collapses_four_or_more_blank_lines(self):
        text = "A\n\n\n\n\n\nB"
        result = _normalize_whitespace(text)
        assert "\n\n\n\n" not in result

    def test_preserves_up_to_three_blank_lines(self):
        text = "A\n\n\nB"
        result = _normalize_whitespace(text)
        assert "A\n\n\nB" in result


# ---------------------------------------------------------------------------
# postprocess (integration)
# ---------------------------------------------------------------------------


class TestPostprocess:
    def test_all_steps_run_by_default(self):
        # Repeated header, page number, trailing spaces
        text = "Header\nText   \n1\nHeader\nMore\n2\nHeader\nEnd\n3"
        result = postprocess(text)
        assert "Header" not in result
        # bare page numbers removed
        assert "\n1\n" not in result and "\n2\n" not in result and "\n3\n" not in result
        # no trailing spaces
        for line in result.split("\n"):
            assert line == line.rstrip()

    def test_individual_step_can_be_disabled(self):
        text = "42\nContent"
        # With strip_page_numbers disabled, "42" should remain
        result = postprocess(text, strip_page_numbers=False)
        assert "42" in result

    def test_empty_string_returns_empty(self):
        assert postprocess("") == ""

    def test_idempotent(self):
        text = "# Heading\n\nSome content with **bold** text.\n\n- Item one\n- Item two\n"
        once = postprocess(text)
        twice = postprocess(once)
        assert once == twice
