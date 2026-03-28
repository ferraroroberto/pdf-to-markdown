"""Tests for src/validation.py — markdown quality validation helpers."""

from __future__ import annotations

import pytest

from src.validation import (
    _check_table_row_consistency,
    _compute_similarity,
    _count_headings,
    _count_list_items,
    _count_tables,
    _normalize_for_comparison,
    _strip_markdown,
)


# ---------------------------------------------------------------------------
# _strip_markdown
# ---------------------------------------------------------------------------


class TestStripMarkdown:
    def test_removes_atx_headings(self):
        result = _strip_markdown("# Title\n## Sub\n### Deep")
        assert "#" not in result
        assert "Title" in result
        assert "Sub" in result

    def test_removes_bold_markers(self):
        result = _strip_markdown("**bold** and __also bold__")
        assert "**" not in result
        assert "__" not in result
        assert "bold" in result

    def test_removes_italic_markers(self):
        result = _strip_markdown("*italic* and _also italic_")
        assert "*" not in result
        # Underscores in words can remain but formatting underscores removed
        assert "italic" in result

    def test_removes_link_syntax(self):
        result = _strip_markdown("[click here](https://example.com)")
        assert "click here" in result
        assert "https://example.com" not in result
        assert "[" not in result

    def test_removes_unordered_list_markers(self):
        result = _strip_markdown("- item one\n* item two\n+ item three")
        assert "item one" in result
        assert "item two" in result
        assert "item three" in result

    def test_removes_ordered_list_markers(self):
        result = _strip_markdown("1. First\n2. Second\n3) Third")
        assert "First" in result
        assert "Second" in result

    def test_removes_table_separators(self):
        result = _strip_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        assert "|---" not in result

    def test_collapses_whitespace(self):
        result = _strip_markdown("word1   word2\n\nword3")
        assert "  " not in result  # no multiple spaces


# ---------------------------------------------------------------------------
# _normalize_for_comparison
# ---------------------------------------------------------------------------


class TestNormalizeForComparison:
    def test_lowercases_text(self):
        result = _normalize_for_comparison("Hello WORLD")
        assert result == "hello world"

    def test_collapses_multiple_spaces(self):
        result = _normalize_for_comparison("a  b   c")
        assert result == "a b c"

    def test_collapses_newlines(self):
        result = _normalize_for_comparison("line one\nline two")
        assert "\n" not in result

    def test_strips_leading_trailing_whitespace(self):
        result = _normalize_for_comparison("  hello  ")
        assert result == "hello"


# ---------------------------------------------------------------------------
# _compute_similarity
# ---------------------------------------------------------------------------


class TestComputeSimilarity:
    def test_identical_strings_return_one(self):
        assert _compute_similarity("hello world", "hello world") == 1.0

    def test_both_empty_return_one(self):
        assert _compute_similarity("", "") == 1.0

    def test_one_empty_returns_zero(self):
        assert _compute_similarity("some text", "") == 0.0
        assert _compute_similarity("", "some text") == 0.0

    def test_partial_overlap_between_zero_and_one(self):
        ratio = _compute_similarity("hello world", "hello earth")
        assert 0.0 < ratio < 1.0

    def test_completely_different_strings_low_similarity(self):
        ratio = _compute_similarity("abcdefghij", "klmnopqrst")
        assert ratio < 0.5

    def test_similarity_is_symmetric(self):
        a = "The quick brown fox jumps over the lazy dog"
        b = "The slow brown cat jumps over the lazy dog"
        assert _compute_similarity(a, b) == pytest.approx(_compute_similarity(b, a))


# ---------------------------------------------------------------------------
# _count_headings
# ---------------------------------------------------------------------------


class TestCountHeadings:
    def test_counts_all_heading_levels(self):
        md = "# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6"
        assert _count_headings(md) == 6

    def test_no_headings_returns_zero(self):
        assert _count_headings("just plain text") == 0

    def test_inline_hashes_not_counted(self):
        md = "Color #ff0000 is red"
        assert _count_headings(md) == 0

    def test_heading_without_space_not_counted(self):
        # "#Title" without a space after # should not be counted
        assert _count_headings("#Title") == 0


# ---------------------------------------------------------------------------
# _count_tables
# ---------------------------------------------------------------------------


class TestCountTables:
    def test_single_table(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        assert _count_tables(md) == 1

    def test_two_separate_tables(self):
        md = "| A |\n|---|\n| 1 |\n\nSome text\n\n| B |\n|---|\n| 2 |"
        assert _count_tables(md) == 2

    def test_no_tables(self):
        assert _count_tables("No tables here") == 0

    def test_contiguous_rows_count_as_one_table(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        assert _count_tables(md) == 1


# ---------------------------------------------------------------------------
# _count_list_items
# ---------------------------------------------------------------------------


class TestCountListItems:
    def test_counts_unordered_items(self):
        md = "- one\n- two\n* three\n+ four"
        assert _count_list_items(md) == 4

    def test_counts_ordered_items(self):
        md = "1. First\n2. Second\n3) Third"
        assert _count_list_items(md) == 3

    def test_counts_mixed_list_items(self):
        md = "- bullet\n1. numbered"
        assert _count_list_items(md) == 2

    def test_no_list_items_returns_zero(self):
        assert _count_list_items("Just a paragraph.") == 0


# ---------------------------------------------------------------------------
# _check_table_row_consistency
# ---------------------------------------------------------------------------


class TestCheckTableRowConsistency:
    def test_consistent_table_returns_true(self):
        md = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |"
        assert _check_table_row_consistency(md) is True

    def test_inconsistent_table_returns_false(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 | extra |"
        assert _check_table_row_consistency(md) is False

    def test_no_tables_returns_true(self):
        assert _check_table_row_consistency("No tables here") is True

    def test_separator_rows_ignored_in_count(self):
        # Separator row |---|---| should not affect column count check
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        assert _check_table_row_consistency(md) is True

    def test_two_consistent_tables_both_pass(self):
        md = (
            "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "Some text\n\n"
            "| X | Y | Z |\n|---|---|---|\n| a | b | c |"
        )
        assert _check_table_row_consistency(md) is True

    def test_one_inconsistent_table_among_two_returns_false(self):
        md = (
            "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "Text\n\n"
            "| X | Y |\n|---|---|\n| a | b | extra |"
        )
        assert _check_table_row_consistency(md) is False
