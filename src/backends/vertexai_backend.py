"""Vertex AI (Google Gemini) backend — cloud-based PDF extraction with optional iterative refinement."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from src.auth import build_client
from src.backends.base import BaseBackend
from src.logging_config import log_api_timing

logger = logging.getLogger("backends.vertexai")

# Default prompt locations relative to the project root
_DEFAULT_EXTRACTION_PROMPT = "prompts/extraction.md"
_DEFAULT_REFINEMENT_PROMPT = "prompts/refinement.md"

# Gemini model parameters
_TEMPERATURE = 0.2
_MAX_OUTPUT_TOKENS = 65536

# Retry configuration (optional improvement: exponential backoff)
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0  # seconds

# JSON Schema that constrains the refinement response structure.
# Passed as response_schema so the model cannot invent its own format.
_REFINEMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "iteration_summary": {
            "type": "object",
            "properties": {
                "iteration":       {"type": "integer"},
                "errors_found":    {"type": "integer"},
                "content_errors":  {"type": "integer"},
                "table_errors":    {"type": "integer"},
                "structure_errors":{"type": "integer"},
                "noise_errors":    {"type": "integer"},
                "critical":        {"type": "integer"},
                "moderate":        {"type": "integer"},
                "minor":           {"type": "integer"},
                "verdict":         {"type": "string", "enum": ["NEEDS ANOTHER PASS", "CLEAN"]},
            },
            "required": ["iteration", "errors_found", "critical", "moderate", "minor", "verdict"],
        },
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "location":     {"type": "string"},
                    "category":     {"type": "string"},
                    "severity":     {"type": "string"},
                    "pdf_says":     {"type": "string"},
                    "markdown_had": {"type": "string"},
                    "corrected_to": {"type": "string"},
                    "risk":         {"type": "string"},
                },
            },
        },
        "corrected_markdown": {"type": "string"},
    },
    "required": ["iteration_summary", "corrections", "corrected_markdown"],
}

# In JSON, \b \f \r are technically valid (backspace, form-feed, CR), but in
# LaTeX/Markdown documents they almost always signal \begin, \frac, \right etc.
# We deliberately exclude b, f, r so they get doubled like any other LaTeX command.
# We keep \n and \t because the model genuinely uses them for newlines/tabs in strings.
_VALID_SINGLE_ESCAPES: frozenset[str] = frozenset('"\\/nt')
_HEX_CHARS: frozenset[str] = frozenset('0123456789abcdefABCDEF')


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


def _prompt_hash(text: str) -> str:
    """Return a short SHA-256 hex digest for prompt versioning."""
    return hashlib.sha256(text.encode()).hexdigest()[:8]


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
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise json.JSONDecodeError("Expected a JSON object, got a list or scalar", text, 0)
    except json.JSONDecodeError:
        repaired = _repair_json_escapes(text)
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed
            raise json.JSONDecodeError("Expected a JSON object, got a list or scalar", repaired, 0)
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


def _call_with_retry(client: object, model_id: str, contents: list, config: object) -> object:
    """Call ``client.models.generate_content`` with exponential-backoff retry.

    Retries up to ``_RETRY_MAX_ATTEMPTS`` times on transient errors (network,
    rate-limit, 5xx).  Raises the last exception if all attempts fail.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        logger.debug("API call attempt %d/%d — model=%s", attempt, _RETRY_MAX_ATTEMPTS, model_id)
        try:
            t0 = time.time()
            result = client.models.generate_content(  # type: ignore[attr-defined]
                model=model_id,
                contents=contents,
                config=config,
            )
            logger.debug("API call succeeded in %.2fs (attempt %d)", time.time() - t0, attempt)
            return result
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.debug("API call exception (attempt %d): %s: %s", attempt, type(exc).__name__, exc)
            if attempt < _RETRY_MAX_ATTEMPTS:
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "⚠️ API call failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt, _RETRY_MAX_ATTEMPTS, exc, delay,
                )
                time.sleep(delay)
            else:
                logger.error("❌ API call failed after %d attempts: %s", _RETRY_MAX_ATTEMPTS, exc)
    raise RuntimeError(f"Vertex AI call failed after {_RETRY_MAX_ATTEMPTS} attempts") from last_exc


