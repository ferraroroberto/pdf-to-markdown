"""Vertex AI (Google Gemini) backend — cloud-based PDF extraction with optional iterative refinement."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

from src.backends.base import BaseBackend

logger = logging.getLogger("backends.vertexai")

# Default prompt locations relative to the project root
_DEFAULT_EXTRACTION_PROMPT = "prompts/extraction.md"
_DEFAULT_REFINEMENT_PROMPT = "prompts/refinement.md"

# Gemini model parameters
_TEMPERATURE = 0.2
_MAX_OUTPUT_TOKENS = 65536

# In JSON, \b \f \r are technically valid (backspace, form-feed, CR), but in
# LaTeX/Markdown documents they almost always signal \begin, \frac, \right etc.
# We deliberately exclude b, f, r so they get doubled like any other LaTeX command.
# We keep \n and \t because the model genuinely uses them for newlines/tabs in strings.
_VALID_SINGLE_ESCAPES: frozenset[str] = frozenset('"\\/nt')
_HEX_CHARS: frozenset[str] = frozenset('0123456789abcdefABCDEF')


def _load_config_clean_stop_max_errors() -> int | None:
    """Read clean_stop_max_errors from config.json, or return None if not set."""
    config_path = _project_root() / "config.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            val = data.get("vertexai", {}).get("clean_stop_max_errors")
            if val is not None:
                return int(val)
        except Exception:  # noqa: BLE001
            pass
    return None


def _project_root() -> Path:
    """Return the project root (3 levels up from this file: src/backends/vertexai_backend.py)."""
    return Path(__file__).parent.parent.parent


def _resolve_prompt_path(prompt_file: str) -> Path:
    """Resolve a prompt file path, relative to project root if not absolute."""
    p = Path(prompt_file)
    if not p.is_absolute():
        p = _project_root() / prompt_file
    return p


def _load_prompt(prompt_file: str) -> str:
    """Load a prompt from a Markdown file. Raises FileNotFoundError if missing."""
    path = _resolve_prompt_path(prompt_file)
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt file not found: {path}. "
            f"Create it or pass the correct path via kwargs."
        )
    return path.read_text(encoding="utf-8")


def _repair_json_escapes(text: str) -> str:
    """Walk JSON text character-by-character and double any backslash inside a string
    literal that does not form a valid JSON escape sequence.

    Valid sequences: \\" \\/ \\\\ \\b \\f \\n \\r \\t \\uXXXX (4 hex digits).
    Everything else (\\alpha, \\url, \\frac, \\upsilon …) gets doubled to \\\\.
    Skips content outside string literals so structural JSON characters are untouched.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != '"':
            out.append(ch)
            i += 1
            continue
        # Enter a JSON string literal
        out.append(ch)
        i += 1
        while i < n:
            ch = text[i]
            if ch == '"':
                # Closing quote — end of string
                out.append(ch)
                i += 1
                break
            if ch != '\\':
                out.append(ch)
                i += 1
                continue
            # Backslash: inspect the next character
            if i + 1 >= n:
                out.append(ch)
                i += 1
                break
            nxt = text[i + 1]
            if nxt in _VALID_SINGLE_ESCAPES:
                # Valid single-char escape — emit as-is
                out.append(ch)
                out.append(nxt)
                i += 2
            elif nxt == 'u':
                # \uXXXX — valid only if followed by exactly 4 hex digits
                hex4 = text[i + 2: i + 6]
                if len(hex4) == 4 and all(c in _HEX_CHARS for c in hex4):
                    out.append(ch)
                    out.append(nxt)
                    out.append(hex4)
                    i += 6
                else:
                    # e.g. \url, \underbrace — double the backslash
                    out.append('\\\\')
                    i += 1
            else:
                # Invalid escape (e.g. \a \c \e \p \f-as-in-\frac …) — double it
                out.append('\\\\')
                i += 1
    return ''.join(out)


def _parse_refinement_response(text: str) -> dict:
    """Parse the JSON response from a refinement step.

    Strips markdown code fences if the model wraps the JSON in them.
    Returns a fallback structure on parse failure to avoid losing work.
    """
    text = text.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # The model often embeds LaTeX/Markdown with bare backslashes (e.g. \alpha, \url)
        # inside the corrected_markdown JSON string, which is illegal.
        # Use a character-level scanner to repair only invalid escape sequences.
        repaired = _repair_json_escapes(text)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            pos = exc.pos or 0
            snippet = repaired[max(0, pos - 30): pos + 30].replace("\n", "↵")
            logger.warning("⚠️ Failed to parse JSON refinement response: %s | near: %r", exc, snippet)
        return {
            "iteration_summary": {
                "iteration": 0,
                "errors_found": -1,
                "content_errors": 0,
                "table_errors": 0,
                "structure_errors": 0,
                "noise_errors": 0,
                "critical": 0,
                "moderate": 0,
                "minor": 0,
                "verdict": "PARSE_ERROR",
            },
            "corrections": [],
            "corrected_markdown": text,
        }


