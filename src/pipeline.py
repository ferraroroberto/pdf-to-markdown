"""Main Pipeline orchestrator for PDF-to-Markdown conversion."""

from __future__ import annotations

import logging
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
            known = ["marker", "docling", "pdfplumber", "vertexai"]
            if backend not in known:
                raise ValueError(f"Unknown backend '{backend}'. Choose from {known}")

    def convert(
        self,
        pdf_path: str | Path,
        validate_output: bool = False,
        **backend_kwargs: object,
    ) -> ConversionResult:
        """Convert a single PDF to Markdown.

        Parameters
        ----------
        pdf_path:
            Path to the PDF file.
        validate_output:
            If True, run quality validation against the source PDF.
        **backend_kwargs:
            Extra keyword arguments forwarded to the backend's ``convert()``
            method (e.g. ``force_ocr``, ``page_range``).
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        if pdf_path.suffix.lower() != ".pdf":
            raise ValueError(f"Not a PDF file: {pdf_path}")

        # Classify
        pdf_info = classify_pdf(pdf_path)
        logger.info(
            "Classified %s as %s (%d pages, %.0f avg chars/page)",
            pdf_path.name,
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
        markdown, metadata = backend.convert(pdf_path, **backend_kwargs)
        if "page_count" not in metadata or metadata["page_count"] is None:
            metadata["page_count"] = pdf_info.page_count

        # Post-process
        markdown = postprocess(markdown, **self._postprocess_options)

        # Validate
        validation_report = None
        if validate_output:
            validation_report = validate(pdf_path, markdown)
            logger.info(
                "Validation: %s (similarity=%.1f%%)",
                "PASS" if validation_report.passed else "FAIL",
                validation_report.char_similarity * 100,
            )

        return ConversionResult(
            source=pdf_path,
            markdown=markdown,
            backend_used=backend.name,
            metadata=metadata,
            validation=validation_report,
        )

    def convert_batch(
        self,
        input_path: str | Path,
        output_dir: str | Path | None = None,
        workers: int = 1,
        validate_output: bool = False,
        **backend_kwargs: object,
    ) -> list[ConversionResult]:
        """Convert all PDFs found under *input_path*.

        Parameters
        ----------
        input_path:
            Directory to search for ``**/*.pdf`` files.
        output_dir:
            If given, save each result as ``.md`` in this directory.
        workers:
            Number of parallel worker processes.  ``1`` means sequential.
        validate_output:
            If True, validate every conversion.
        **backend_kwargs:
            Extra arguments forwarded to each backend call.
        """
        input_path = Path(input_path)
        pdfs = sorted(input_path.glob("**/*.pdf"))
        if not pdfs:
            logger.warning("No PDF files found in %s", input_path)
            return []

        logger.info("Found %d PDF(s) in %s", len(pdfs), input_path)

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
