"""Main Pipeline orchestrator for PDF-to-Markdown conversion."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from src.backends import get_backend, get_best_available
from src.classifier import classify_pdf
from src.models import ConversionResult
from src.postprocess import postprocess
from src.validation import validate

logger = logging.getLogger("pipeline")


class Pipeline:
    """Orchestrates PDF classification, backend selection, extraction,
    post-processing, and optional validation.
    """

    def __init__(
        self,
        backend: str | None = None,
        postprocess_options: dict | None = None,
    ) -> None:
        self._backend_name = backend
        self._postprocess_options = postprocess_options or {}

        # Eagerly validate a forced backend name
        if backend is not None:
            known = ["marker", "pdfplumber", "vertexai"]
            if backend not in known:
                raise ValueError(f"Unknown backend '{backend}'. Choose from {known}")

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
            Non-PDF types are only supported with the Vertex AI backend.
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
            if self._backend_name not in (None, "vertexai"):
                raise ValueError(
                    f"File type '{pdf_path.suffix}' is only supported with the "
                    f"Vertex AI backend. Current backend: '{self._backend_name}'"
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

            # Select backend
            if self._backend_name:
                backend = get_backend(self._backend_name)
            else:
                backend = get_best_available(needs_ocr=pdf_info.is_scanned)

            if pdf_info.is_scanned and not backend.supports_scanned():
                logger.warning(
                    "Backend '%s' does not support scanned PDFs — results may be poor",
                    backend.name,
                )

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

    def convert_batch(
        self,
        input_path: str | Path,
        output_dir: str | Path | None = None,
        workers: int = 1,
        validate_output: bool = False,
        extensions: list[str] | None = None,
        **backend_kwargs: object,
    ) -> list[ConversionResult]:
        """Convert all matching files found under *input_path*.

        Parameters
        ----------
        input_path:
            Directory to search for files.
        output_dir:
            If given, save each result as ``.md`` in this directory.
        workers:
            Number of parallel worker processes.  ``1`` means sequential.
        validate_output:
            If True, validate every conversion.
        extensions:
            File extensions to match (lower-case, with dot), e.g. ``[".pdf", ".docx"]``.
            Defaults to ``[".pdf"]``.
        **backend_kwargs:
            Extra arguments forwarded to each backend call.
        """
        input_path = Path(input_path)
        exts = {e.lower() for e in (extensions or [".pdf"])}
        pattern = "**/*"
        pdfs = sorted(
            p for p in input_path.glob(pattern)
            if p.is_file() and p.suffix.lower() in exts
        )
        if not pdfs:
            logger.warning("No matching files found in %s", input_path)
            return []

        logger.info("Found %d file(s) in %s", len(pdfs), input_path)

        if workers <= 1:
            results = [
                self.convert(p, validate_output=validate_output, **backend_kwargs)
                for p in pdfs
            ]
        else:
            results = self._parallel_convert(
                pdfs, workers, validate_output, **backend_kwargs
            )

        # Save results
        if output_dir is not None:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            for r in results:
                md_name = r.source.stem + ".md"
                r.save(out / md_name)

        passed = sum(1 for r in results if r.validation and r.validation.passed)
        failed = sum(1 for r in results if r.validation and not r.validation.passed)
        no_val = sum(1 for r in results if r.validation is None)
        logger.info(
            "Batch complete: %d converted (%d passed, %d failed, %d not validated)",
            len(results), passed, failed, no_val,
        )

        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _parallel_convert(
        self,
        pdfs: list[Path],
        workers: int,
        validate_output: bool,
        **backend_kwargs: object,
    ) -> list[ConversionResult]:
        """Process PDFs in parallel using ProcessPoolExecutor."""
        results: list[ConversionResult] = []
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _worker_convert,
                    pdf,
                    self._backend_name,
                    self._postprocess_options,
                    validate_output,
                    backend_kwargs,
                ): pdf
                for pdf in pdfs
            }
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    pdf = futures[future]
                    logger.error("Failed to convert %s: %s", pdf, exc)
        return results


def _worker_convert(
    pdf_path: Path,
    backend_name: str | None,
    postprocess_options: dict,
    validate_output: bool,
    backend_kwargs: dict,
) -> ConversionResult:
    """Top-level function so ProcessPoolExecutor can pickle it."""
    pipeline = Pipeline(backend=backend_name, postprocess_options=postprocess_options)
    return pipeline.convert(pdf_path, validate_output=validate_output, **backend_kwargs)
