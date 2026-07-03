"""Tests for structured execution logging."""

from __future__ import annotations

from src.logger_exec import log_conversion_steps


def test_log_conversion_steps_uses_reported_step_total_tokens(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr("src.logger_exec.append_row", rows.append)

    log_conversion_steps(
        file="doc.pdf",
        chunk_idx=0,
        chunk_pages="all",
        meta={
            "extraction_step": {
                "step_input_tokens": 10,
                "step_output_tokens": 20,
                "step_total_tokens": 45,
            },
            "refinement_log": [
                {
                    "step": 1,
                    "step_type": "refinement",
                    "step_input_tokens": 30,
                    "step_output_tokens": 40,
                    "step_total_tokens": 95,
                    "errors_found": 1,
                    "critical": 0,
                    "moderate": 1,
                    "minor": 0,
                    "verdict": "NEEDS ANOTHER PASS",
                }
            ],
        },
        pricing_data={"gemini-test": {"input": 1.0, "output": 1.0}},
        model="gemini-test",
        auth_mode="api",
    )

    assert [row["total_tokens"] for row in rows] == [45, 95]
    assert rows[0]["input_tokens"] == 10
    assert rows[0]["output_tokens"] == 20
