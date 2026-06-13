"""Hub Gemini backend — routes PDF extraction through the local-llm-hub.

This backend talks to the local LLM hub (``http://127.0.0.1:8000``) using the
Anthropic SDK shape, sending the source PDF as a ``document`` content block.
The hub forwards the request to its Gemini backend (the ``agy`` CLI) which
reads the PDF natively — no rasterization, no Vertex AI credentials.

It implements the same interface and returns the same metadata contract as
:class:`src.vertexai_backend.VertexAIBackend`, so the pipeline, batch
orchestrator, CLI, UI tabs, and execution logger work against it unchanged. The
shared extraction → refinement workflow lives in :mod:`src.refinement`; this
module supplies only the hub transport and delegates the loop to
:func:`src.refinement.run_conversion`.

Requires ``anthropic`` (``pip install anthropic``).

Notes
-----
* The hub's Gemini path (``agy``) does not surface token counts, so usage is
  reported as zero. Cost estimates therefore show ``$0.00`` for this backend.
* The hub does not enforce a JSON ``response_schema`` for refinement; the
  shared :func:`src.refinement._parse_refinement_response` already repairs
  free-form / truncated JSON, so refinement still works.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from src.refinement import (
    _DEFAULT_EXTRACTION_PROMPT,
    _DEFAULT_REFINEMENT_PROMPT,
    run_conversion,
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

        verbose_save_dir_str: str = str(kwargs.get("verbose_save_dir", ""))
        verbose_file_stem: str = str(kwargs.get("verbose_file_stem", ""))
        verbose_save_dir: Path | None = Path(verbose_save_dir_str) if verbose_save_dir_str else None

        logger.info("ℹ️ Hub Gemini backend — endpoint=%s, model=%s", _HUB_BASE_URL, model_id)
        logger.debug(
            "convert() called — pdf_path=%s, size=%d bytes, refine_iterations=%d, "
            "clean_stop_max_errors=%d, dry_run=%s",
            pdf_path, pdf_path.stat().st_size if pdf_path.exists() else 0,
            refine_iterations, clean_stop_max_errors, dry_run,
        )

        def _build_transports(extraction_prompt: str, refinement_prompt: str):
            """Build the hub client + reusable PDF block and return the transports."""
            client = self._build_client()
            pdf_block = self._pdf_block_for(pdf_path)

            def extract_fn() -> tuple[str, dict]:
                return self._call_hub(
                    client,
                    model_id,
                    content_blocks=[pdf_block, {"type": "text", "text": extraction_prompt}],
                )

            def refine_fn(user_message: str) -> tuple[str, dict]:
                return self._call_hub(
                    client,
                    model_id,
                    content_blocks=[pdf_block, {"type": "text", "text": user_message}],
                    system=refinement_prompt,
                )

            return extract_fn, refine_fn

        return run_conversion(
            backend_name=self.name,
            display_name="Hub Gemini",
            model_id=model_id,
            auth_mode="hub",
            model_phrase="via hub model=",
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
