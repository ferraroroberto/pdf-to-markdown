"""Shared refinement-loop orchestration for the Gemini-based PDF backends.

Both :class:`src.vertexai_backend.VertexAIBackend` (cloud, via ``google-genai``)
and :class:`src.hub_gemini_backend.HubGeminiBackend` (local, via the LLM hub) run
the *same* extraction → iterative-refinement workflow and return the *same*
metadata contract. The only genuine difference between them is the **transport** —
how a single model call is issued and how its token usage is read back.

This module owns the transport-agnostic half of that workflow:

* the pure helpers shared by both backends — prompt loading/hashing, JSON repair
  and refinement-response parsing, raw-response persistence; and
* :func:`run_conversion`, the stateful orchestrator that drives the dry-run
  estimate, the step-0 extraction bookkeeping, the refinement loop (token
  accounting, per-pass track record, CLEAN early-stop, diminishing-returns stop,
  verbose ``raw_step_NN`` saving) and assembles the final metadata dict.

A backend supplies only a *transport factory*: given the loaded extraction and
refinement prompts, it returns two callables —

* ``extract() -> (text, usage)`` and
* ``refine(user_message) -> (text, usage)``

— where ``usage`` is a mapping carrying ``input_tokens`` / ``output_tokens`` (and
an optional ``total_tokens``, derived as the sum when absent).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

from src.logging_config import log_api_timing

logger = logging.getLogger("refinement")

# Default prompt locations relative to the project root.
_DEFAULT_EXTRACTION_PROMPT = "prompts/extraction.md"
_DEFAULT_REFINEMENT_PROMPT = "prompts/refinement.md"

# In JSON, \b \f \r are technically valid (backspace, form-feed, CR), but in
# LaTeX/Markdown documents they almost always signal \begin, \frac, \right etc.
# We deliberately exclude b, f, r so they get doubled like any other LaTeX command.
# We keep \n and \t because the model genuinely uses them for newlines/tabs in strings.
_VALID_SINGLE_ESCAPES: frozenset[str] = frozenset('"\\/nt')
_HEX_CHARS: frozenset[str] = frozenset('0123456789abcdefABCDEF')

# Regex to strip trailing commas before a closing bracket/brace (invalid JSON but
# produced by some LLMs, especially when a response is cut off mid-structure).
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")

# Transport callable shapes. A backend's factory returns these two closures.
ExtractFn = Callable[[], "tuple[str, dict]"]
RefineFn = Callable[[str], "tuple[str, dict]"]
TransportFactory = Callable[[str, str], "tuple[ExtractFn, RefineFn]"]


# ── Pure helpers ────────────────────────────────────────────────────────────


def _project_root() -> Path:
    """Return the project root (2 levels up from this file: src/refinement.py)."""
    return Path(__file__).parent.parent


def _resolve_prompt_path(prompt_file: str) -> Path:
    """Resolve a prompt file path, relative to project root if not absolute.

    Backslashes are normalised to forward slashes first so a path written on
    Windows (``prompts\\extraction_rag.md``) still resolves on POSIX, where a
    backslash is a literal filename character rather than a separator.
    """
    p = Path(prompt_file.replace("\\", "/"))
    if not p.is_absolute():
        p = _project_root() / p
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


def _remove_trailing_commas(text: str) -> str:
    """Strip trailing commas before `}` or `]` — invalid JSON but a common LLM output artifact."""
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def _repair_truncated_json(text: str) -> str | None:
    """Close a JSON document that was truncated mid-string or mid-structure.

    This handles responses where the LLM hit the token limit and the JSON output
    ends abruptly inside a string value or inside an unclosed array / object.

    Walks the text tracking nesting depth and open-string state, then appends
    the minimum closing characters needed to produce syntactically valid JSON.
    Returns the candidate repaired string, or ``None`` if the JSON is already
    balanced (i.e. no truncation was detected).
    """
    i = 0
    n = len(text)
    stack: list[str] = []  # unmatched '{' or '['
    in_string = False

    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                i += 2  # skip the escaped character
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
        i += 1

    if not in_string and not stack:
        return None  # already balanced

    closing = '"' if in_string else ""
    for opener in reversed(stack):
        closing += "}" if opener == "{" else "]"
    return text + closing


def _save_raw_response(pdf_path: Path, iteration: int, raw_text: str) -> None:
    """Persist a raw LLM response to the tmp/ directory for post-mortem debugging."""
    raw_dir = _project_root() / "tmp"
    raw_dir.mkdir(exist_ok=True)
    timestamp = int(time.time())
    filename = f"refinement_raw_{pdf_path.stem}_iter{iteration}_{timestamp}.txt"
    dest = raw_dir / filename
    dest.write_text(raw_text, encoding="utf-8")
    logger.warning("⚠️ Raw LLM response saved for debug: %s", dest)


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
            # Attempt 3: strip trailing commas (e.g. "value",} from truncated objects)
            no_trailing = _remove_trailing_commas(repaired)
            try:
                return json.loads(no_trailing)
            except json.JSONDecodeError:
                pass

            # Attempt 4: close open strings / structures (LLM hit token limit)
            closed = _repair_truncated_json(no_trailing)
            if closed is not None:
                try:
                    return json.loads(_remove_trailing_commas(closed))
                except json.JSONDecodeError:
                    pass

            # All repairs failed — log and fall through to the error placeholder
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


# ── Orchestration ───────────────────────────────────────────────────────────


def _usage_triplet(usage: dict) -> tuple[int, int, int]:
    """Normalise a transport's ``usage`` mapping to ``(input, output, total)``.

    ``total_tokens`` is honoured when the transport reports it (Vertex's
    ``total_token_count`` can exceed input + output); otherwise it is derived as
    the sum (the hub Gemini path reports only input/output, or zeros).
    """
    step_in = int(usage.get("input_tokens", 0) or 0)
    step_out = int(usage.get("output_tokens", 0) or 0)
    step_total = int(usage.get("total_tokens") or 0) or (step_in + step_out)
    return step_in, step_out, step_total


def _save_verbose(
    verbose_save_dir: Optional[Path], verbose_file_stem: str, step: int, raw_text: str
) -> None:
    """Write one raw step response next to the output (verbose mode only)."""
    if verbose_save_dir is None or not verbose_file_stem:
        return
    raw_path = verbose_save_dir / f"{verbose_file_stem}.raw_step_{step:02d}.txt"
    try:
        raw_path.write_text(raw_text, encoding="utf-8")
        logger.debug("Saved raw response step %d → %s", step, raw_path.name)
    except OSError as _e:
        logger.warning("⚠️ Could not save raw response: %s", _e)


def run_conversion(
    *,
    backend_name: str,
    display_name: str,
    model_id: str,
    auth_mode: str,
    model_phrase: str,
    pdf_path: Path,
    refine_iterations: int,
    clean_stop_max_errors: int,
    diminishing_returns_enabled: bool,
    dry_run: bool,
    extraction_prompt_file: str,
    refinement_prompt_file: str,
    verbose_save_dir: Optional[Path],
    verbose_file_stem: str,
    build_transports: TransportFactory,
) -> tuple[str, dict]:
    """Drive extraction + iterative refinement for one PDF, transport-agnostically.

    Parameters
    ----------
    backend_name, display_name :
        ``backend_name`` is recorded in the metadata (e.g. ``"vertexai"``);
        ``display_name`` is the human label used in summary log lines.
    model_id, auth_mode :
        Recorded in the returned metadata for the execution log.
    model_phrase :
        Connector used in the dry-run placeholder text, e.g. ``"with model="``
        (Vertex) or ``"via hub model="`` (hub Gemini).
    refine_iterations, clean_stop_max_errors, diminishing_returns_enabled :
        Refinement-loop controls (see the backend ``convert`` docstrings).
    dry_run :
        If True, estimate tokens and return without invoking ``build_transports``
        (so credentials / SDKs are never required on the dry-run path).
    extraction_prompt_file, refinement_prompt_file :
        Prompt file paths, resolved and loaded here.
    verbose_save_dir, verbose_file_stem :
        When both set, each raw step response is written next to the output.
    build_transports :
        Factory ``(extraction_prompt, refinement_prompt) -> (extract, refine)``.
        Called once, only when not a dry run.
    """
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
        _cse_label = "any CLEAN" if clean_stop_max_errors < 0 else f"errors ≤ {clean_stop_max_errors}"
        logger.info("ℹ️ Early-stop threshold: %s", _cse_label)

    # ── Dry-run: estimate and return without invoking the transport ─────────
    if dry_run:
        pdf_bytes = pdf_path.read_bytes()
        # Rough estimate: PDF bytes / 4 for token count
        est_tokens = len(pdf_bytes) // 4 + len(extraction_prompt) // 4
        logger.info(
            "ℹ️ [DRY RUN] Skipping model calls. Estimated extraction tokens: ~%d", est_tokens
        )
        return (
            f"[DRY RUN] Would process {pdf_path.name} "
            f"({len(pdf_bytes):,} bytes, ~{est_tokens:,} est. tokens) "
            f"{model_phrase}{model_id}, refine_iterations={refine_iterations}",
            {
                "backend": backend_name,
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

    extract_fn, refine_fn = build_transports(extraction_prompt, refinement_prompt)

    # Accumulated token counts across all model calls
    total_input_tokens = 0
    total_output_tokens = 0
    total_tokens = 0

    # ── Step 1: Initial extraction ──────────────────────────────────────────
    logger.info("ℹ️ Step 1: Initial extraction")
    start = time.time()
    text, usage = extract_fn()
    latency = time.time() - start

    step_in, step_out, step_total = _usage_triplet(usage)
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

    current_markdown: str = text or ""
    _save_verbose(verbose_save_dir, verbose_file_stem, 0, current_markdown)

    if refine_iterations == 0:
        metadata = {
            "backend": backend_name,
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
            "raw_responses": [{"step": 0, "step_type": "extraction", "raw_text": current_markdown}],
            "extraction_prompt_hash": extraction_prompt_hash,
            "refinement_prompt_hash": refinement_prompt_hash,
        }
        return current_markdown, metadata

    # ── Steps 2..N: Iterative refinement ────────────────────────────────────
    track_record: list[dict] = []
    all_corrections: list[dict] = []
    # step_01 = raw extraction; subsequent entries = after each refinement pass
    iteration_markdowns: list[str] = [current_markdown]
    raw_responses: list[dict] = [{"step": 0, "step_type": "extraction", "raw_text": current_markdown}]
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
            ref_text, ref_usage = refine_fn(user_message)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "⚠️ Refinement iteration %d failed: %s — keeping current markdown", i, exc
            )
            break

        latency = time.time() - start
        step_in, step_out, step_total = _usage_triplet(ref_usage)
        total_input_tokens += step_in
        total_output_tokens += step_out
        total_tokens += step_total

        # Save raw refinement response to disk immediately (verbose mode)
        raw_responses.append({"step": i, "step_type": "refinement", "raw_text": ref_text or ""})
        _save_verbose(verbose_save_dir, verbose_file_stem, i, ref_text or "")

        parsed = _parse_refinement_response(ref_text or "")
        summary = parsed.get("iteration_summary", {})
        if str(summary.get("verdict")) == "PARSE_ERROR":
            _save_raw_response(pdf_path, i, ref_text or "")
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
        track_record.append({
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
        })

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
            logger.info(
                "ℹ️ CLEAN verdict but %d error(s) > threshold=%d — continuing.",
                errors_found, clean_stop_max_errors,
            )

        if diminishing_returns_enabled and i >= 2:
            prev_errors = track_record[-2]["errors_found"]
            curr_errors = track_record[-1]["errors_found"]
            if curr_errors >= prev_errors >= 0:
                logger.info(
                    "ℹ️ No improvement (%d → %d errors). Stopping — diminishing returns.",
                    prev_errors, curr_errors,
                )
                break

    metadata = {
        "backend": backend_name,
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
        "ℹ️ %s complete — %d refinement(s), verdict=%s, total tokens=%s",
        display_name, len(track_record), final_verdict, f"{total_tokens:,}",
    )
    logger.debug(
        "%s summary — total_input=%s, total_output=%s, total=%s, "
        "corrections=%d, iterations_completed=%d",
        display_name,
        f"{total_input_tokens:,}", f"{total_output_tokens:,}", f"{total_tokens:,}",
        len(all_corrections), len(track_record),
    )

    return current_markdown, metadata