def _build_cumulative_log_text(
    track_record: list[dict],
    all_corrections: list[dict],
) -> str:
    """Build a human-readable cumulative log to feed into the next refinement iteration."""
    lines: list[str] = []

    lines.append("### Cumulative Track Record\n")
    lines.append("| Iteration | Errors Found | Critical | Moderate | Minor | Verdict |")
    lines.append("|-----------|-------------|----------|----------|-------|---------|")
    for row in track_record:
        lines.append(
            f"| {row['iteration']} | {row['errors_found']} | "
            f"{row['critical']} | {row['moderate']} | "
            f"{row['minor']} | {row['verdict']} |"
        )
    lines.append("")

    # Include up to 20 most recent corrections to avoid overflowing context
    recent = all_corrections[-20:] if len(all_corrections) > 20 else all_corrections
    if recent:
        lines.append("### Recent Corrections\n")
        for j, c in enumerate(recent, 1):
            lines.append(f"**Error {j}**")
            lines.append(f"- Location: {c.get('location', 'N/A')}")
            lines.append(f"- Category: {c.get('category', 'N/A')}")
            lines.append(f"- Severity: {c.get('severity', 'N/A')}")
            lines.append(f'- PDF says: "{c.get("pdf_says", "N/A")}"')
            lines.append(f'- Markdown had: "{c.get("markdown_had", "N/A")}"')
            lines.append(f'- Corrected to: "{c.get("corrected_to", "N/A")}"')
            lines.append("")

    return "\n".join(lines)


