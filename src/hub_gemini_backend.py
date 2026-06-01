"""Hub Gemini backend — routes PDF extraction through the local-llm-hub.

This backend talks to the local LLM hub (``http://127.0.0.1:8000``) using the
Anthropic SDK shape, sending the source PDF as a ``document`` content block.
The hub forwards the request to its Gemini backend (the ``agy`` CLI) which
reads the PDF natively — no rasterization, no Vertex AI credentials.

It implements the same interface and returns the same metadata contract as
:class:`src.vertexai_backend.VertexAIBackend`, so the pipeline, batch
orchestrator, CLI, UI tabs, and execution logger work against it unchanged.

Requires ``anthropic`` (``pip install anthropic``).

Notes
-----
* The hub's Gemini path (``agy``) does not surface token counts, so usage is
  reported as zero. Cost estimates therefore show ``$0.00`` for this backend.
* The hub does not enforce a JSON ``response_schema`` for refinement; the
  shared :func:`src.vertexai_backend._parse_refinement_response` already
  repairs free-form / truncated JSON, so refinement still works.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

from src.logging_config import log_api_timing
from src.vertexai_backend import (
    _DEFAULT_EXTRACTION_PROMPT,
    _DEFAULT_REFINEMENT_PROMPT,
    _load_prompt,
    _parse_refinement_response,
    _prompt_hash,
    _save_raw_response,
)

logger = logging.getLogger("hub_gemini_backend")

# Local LLM hub endpoint (Anthropic-shape). Loopback only — the hub runs on
# the same machine and is mutex-guarded against external kill.
_HUB_BASE_URL = "http://127.0.0.1:8000"
_HUB_API_KEY = "local-dummy"  # hub ignores the key for loopback callers

# Stable downstream model alias. NEVER use the display name — the hub repoints
# the display_name under this alias when a newer Gemini ships.
_DEFAULT_MODEL = "gemini_pro"

# Generous ceiling so long documents are not truncated. The hub forwards this
# as the max output token hint.
_MAX_OUTPUT_TOKENS = 65536

# Explicit per-request timeout (seconds). Required: with a large ``max_tokens``
# the Anthropic SDK otherwise refuses non-streaming requests, raising
# "Streaming is required for operations that may take longer than 10 minutes".
# The hub does not stream, so we set a generous explicit timeout instead. A
# local Gemini extraction of a large PDF can legitimately run several minutes.
_REQUEST_TIMEOUT_S = 1800.0


def _pdf_document_block(pdf_bytes: bytes) -> dict:
    """Build an Anthropic ``document`` content block carrying *pdf_bytes*.

    Matches the shape the hub extracts in ``_extract_media_blocks``:
    ``{"type": "document", "source": {"type": "base64",
    "media_type": "application/pdf", "data": "<b64>"}}``.
    """
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(pdf_bytes).decode("ascii"),
        },
    }


class HubGeminiBackend:
    """Gemini via the local LLM hub — PDF extraction with optional refinement.

    Same ``convert()`` contract as :class:`VertexAIBackend`. Authentication is
    handled entirely by the hub; this backend only needs the ``anthropic`` SDK
    to reach the loopback endpoint.
    """

    name = "hubgemini"

    @classmethod
    def is_available(cls) -> bool:
        try:
            import anthropic  # noqa: F401

            return True
        except ImportError:
            return False

    def supports_scanned(self) -> bool:
        return True

    def convert(self, pdf_path: Path, **kwargs: object) -> tuple[str, dict]:
        """Convert *pdf_path* to Markdown via the hub's Gemini backend.

        Keyword arguments
        -----------------
        model_id : str
            Hub model alias, e.g. ``"gemini_pro"`` (default). Display names are
            rejected by the hub registry — always pass a stable alias.
        extraction_prompt_file : str
            Path to the extraction prompt Markdown file.
        refinement_prompt_file : str
            Path to the refinement prompt Markdown file.
        refine_iterations : int
            Number of iterative refinement passes (0 = extraction only).
        clean_stop_max_errors : int
            Early-stop threshold when verdict is CLEAN. -1 = any CLEAN stops.
        diminishing_returns_enabled : bool
            If True (default), stop early when passes show no error reduction.
        dry_run : bool
            If True, skip all hub calls and return estimated token counts only.
        verbose_save_dir, verbose_file_stem :
            When both set, raw responses are written next to the output.

        Other kwargs accepted for interface parity with VertexAIBackend
        (``project_id``, ``location``, ``auth_mode``) are ignored — the hub
        owns routing and credentials.
        """
        model_id: str = str(kwargs.get("model_id", _DEFAULT_MODEL)) or _DEFAULT_MODEL
        refine_iterations: int = int(kwargs.get("refine_iterations", 0))  # type: ignore[arg-type]
        clean_stop_max_errors: int = int(kwargs.get("clean_stop_max_errors", 0))  # type: ignore[arg-type]
        diminishing_returns_enabled: bool = bool(kwargs.get("diminishing_returns_enabled", True))
        dry_run: bool = bool(kwargs.get("dry_run", False))

        extraction_prompt_file: str = str(
            kwargs.get("extraction_prompt_file", _DEFAULT_EXTRACTION_PROMPT)
        )
        refinement_prompt_file: str = str(
            kwargs.get("refinement_prompt_file", _DEFAULT_REFINEMENT_PROMPT)
        )

        logger.info("ℹ️ Hub Gemini backend — endpoint=%s, model=%s", _HUB_BASE_URL, model_id)
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
            _cse_label = "any CLEAN" if clean_stop_max_errors < 0 else f"errors ≤ {clean_stop_max_errors}"
            logger.info("ℹ️ Early-stop threshold: %s", _cse_label)

        # ── Dry-run: estimate and return without hub calls ─────────────────
        if dry_run:
            pdf_bytes = pdf_path.read_bytes()
            est_tokens = len(pdf_bytes) // 4 + len(extraction_prompt) // 4
            logger.info(
                "ℹ️ [DRY RUN] Skipping hub calls. Estimated extraction tokens: ~%d", est_tokens
            )
            return (
                f"[DRY RUN] Would process {pdf_path.name} "
                f"({len(pdf_bytes):,} bytes, ~{est_tokens:,} est. tokens) "
                f"via hub model={model_id}, refine_iterations={refine_iterations}",
                {
                    "backend": self.name,
                    "model": model_id,
                    "auth_mode": "hub",
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

        client = self._build_client()

        pdf_block = self._pdf_block_for(pdf_path)

        # Accumulated token counts (hub Gemini path reports zeros; kept for
        # contract parity with the Vertex backend).
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0

        verbose_save_dir_str: str = str(kwargs.get("verbose_save_dir", ""))
        verbose_file_stem: str = str(kwargs.get("verbose_file_stem", ""))
        verbose_save_dir: Path | None = Path(verbose_save_dir_str) if verbose_save_dir_str else None

        # ── Step 1: Initial extraction ──────────────────────────────────────
        logger.info("ℹ️ Step 1: Initial extraction")
        start = time.time()
        text, usage = self._call_hub(
            client,
            model_id,
            content_blocks=[pdf_block, {"type": "text", "text": extraction_prompt}],
        )
        latency = time.time() - start

        step_in = int(usage.get("input_tokens", 0) or 0)
        step_out = int(usage.get("output_tokens", 0) or 0)
        step_total = step_in + step_out
        total_input_tokens += step_in
        total_output_tokens += step_out
        total_tokens += step_total

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

        if verbose_save_dir is not None and verbose_file_stem:
            raw_path = verbose_save_dir / f"{verbose_file_stem}.raw_step_00.txt"
            try:
                raw_path.write_text(current_markdown, encoding="utf-8")
                logger.debug("Saved raw extraction response → %s", raw_path.name)
            except OSError as _e:
                logger.warning("⚠️ Could not save raw response: %s", _e)

        if refine_iterations == 0:
            metadata = {
                "backend": self.name,
                "model": model_id,
                "auth_mode": "hub",
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

        # ── Steps 2..N: Iterative refinement ────────────────────────────────
        track_record: list[dict] = []
        all_corrections: list[dict] = []
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
                ref_text, ref_usage = self._call_hub(
                    client,
                    model_id,
                    content_blocks=[pdf_block, {"type": "text", "text": user_message}],
                    system=refinement_prompt,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "⚠️ Refinement iteration %d failed: %s — keeping current markdown", i, exc
                )
                break

            latency = time.time() - start
            step_in = int(ref_usage.get("input_tokens", 0) or 0)
            step_out = int(ref_usage.get("output_tokens", 0) or 0)
            step_total = step_in + step_out
            total_input_tokens += step_in
            total_output_tokens += step_out
            total_tokens += step_total

            raw_responses.append({"step": i, "step_type": "refinement", "raw_text": ref_text or ""})
            if verbose_save_dir is not None and verbose_file_stem:
                raw_path = verbose_save_dir / f"{verbose_file_stem}.raw_step_{i:02d}.txt"
                try:
                    raw_path.write_text(ref_text or "", encoding="utf-8")
                    logger.debug("Saved raw refinement response %d → %s", i, raw_path.name)
                except OSError as _e:
                    logger.warning("⚠️ Could not save raw response: %s", _e)

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
                "step": i,
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
            "backend": self.name,
            "model": model_id,
            "auth_mode": "hub",
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
            "ℹ️ Hub Gemini complete — %d refinement(s), verdict=%s, total tokens=%s",
            len(track_record), final_verdict, f"{total_tokens:,}",
        )
        return current_markdown, metadata

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_client(self) -> object:
        """Construct an Anthropic SDK client pointed at the local hub."""
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ImportError(
                "anthropic is not installed. Run: pip install anthropic"
            ) from exc
        logger.debug("Building Anthropic client → %s", _HUB_BASE_URL)
        return Anthropic(
            api_key=_HUB_API_KEY,
            base_url=_HUB_BASE_URL,
            timeout=_REQUEST_TIMEOUT_S,
            max_retries=0,
        )

    def _pdf_block_for(self, pdf_path: Path) -> dict:
        """Read *pdf_path* once and build its reusable document content block."""
        return _pdf_document_block(pdf_path.read_bytes())

    def _call_hub(
        self,
        client: object,
        model_id: str,
        content_blocks: list[dict],
        system: str | None = None,
    ) -> tuple[str, dict]:
        """Send one message to the hub and return ``(text, usage_dict)``.

        Raises ``RuntimeError`` on any SDK / transport error so the caller's
        ``except`` paths (refinement break, pipeline error handling) trigger.
        """
        kwargs: dict = {
            "model": model_id,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": content_blocks}],
        }
        if system:
            kwargs["system"] = system

        try:
            response = client.messages.create(**kwargs)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ Hub call failed: %s: %s", type(exc).__name__, exc)
            raise RuntimeError(f"Hub Gemini call failed: {exc}") from exc

        text_parts: list[str] = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", "") or "")
        text = "".join(text_parts)

        usage_obj = getattr(response, "usage", None)
        usage = {
            "input_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
        }
        return text, usage
