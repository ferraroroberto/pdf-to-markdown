"""Main Pipeline orchestrator for PDF-to-Markdown conversion."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from src.hub_gemini_backend import HubGeminiBackend
from src.vertexai_backend import VertexAIBackend
from src.classifier import classify_pdf
from src.models import ConversionResult
from src.postprocess import postprocess
from src.validation import validate

logger = logging.getLogger("pipeline")

# Backend registry — name → backend class. Both expose the same
# ``convert()`` contract and metadata shape, so callers select by name.
_BACKENDS: dict[str, type] = {
    HubGeminiBackend.name: HubGeminiBackend,
    VertexAIBackend.name: VertexAIBackend,
}

# Default backend: route through the local LLM hub (issue #27). VertexAI
# remains available as an explicit fallback (``backend="vertexai"``).
DEFAULT_BACKEND = HubGeminiBackend.name


class Pipeline:
    """Orchestrates PDF classification, backend selection, extraction,
    post-processing, and optional validation.
    """

    def __init__(
        self,
        backend: str | None = None,
        postprocess_options: dict | None = None,
    ) -> None:
        self._backend_name = backend or DEFAULT_BACKEND
        self._postprocess_options = postprocess_options or {}

        if self._backend_name not in _BACKENDS:
            raise ValueError(
                f"Unknown backend '{self._backend_name}'. "
                f"Available: {sorted(_BACKENDS)}."
            )

    def convert(
        self,
        pdf_path: str | Path,
        validate_output: bool = False,
        **backend_kwargs: object,
    ) -> ConversionResult:
        """Convert a single PDF (or supported Office/image file) to Markdown.

        Parameters
        ----------
        pdf_path:
            Path to the input file (PDF, Word, PowerPoint, Excel, or image).
            Non-PDF types are pre-converted to PDF first (see
            ``file_converter.ensure_pdf``), so every backend handles them.
        validate_output:
            If True, run quality validation against the source PDF.
        **backend_kwargs:
            Extra keyword arguments forwarded to the backend's ``convert()``
            method (e.g. ``force_ocr``, ``page_range``).
        """
        from src.file_converter import SUPPORTED_EXTENSIONS, ensure_pdf, needs_conversion

        pdf_path = Path(pdf_path)
        logger.debug("Pipeline.convert() — file=%s, validate=%s", pdf_path, validate_output)
        pipeline_start = time.time()
        if not pdf_path.exists():
            raise FileNotFoundError(f"File not found: {pdf_path}")

        suffix = pdf_path.suffix.lower()
        if suffix != ".pdf":
            if suffix not in SUPPORTED_EXTENSIONS:
                raise ValueError(
                    f"Unsupported file type: {pdf_path.suffix!r}. "
                    f"Supported: .pdf + {sorted(SUPPORTED_EXTENSIONS)}"
                )

        original_source = pdf_path

        # Variables that must survive the with-block
        backend = None
        markdown = ""
        metadata: dict = {}
        validation_report = None

        with ensure_pdf(pdf_path) as work_pdf:
            # Classify
            logger.debug("Classifying PDF: %s", work_pdf)
            t0 = time.time()
            pdf_info = classify_pdf(work_pdf)
            logger.debug("Classification took %.3fs", time.time() - t0)
            logger.info(
                "Classified %s as %s (%d pages, %.0f avg chars/page)",
                original_source.name,
                pdf_info.classification,
                pdf_info.page_count,
                pdf_info.avg_chars_per_page,
            )

            # Select backend from the registry by resolved name
            backend = _BACKENDS[self._backend_name]()

            logger.info("Using backend: %s", backend.name)

            # Extract
            logger.debug("Starting extraction with backend=%s", backend.name)
            t0 = time.time()
            markdown, metadata = backend.convert(work_pdf, **backend_kwargs)
            logger.debug("Extraction took %.3fs, output=%d chars", time.time() - t0, len(markdown))
            if "page_count" not in metadata or metadata["page_count"] is None:
                metadata["page_count"] = pdf_info.page_count

            # Post-process
            logger.debug("Starting post-processing")
            t0 = time.time()
            markdown = postprocess(markdown, **self._postprocess_options)
            logger.debug("Post-processing took %.3fs, output=%d chars", time.time() - t0, len(markdown))

            # Validate
            if validate_output:
                validation_report = validate(work_pdf, markdown)
                logger.info(
                    "Validation: %s (similarity=%.1f%%)",
                    "PASS" if validation_report.passed else "FAIL",
                    validation_report.char_similarity * 100,
                )

        result = ConversionResult(
            source=original_source,
            markdown=markdown,
            backend_used=backend.name,
            metadata=metadata,
            validation=validation_report,
        )
        logger.debug(
            "Pipeline.convert() finished in %.3fs — backend=%s, chars=%d, tokens=~%d",
            time.time() - pipeline_start, backend.name, len(markdown), result.token_estimate,
        )
        return result