class VertexAIBackend(BaseBackend):
    """Google Gemini via Vertex AI — cloud PDF extraction with optional iterative refinement.

    Requires ``google-genai`` (``pip install google-genai``).
    Authentication is handled by ``src.auth.build_client`` (api or gcloud mode).
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
        auth_mode : str
            ``"api"`` (default) or ``"gcloud"``.
        extraction_prompt_file : str
            Path to the extraction prompt Markdown file.
        refinement_prompt_file : str
            Path to the refinement prompt Markdown file.
        refine_iterations : int
            Number of iterative refinement passes (0 = extraction only).
        clean_stop_max_errors : int
            Early-stop threshold when verdict is CLEAN.  -1 = any CLEAN stops; 0 = only 0 errors.
        dry_run : bool
            If True, skip all API calls and return estimated token counts only.
        """
        from google.genai import types

        project_id: str = str(kwargs.get("project_id", os.getenv("PROJECT_ID", "")))
        location: str = str(kwargs.get("location", os.getenv("LOCATION", "europe-west3")))
        model_id: str = str(kwargs.get("model_id", os.getenv("MODEL_ID", "gemini-2.5-pro")))
        auth_mode: str = str(kwargs.get("auth_mode", "api"))
        refine_iterations: int = int(kwargs.get("refine_iterations", 0))  # type: ignore[arg-type]
        clean_stop_max_errors: int = int(kwargs.get("clean_stop_max_errors", 0))  # type: ignore[arg-type]
        dry_run: bool = bool(kwargs.get("dry_run", False))

        extraction_prompt_file: str = str(
            kwargs.get("extraction_prompt_file", _DEFAULT_EXTRACTION_PROMPT)
        )
        refinement_prompt_file: str = str(
            kwargs.get("refinement_prompt_file", _DEFAULT_REFINEMENT_PROMPT)
        )

        logger.info(
            "ℹ️ Vertex AI backend — project=%s, location=%s, model=%s, auth=%s",
            project_id, location, model_id, auth_mode,
        )
        logger.debug(
            "convert() called — pdf_path=%s, size=%d bytes, refine_iterations=%d, "
            "clean_stop_max_errors=%d, dry_run=%s",
            pdf_path, pdf_path.stat().st_size if pdf_path.exists() else 0,
            refine_iterations, clean_stop_max_errors, dry_run,
        )

        # Load prompts
        extraction_prompt = _load_prompt(extraction_prompt_file)
        extraction_prompt_hash = _prompt_hash(extraction_prompt)
        logger.info(
            "ℹ️ Extraction prompt: %d chars from %s (hash=%s)",
            len(extraction_prompt), extraction_prompt_file, extraction_prompt_hash,
        )

        refinement_prompt = ""
        refinement_prompt_hash = ""
        if refine_iterations > 0:
            refinement_prompt = _load_prompt(refinement_prompt_file)
            refinement_prompt_hash = _prompt_hash(refinement_prompt)
            logger.info(
                "ℹ️ Refinement prompt: %d chars from %s (hash=%s)",
                len(refinement_prompt), refinement_prompt_file, refinement_prompt_hash,
            )

        if refine_iterations > 0:
            _cse_label = "any CLEAN" if clean_stop_max_errors < 0 else f"errors ≤ {clean_stop_max_errors}"
            logger.info("ℹ️ Early-stop threshold: %s", _cse_label)

        # ── Dry-run: estimate and return without API calls ─────────────────
        if dry_run:
            pdf_bytes = pdf_path.read_bytes()
            # Rough estimate: PDF bytes / 4 for token count
            est_tokens = len(pdf_bytes) // 4 + len(extraction_prompt) // 4
            logger.info(
                "ℹ️ [DRY RUN] Skipping API calls. Estimated extraction tokens: ~%d", est_tokens
            )
            return (
                f"[DRY RUN] Would process {pdf_path.name} "
                f"({len(pdf_bytes):,} bytes, ~{est_tokens:,} est. tokens) "
                f"with model={model_id}, refine_iterations={refine_iterations}",
                {
                    "backend": self.name,
                    "model": model_id,
                    "auth_mode": auth_mode,
                    "page_count": 0,
                    "dry_run": True,
                    "estimated_tokens": est_tokens,
                    "iterations_completed": 0,
                    "final_verdict": "DRY_RUN",
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_tokens": 0,
                    "refinement_log": [],
                    "extraction_prompt_hash": extraction_prompt_hash,
                    "refinement_prompt_hash": refinement_prompt_hash,
                },
            )

        # Build authenticated client (raises ConfigError on misconfiguration)
        logger.debug("Building authenticated client (auth_mode=%s)", auth_mode)
        client = build_client(auth_mode=auth_mode, project_id=project_id, location=location)
        logger.debug("Client built successfully")

        gen_config = types.GenerateContentConfig(
            temperature=_TEMPERATURE,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
            response_mime_type="text/plain",
        )

        ref_gen_config = types.GenerateContentConfig(
            temperature=_TEMPERATURE,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            response_schema=_REFINEMENT_SCHEMA,
            system_instruction=refinement_prompt,
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

        # Optional: save raw responses to disk for debugging (verbose mode)
        verbose_save_dir_str: str = str(kwargs.get("verbose_save_dir", ""))
        verbose_file_stem: str = str(kwargs.get("verbose_file_stem", ""))
        verbose_save_dir: Path | None = Path(verbose_save_dir_str) if verbose_save_dir_str else None

        # ── Step 1: Initial extraction ──────────────────────────────────────

        logger.info("ℹ️ Step 1: Initial extraction")
        start = time.time()

        response = _call_with_retry(
            client, model_id,
            contents=[pdf_part, extraction_prompt],
            config=gen_config,
        )

        latency = time.time() - start
        usage = response.usage_metadata
        step_in = getattr(usage, "prompt_token_count", 0) or 0
        step_out = getattr(usage, "candidates_token_count", 0) or 0
        step_total = getattr(usage, "total_token_count", 0) or (step_in + step_out)
        total_input_tokens += step_in
        total_output_tokens += step_out
        total_tokens += step_total

        # Per-step record for the extraction call (step 0)
        extraction_step = {
            "step": 0,
            "step_type": "extraction",
            "step_input_tokens": step_in,
            "step_output_tokens": step_out,
            "step_total_tokens": step_total,
            "latency_s": round(latency, 2),
        }

        log_api_timing(
            logger,
            step_label="Extraction",
            latency_s=latency,
            input_tokens=step_in,
            output_tokens=step_out,
            model=model_id,
            extra={"pdf": pdf_path.name, "prompt_hash": extraction_prompt_hash},
        )

        current_markdown: str = response.text or ""

        # Save raw extraction response to disk immediately (verbose mode)
        if verbose_save_dir is not None and verbose_file_stem:
            raw_path = verbose_save_dir / f"{verbose_file_stem}.raw_step_00.txt"
            try:
                raw_path.write_text(response.text or "", encoding="utf-8")
                logger.debug("Saved raw extraction response → %s", raw_path.name)
            except OSError as _e:
                logger.warning("⚠️ Could not save raw response: %s", _e)

        if refine_iterations == 0:
            metadata = {
                "backend": self.name,
                "model": model_id,
                "auth_mode": auth_mode,
                "page_count": 0,
                "iterations_completed": 0,
                "final_verdict": "N/A",
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "total_tokens": total_tokens,
                "extraction_step": extraction_step,
                "refinement_log": [],
                "raw_responses": [{"step": 0, "step_type": "extraction", "raw_text": response.text or ""}],
                "extraction_prompt_hash": extraction_prompt_hash,
                "refinement_prompt_hash": refinement_prompt_hash,
            }
            return current_markdown, metadata

        # ── Steps 2..N: Iterative refinement ────────────────────────────────

        track_record: list[dict] = []
        all_corrections: list[dict] = []
        # step_01 = raw extraction; subsequent entries = after each refinement pass
        iteration_markdowns: list[str] = [current_markdown]
        raw_responses: list[dict] = [{"step": 0, "step_type": "extraction", "raw_text": response.text or ""}]
        final_verdict = "N/A"

        for i in range(1, refine_iterations + 1):
            logger.info("ℹ️ Step %d/%d: Refinement iteration %d", i + 1, refine_iterations + 1, i)

            user_message = (
                "Audit the current Markdown against the original PDF and return your response "
                "as specified in your instructions.\n\n"
                "---\n\n"
                "## Current Markdown to audit:\n\n"
                f"{current_markdown}"
            )

            start = time.time()
            try:
                ref_response = _call_with_retry(
                    client, model_id,
                    contents=[pdf_part, user_message],
                    config=ref_gen_config,
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

            # Save raw refinement response to disk immediately (verbose mode)
            raw_responses.append({"step": i, "step_type": "refinement", "raw_text": ref_response.text or ""})
            if verbose_save_dir is not None and verbose_file_stem:
                raw_path = verbose_save_dir / f"{verbose_file_stem}.raw_step_{i:02d}.txt"
                try:
                    raw_path.write_text(ref_response.text or "", encoding="utf-8")
                    logger.debug("Saved raw refinement response %d → %s", i, raw_path.name)
                except OSError as _e:
                    logger.warning("⚠️ Could not save raw response: %s", _e)

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

            log_api_timing(
                logger,
                step_label=f"Refinement pass {i}",
                latency_s=latency,
                input_tokens=step_in,
                output_tokens=step_out,
                model=model_id,
                extra={
                    "pdf": pdf_path.name,
                    "errors_found": errors_found,
                    "critical": critical,
                    "moderate": moderate,
                    "minor": minor,
                    "verdict": verdict,
                    "prompt_hash": refinement_prompt_hash,
                },
            )
            logger.info(
                "ℹ️ Refinement %d: %d errors (critical=%d, moderate=%d, minor=%d) — %s",
                i, errors_found, critical, moderate, minor, verdict,
            )

            for _c in corrections:
                _c["iteration"] = i
            all_corrections.extend(corrections)
            track_row = {
                "step": i,  # 1-indexed; step 0 is the extraction call
                "step_type": "refinement",
                "iteration": i,
                "errors_found": errors_found,
                "critical": critical,
                "moderate": moderate,
                "minor": minor,
                "verdict": verdict,
                "step_input_tokens": step_in,
                "step_output_tokens": step_out,
                "step_total_tokens": step_total,
                "latency_s": round(latency, 2),
            }
            track_record.append(track_row)

            current_markdown = corrected_markdown
            iteration_markdowns.append(current_markdown)

            if verdict == "CLEAN":
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
            "auth_mode": auth_mode,
            "page_count": 0,
            "iterations_completed": len(track_record),
            "final_verdict": final_verdict,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "extraction_step": extraction_step,
            "refinement_log": track_record,
            "all_corrections": all_corrections,
            "iteration_markdowns": iteration_markdowns,
            "raw_responses": raw_responses,
            "extraction_prompt_hash": extraction_prompt_hash,
            "refinement_prompt_hash": refinement_prompt_hash,
        }

        logger.info(
            "ℹ️ Vertex AI complete — %d refinement(s), verdict=%s, total tokens=%s",
            len(track_record), final_verdict, f"{total_tokens:,}",
        )
        logger.debug(
            "Vertex AI summary — total_input=%s, total_output=%s, total=%s, "
            "corrections=%d, iterations_completed=%d",
            f"{total_input_tokens:,}", f"{total_output_tokens:,}", f"{total_tokens:,}",
            len(all_corrections), len(track_record),
        )

        return current_markdown, metadata
