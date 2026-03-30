"""Tests for JSON repair helpers in src/backends/vertexai_backend.py."""

from __future__ import annotations

import json

import pytest

from src.backends.vertexai_backend import (
    _parse_refinement_response,
    _remove_trailing_commas,
    _repair_json_escapes,
    _repair_truncated_json,
)

# ---------------------------------------------------------------------------
# _remove_trailing_commas
# ---------------------------------------------------------------------------

class TestRemoveTrailingCommas:
    def test_trailing_comma_before_closing_brace(self):
        result = _remove_trailing_commas('{"a": 1,}')
        assert json.loads(result) == {"a": 1}

    def test_trailing_comma_before_closing_bracket(self):
        result = _remove_trailing_commas('[1, 2, 3,]')
        assert json.loads(result) == [1, 2, 3]

    def test_trailing_comma_with_whitespace(self):
        result = _remove_trailing_commas('{"a": 1,  \n}')
        assert json.loads(result) == {"a": 1}

    def test_no_modification_when_valid(self):
        text = '{"a": 1, "b": 2}'
        assert _remove_trailing_commas(text) == text

    def test_nested_trailing_commas(self):
        text = '{"a": [1, 2,], "b": {"c": 3,},}'
        result = _remove_trailing_commas(text)
        assert json.loads(result) == {"a": [1, 2], "b": {"c": 3}}

# ---------------------------------------------------------------------------
# _repair_truncated_json
# ---------------------------------------------------------------------------

class TestRepairTruncatedJson:
    def test_returns_none_for_balanced_json(self):
        assert _repair_truncated_json('{"a": "b"}') is None

    def test_closes_unterminated_string_in_value(self):
        truncated = '{"corrected_markdown": "Hello world'
        repaired = _repair_truncated_json(truncated)
        assert repaired is not None
        parsed = json.loads(repaired)
        assert parsed["corrected_markdown"] == "Hello world"

    def test_closes_unterminated_string_and_open_objects(self):
        truncated = '{"corrections": [{"pdf_says": "Resumen\\nExpediente'
        repaired = _repair_truncated_json(truncated)
        assert repaired is not None
        parsed = json.loads(repaired)
        assert "corrections" in parsed
        assert parsed["corrections"][0]["pdf_says"] == "Resumen\nExpediente"

    def test_closes_open_array_only(self):
        truncated = '{"items": [1, 2, 3'
        repaired = _repair_truncated_json(truncated)
        assert repaired is not None
        parsed = json.loads(repaired)
        assert parsed["items"] == [1, 2, 3]

    def test_closes_multiple_nested_structures(self):
        truncated = '{"a": {"b": ["x", "y"'
        repaired = _repair_truncated_json(truncated)
        assert repaired is not None
        parsed = json.loads(repaired)
        assert parsed["a"]["b"] == ["x", "y"]

    def test_escaped_backslash_inside_string_not_confused(self):
        truncated = '{"value": "path\\\\end'
        repaired = _repair_truncated_json(truncated)
        assert repaired is not None
        parsed = json.loads(repaired)
        assert parsed["value"] == "path\\end"

# ---------------------------------------------------------------------------
# _parse_refinement_response — integration across the repair chain
# ---------------------------------------------------------------------------

_VALID_RESPONSE = json.dumps({
    "iteration_summary": {
        "iteration": 1,
        "errors_found": 2,
        "content_errors": 1,
        "table_errors": 0,
        "structure_errors": 1,
        "noise_errors": 0,
        "critical": 0,
        "moderate": 1,
        "minor": 1,
        "verdict": "NEEDS ANOTHER PASS",
    },
    "corrections": [],
    "corrected_markdown": "# Title\n\nBody text.",
})

class TestParseRefinementResponse:
    def test_valid_json(self):
        result = _parse_refinement_response(_VALID_RESPONSE)
        assert result["iteration_summary"]["verdict"] == "NEEDS ANOTHER PASS"
        assert result["corrected_markdown"] == "# Title\n\nBody text."

    def test_strips_markdown_fences(self):
        fenced = f"```json\n{_VALID_RESPONSE}\n```"
        result = _parse_refinement_response(fenced)
        assert result["iteration_summary"]["errors_found"] == 2

    def test_repairs_latex_backslash_escapes(self):
        text = (
            '{"iteration_summary": {"iteration": 1, "errors_found": 0,'
            ' "content_errors": 0, "table_errors": 0, "structure_errors": 0,'
            ' "noise_errors": 0, "critical": 0, "moderate": 0, "minor": 0,'
            ' "verdict": "CLEAN"}, "corrections": [],'
            ' "corrected_markdown": "\\alpha + \\beta"}'
        )
        result = _parse_refinement_response(text)
        assert result["iteration_summary"]["verdict"] == "CLEAN"
        assert "alpha" in result["corrected_markdown"]

    def test_repairs_trailing_comma(self):
        text = (
            '{"iteration_summary": {"iteration": 1, "errors_found": 0,'
            ' "content_errors": 0, "table_errors": 0, "structure_errors": 0,'
            ' "noise_errors": 0, "critical": 0, "moderate": 0, "minor": 0,'
            ' "verdict": "CLEAN",}, "corrections": [],'
            ' "corrected_markdown": "ok",}'
        )
        result = _parse_refinement_response(text)
        assert result["iteration_summary"]["verdict"] == "CLEAN"

    def test_recovers_truncated_corrected_markdown(self):
        full = {
            "iteration_summary": {
                "iteration": 2, "errors_found": 1,
                "content_errors": 1, "table_errors": 0,
                "structure_errors": 0, "noise_errors": 0,
                "critical": 0, "moderate": 1, "minor": 0,
                "verdict": "NEEDS ANOTHER PASS",
            },
            "corrections": [],
            "corrected_markdown": "# Title\n\nThis is a long document",
        }
        raw = json.dumps(full)
        truncated = raw[: raw.index("long document") + 4]
        result = _parse_refinement_response(truncated)
        assert result["iteration_summary"]["verdict"] != "PARSE_ERROR"
        assert "corrected_markdown" in result

    def test_recovers_truncated_mid_corrections(self):
        text = (
            '{"iteration_summary": {"iteration": 1, "errors_found": 1,'
            ' "content_errors": 1, "table_errors": 0, "structure_errors": 0,'
            ' "noise_errors": 0, "critical": 0, "moderate": 1, "minor": 0,'
            ' "verdict": "NEEDS ANOTHER PASS"}, '
            '"corrections": [{"location": "p1", "category": "content",'
            ' "severity": "moderate", "pdf_says": "Propuesta ADV GPS\\nPropuesta'
        )
        result = _parse_refinement_response(text)
        assert result["iteration_summary"]["verdict"] != "PARSE_ERROR"
        assert len(result["corrections"]) >= 1

    def test_fallback_on_unrecoverable_json(self):
        result = _parse_refinement_response("this is not json at all {{{")
        assert result["iteration_summary"]["verdict"] == "PARSE_ERROR"
        assert result["corrections"] == []
