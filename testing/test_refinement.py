"""Tests for the shared refinement orchestration — unparseable-response handling.

Covers the ``PARSE_ERROR`` path: a refinement response that survives none of the
four repair attempts must never replace the Markdown extracted so far, and must
stop the loop rather than let it burn every remaining iteration.
"""

from __future__ import annotations

from pathlib import Path

import src.refinement as refinement_mod
from src.refinement import _parse_refinement_response, run_conversion

_UNPARSEABLE = 'Sorry — here is the audit: {"iteration_summary": ' * 3


def test_parse_fallback_omits_corrected_markdown():
    """The fallback must not hand the raw blob back as the corrected document."""
    parsed = _parse_refinement_response(_UNPARSEABLE)

    assert parsed["iteration_summary"]["verdict"] == "PARSE_ERROR"
    assert "corrected_markdown" not in parsed
    # A caller reading it with a default therefore keeps what it already had.
    assert parsed.get("corrected_markdown", "# Kept") == "# Kept"


def _run(monkeypatch, minimal_pdf: Path, refine_returns: list[str]) -> tuple[str, dict, list[int]]:
    """Drive run_conversion over a scripted transport; return (md, metadata, call log)."""
    monkeypatch.setattr(refinement_mod, "_save_raw_response", lambda *a, **k: None)

    calls: list[int] = []

    def build_transports(extraction_prompt: str, refinement_prompt: str):
        def extract() -> tuple[str, dict]:
            return "# Good extraction\n\nReal content.", {"input_tokens": 10, "output_tokens": 20}

        def refine(_user_message: str) -> tuple[str, dict]:
            calls.append(len(calls))
            return refine_returns[len(calls) - 1], {"input_tokens": 5, "output_tokens": 5}

        return extract, refine

    markdown, metadata = run_conversion(
        backend_name="test-backend",
        display_name="Test backend",
        model_id="test-model",
        auth_mode="api",
        model_phrase="with model=",
        pdf_path=minimal_pdf,
        refine_iterations=3,
        clean_stop_max_errors=0,
        diminishing_returns_enabled=True,
        dry_run=False,
        extraction_prompt_file="prompts/extraction_rag.md",
        refinement_prompt_file="prompts/refinement_rag.md",
        verbose_save_dir=None,
        verbose_file_stem="",
        build_transports=build_transports,
    )
    return markdown, metadata, calls


def test_parse_error_keeps_extraction_and_stops_the_loop(monkeypatch, minimal_pdf):
    markdown, metadata, calls = _run(
        monkeypatch, minimal_pdf, [_UNPARSEABLE, _UNPARSEABLE, _UNPARSEABLE]
    )

    # The good extraction survives — not overwritten by the unparseable blob.
    assert markdown == "# Good extraction\n\nReal content."
    # And the loop stopped instead of re-auditing nothing twice more.
    assert len(calls) == 1
    assert metadata["iterations_completed"] == 1
    assert metadata["final_verdict"] == "PARSE_ERROR"
    # The failed pass contributed no markdown revision.
    assert metadata["iteration_markdowns"] == ["# Good extraction\n\nReal content."]
    # The raw response is still carried for post-mortem.
    assert metadata["raw_responses"][-1]["raw_text"] == _UNPARSEABLE


def test_parse_error_after_a_good_pass_keeps_the_good_pass(monkeypatch, minimal_pdf):
    good = '{"iteration_summary": {"errors_found": 2, "verdict": "IMPROVED"}, ' \
           '"corrections": [], "corrected_markdown": "# Refined once"}'

    markdown, metadata, calls = _run(monkeypatch, minimal_pdf, [good, _UNPARSEABLE, good])

    assert markdown == "# Refined once"
    assert len(calls) == 2
    assert metadata["iterations_completed"] == 2
    assert metadata["final_verdict"] == "PARSE_ERROR"
