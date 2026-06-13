"""Vertex AI (Google Gemini) backend — cloud-based PDF extraction with optional iterative refinement.

The extraction → refinement *workflow* lives in :mod:`src.refinement`; this module
supplies only the Vertex transport (``google-genai`` client + per-call config) and
delegates the stateful loop to :func:`src.refinement.run_conversion`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from src.auth import build_client
from src.config import DEFAULT_MODEL
from src.refinement import (
    _DEFAULT_EXTRACTION_PROMPT,
    _DEFAULT_REFINEMENT_PROMPT,
    run_conversion,
)
from src.refinement import _resolve_prompt_path  # noqa: F401  re-exported for tests

logger = logging.getLogger("vertexai_backend")

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


def _usage_dict(usage_metadata: object) -> dict:
    """Normalise Vertex ``usage_metadata`` into the transport usage mapping.

    ``total_token_count`` is preserved (it can exceed input + output, e.g. when
    thinking tokens are billed); :func:`src.refinement._usage_triplet` falls back
    to the sum when it is absent or zero.
    """
    step_in = getattr(usage_metadata, "prompt_token_count", 0) or 0
    step_out = getattr(usage_metadata, "candidates_token_count", 0) or 0
    step_total = getattr(usage_metadata, "total_token_count", 0) or 0
    return {"input_tokens": step_in, "output_tokens": step_out, "total_tokens": step_total}


class VertexAIBackend:
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
            Google Cloud project ID.
        location : str
            Vertex AI region, e.g. ``"europe-west3"``.
        model_id : str
            Gemini model string, e.g. ``"gemini-2.5-pro"``.
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
        diminishing_returns_enabled : bool
            If True (default), stop early when successive refinement passes show no error reduction.
            Set to False to always run all ``refine_iterations`` regardless of improvement.
        dry_run : bool
            If True, skip all API calls and return estimated token counts only.
        verbose_save_dir, verbose_file_stem :
            When both set, raw responses are written next to the output.
        """
        project_id: str = str(kwargs.get("project_id", ""))
        location: str = str(kwargs.get("location", "europe-west3"))
        model_id: str = str(kwargs.get("model_id", DEFAULT_MODEL))
        auth_mode: str = str(kwargs.get("auth_mode", "api"))
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

        verbose_save_dir_str: str = str(kwargs.get("verbose_save_dir", ""))
        verbose_file_stem: str = str(kwargs.get("verbose_file_stem", ""))
        verbose_save_dir: Path | None = Path(verbose_save_dir_str) if verbose_save_dir_str else None

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

        def _build_transports(extraction_prompt: str, refinement_prompt: str):
            """Build the Vertex client + per-call configs and return the transports."""
            from google.genai import types

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

            def extract_fn() -> tuple[str, dict]:
                resp = _call_with_retry(
                    client, model_id, contents=[pdf_part, extraction_prompt], config=gen_config
                )
                return resp.text or "", _usage_dict(resp.usage_metadata)

            def refine_fn(user_message: str) -> tuple[str, dict]:
                resp = _call_with_retry(
                    client, model_id, contents=[pdf_part, user_message], config=ref_gen_config
                )
                return resp.text or "", _usage_dict(resp.usage_metadata)

            return extract_fn, refine_fn

        return run_conversion(
            backend_name=self.name,
            display_name="Vertex AI",
            model_id=model_id,
            auth_mode=auth_mode,
            model_phrase="with model=",
            pdf_path=pdf_path,
            refine_iterations=refine_iterations,
            clean_stop_max_errors=clean_stop_max_errors,
            diminishing_returns_enabled=diminishing_returns_enabled,
            dry_run=dry_run,
            extraction_prompt_file=extraction_prompt_file,
            refinement_prompt_file=refinement_prompt_file,
            verbose_save_dir=verbose_save_dir,
            verbose_file_stem=verbose_file_stem,
            build_transports=_build_transports,
        )