class VertexAIBackend(BaseBackend):
    """Google Gemini via Vertex AI — cloud PDF extraction with optional iterative refinement.

    Requires ``google-genai`` (``pip install google-genai``).
    Authentication via ``gcloud auth application-default login`` or
    the ``GOOGLE_APPLICATION_CREDENTIALS`` environment variable.
    """

    name = "vertexai"

    @classmethod
    def is_available(cls) -> bool:
        try:
            from google import genai  # noqa: F401
            return True
        except ImportError:
            return False

    def supports_scanned(self) -> bool:
        return True

    def convert(self, pdf_path: Path, **kwargs: object) -> tuple[str, dict]:
        """Convert *pdf_path* to Markdown using Gemini on Vertex AI.

        Keyword arguments
        -----------------
        project_id : str
            Google Cloud project ID. Falls back to ``PROJECT_ID`` env var.
        location : str
            Vertex AI region, e.g. ``"europe-west3"``. Falls back to ``LOCATION`` env var.
        model_id : str
            Gemini model string, e.g. ``"gemini-2.5-pro"``. Falls back to ``MODEL_ID`` env var.
        extraction_prompt_file : str
            Path to the extraction prompt Markdown file.
        refinement_prompt_file : str
            Path to the refinement prompt Markdown file.
        refine_iterations : int
            Number of iterative refinement passes (0 = extraction only).
        """
        from google import genai
        from google.genai import types

        project_id: str = str(kwargs.get("project_id", os.getenv("PROJECT_ID", "")))
        location: str = str(kwargs.get("location", os.getenv("LOCATION", "europe-west3")))
        model_id: str = str(kwargs.get("model_id", os.getenv("MODEL_ID", "gemini-2.5-pro")))
        api_key: str = str(kwargs.get("api_key", os.getenv("GOOGLE_API_KEY", "")))
        refine_iterations: int = int(kwargs.get("refine_iterations", 0))  # type: ignore[arg-type]
        # Max errors to accept a CLEAN verdict for early stop.
        # -1 = stop on any CLEAN verdict (legacy); 0 = only stop if 0 errors; N = stop if errors <= N.
        _raw_cse = kwargs.get("clean_stop_max_errors", _load_config_clean_stop_max_errors())
        # Explicit 0 is a valid threshold — treat None as "no config" → default -1 (stop on any CLEAN)
        clean_stop_max_errors: int = int(_raw_cse) if _raw_cse is not None else -1  # type: ignore[arg-type]
        if refine_iterations > 0:
            _cse_label = "any CLEAN" if clean_stop_max_errors < 0 else f"errors ≤ {clean_stop_max_errors}"
            logger.info("ℹ️ Early-stop threshold: %s", _cse_label)
        extraction_prompt_file: str = str(
            kwargs.get("extraction_prompt_file", _DEFAULT_EXTRACTION_PROMPT)
        )
        refinement_prompt_file: str = str(
            kwargs.get("refinement_prompt_file", _DEFAULT_REFINEMENT_PROMPT)
        )

        if not project_id:
            raise ValueError(
                "Vertex AI project_id is required. "
                "Pass it via kwargs or set the PROJECT_ID environment variable."
            )

        logger.info("ℹ️ Vertex AI backend — project=%s, location=%s, model=%s", project_id, location, model_id)

        # Load prompts
        extraction_prompt = _load_prompt(extraction_prompt_file)
        logger.info("ℹ️ Extraction prompt: %d chars from %s", len(extraction_prompt), extraction_prompt_file)

        refinement_prompt = ""
        if refine_iterations > 0:
            refinement_prompt = _load_prompt(refinement_prompt_file)
            logger.info("ℹ️ Refinement prompt: %d chars from %s", len(refinement_prompt), refinement_prompt_file)

        # Create client.
        #
        # Vertex AI Express Mode: pass api_key WITHOUT project/location constructor params.
        # The SDK validates that (project or location) and api_key are mutually exclusive in
        # the constructor — but env vars are allowed. We set GOOGLE_CLOUD_PROJECT/LOCATION
        # env vars so the SDK has context, yet the explicit api_key wins (SDK clears them).
        #
        # ADC mode: pass project/location as constructor params; relies on
        # Application Default Credentials (gcloud auth application-default login).
        if api_key:
            logger.info("ℹ️ Authenticating via Vertex AI Express Mode (API key)")
            os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
            os.environ.setdefault("GOOGLE_CLOUD_LOCATION", location)
            client = genai.Client(
                vertexai=True,
                api_key=api_key,
                http_options=types.HttpOptions(api_version="v1beta1"),
            )
        else:
            logger.info("ℹ️ Authenticating via ADC — project=%s, location=%s", project_id, location)
            client = genai.Client(
                vertexai=True,
                project=project_id,
                location=location,
                http_options=types.HttpOptions(api_version="v1"),
            )

        # Load PDF bytes once
        pdf_part = types.Part.from_bytes(
            data=pdf_path.read_bytes(),
            mime_type="application/pdf",
        )

        # Accumulated token counts across all API calls
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0

        # ── Step 1: Initial extraction ──────────────────────────────────

        logger.info("ℹ️ Step 1: Initial extraction")
        start = time.time()

        try:
            response = client.models.generate_content(
                model=model_id,
                contents=[pdf_part, extraction_prompt],
                config=types.GenerateContentConfig(
                    temperature=_TEMPERATURE,
                    max_output_tokens=_MAX_OUTPUT_TOKENS,
                    response_mime_type="text/plain",
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"Vertex AI extraction call failed: {exc}") from exc

        latency = time.time() - start
        usage = response.usage_metadata
        step_in = getattr(usage, "prompt_token_count", 0) or 0
        step_out = getattr(usage, "candidates_token_count", 0) or 0
        step_total = getattr(usage, "total_token_count", 0) or (step_in + step_out)
        total_input_tokens += step_in
        total_output_tokens += step_out
        total_tokens += step_total

        logger.info(
            "ℹ️ Extraction done in %.1fs — %s input + %s output tokens",
            latency, f"{step_in:,}", f"{step_out:,}",
        )

        current_markdown: str = response.text or ""

        if refine_iterations == 0:
            metadata = {
                "backend": self.name,
                "model": model_id,
                "page_count": 0,
                "iterations_completed": 0,
                "final_verdict": "N/A",
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "total_tokens": total_tokens,
                "refinement_log": [],
            }
            return current_markdown, metadata

        # ── Steps 2..N: Iterative refinement ────────────────────────────

        track_record: list[dict] = []
        all_corrections: list[dict] = []
        # step_01 = raw extraction; subsequent entries = after each refinement pass
        iteration_markdowns: list[str] = [current_markdown]
        cumulative_log = ""
        final_verdict = "N/A"

        for i in range(1, refine_iterations + 1):
            logger.info("ℹ️ Step %d/%d: Refinement iteration %d", i + 1, refine_iterations + 1, i)

            user_message = (
                f"This is iteration {i}. "
                f'{"There is no prior correction log." if i == 1 else "The prior correction log is included below."}\n\n'
                "Please audit the current Markdown against the original PDF, correct any errors, "
                "and produce your response as a JSON object with exactly these keys:\n\n"
                '- "iteration_summary": an object with keys "iteration" (int), "errors_found" (int), '
                '"content_errors" (int), "table_errors" (int), "structure_errors" (int), '
                '"noise_errors" (int), "critical" (int), "moderate" (int), "minor" (int), '
                '"verdict" (string: "NEEDS ANOTHER PASS" or "CLEAN")\n'
                '- "corrections": a list of objects, each with keys "location" (str), "category" (str), '
                '"severity" (str), "pdf_says" (str), "markdown_had" (str), "corrected_to" (str), "risk" (str)\n'
                '- "corrected_markdown": the full corrected Markdown document as a single string\n\n'
                "Return ONLY the JSON object. No preamble, no markdown fences, no commentary outside the JSON.\n"
                "IMPORTANT: All backslashes inside JSON string values MUST be double-escaped. "
                r'For example, LaTeX \alpha must be written as \\alpha in the JSON string, '
                r'and \frac{a}{b} must be written as \\frac{a}{b}.' "\n\n"
                "---\n\n"
                "## Current Markdown to audit:\n\n"
                f"{current_markdown}\n\n"
                "---\n\n"
                "## Cumulative correction log from previous iterations:\n\n"
                f"{cumulative_log if cumulative_log else 'No previous iterations.'}"
            )

            start = time.time()
            try:
                ref_response = client.models.generate_content(
                    model=model_id,
                    contents=[pdf_part, refinement_prompt, user_message],
                    config=types.GenerateContentConfig(
                        temperature=_TEMPERATURE,
                        max_output_tokens=_MAX_OUTPUT_TOKENS,
                        response_mime_type="text/plain",
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "⚠️ Refinement iteration %d failed: %s — keeping current markdown", i, exc
                )
                break

            latency = time.time() - start
            usage = ref_response.usage_metadata
            step_in = getattr(usage, "prompt_token_count", 0) or 0
            step_out = getattr(usage, "candidates_token_count", 0) or 0
            step_total = getattr(usage, "total_token_count", 0) or (step_in + step_out)
            total_input_tokens += step_in
            total_output_tokens += step_out
            total_tokens += step_total

            parsed = _parse_refinement_response(ref_response.text or "")
            summary = parsed.get("iteration_summary", {})
            corrections = parsed.get("corrections", [])
            corrected_markdown = parsed.get("corrected_markdown", current_markdown)

            errors_found: int = int(summary.get("errors_found", -1))
            critical: int = int(summary.get("critical", 0))
            moderate: int = int(summary.get("moderate", 0))
            minor: int = int(summary.get("minor", 0))
            verdict: str = str(summary.get("verdict", "UNKNOWN"))
            final_verdict = verdict

            logger.info(
                "ℹ️ Refinement %d: %d errors (critical=%d, moderate=%d, minor=%d) — %s — %.1fs, %s+%s tokens",
                i, errors_found, critical, moderate, minor, verdict, latency,
                f"{step_in:,}", f"{step_out:,}",
            )

            all_corrections.extend(corrections)
            track_row = {
                "iteration": i,
                "errors_found": errors_found,
                "critical": critical,
                "moderate": moderate,
                "minor": minor,
                "verdict": verdict,
            }
            track_record.append(track_row)

            cumulative_log = _build_cumulative_log_text(track_record, all_corrections)
            current_markdown = corrected_markdown
            iteration_markdowns.append(current_markdown)

            if verdict == "CLEAN":
                # Honour the acceptance threshold: -1 = accept any CLEAN; >= 0 = accept only if
                # errors_found <= threshold.
                threshold_met = clean_stop_max_errors < 0 or errors_found <= clean_stop_max_errors
                if threshold_met:
                    logger.info(
                        "ℹ️ Document is CLEAN with %d error(s) (threshold=%s). Stopping early.",
                        errors_found,
                        "any" if clean_stop_max_errors < 0 else clean_stop_max_errors,
                    )
                    break
                else:
                    logger.info(
                        "ℹ️ CLEAN verdict but %d error(s) > threshold=%d — continuing.",
                        errors_found, clean_stop_max_errors,
                    )

            if i >= 2:
                prev_errors = track_record[-2]["errors_found"]
                curr_errors = track_record[-1]["errors_found"]
                if curr_errors >= prev_errors >= 0:
                    logger.info(
                        "ℹ️ No improvement (%d → %d errors). Stopping — diminishing returns.",
                        prev_errors, curr_errors,
                    )
                    break

        metadata: dict = {
            "backend": self.name,
            "model": model_id,
            "page_count": 0,
            "iterations_completed": len(track_record),
            "final_verdict": final_verdict,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "refinement_log": track_record,
            "all_corrections": all_corrections,
            "iteration_markdowns": iteration_markdowns,
        }

        logger.info(
            "ℹ️ Vertex AI complete — %d refinement(s), verdict=%s, total tokens=%s",
            len(track_record), final_verdict, f"{total_tokens:,}",
        )

        return current_markdown, metadata
