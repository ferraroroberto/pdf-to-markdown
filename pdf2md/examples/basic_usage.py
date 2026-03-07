"""Example usage patterns for the pdf2md library.

These functions are documented but not auto-executed.
Run them individually from your own scripts or a REPL.
"""

from __future__ import annotations

from pathlib import Path


def example_basic() -> None:
    """Convert a single PDF and print basic stats."""
    from pdf2md import Pipeline

    pipe = Pipeline()
    result = pipe.convert("document.pdf")

    print(f"Backend : {result.backend_used}")
    print(f"Pages   : {result.page_count}")
    print(f"Chars   : {len(result.markdown):,}")
    print(f"Tokens  : ~{result.token_estimate:,}")
    print()
    print(result.markdown[:500])

    result.save("output/document.md")


def example_with_validation() -> None:
    """Convert a PDF with quality validation enabled."""
    from pdf2md import Pipeline

    pipe = Pipeline()
    result = pipe.convert("document.pdf", validate_output=True)

    print(result.markdown[:500])
    print()

    if result.validation:
        print(result.validation.summary())
        if not result.validation.passed:
            print("\nWARNING: Conversion quality below threshold!")


def example_specific_backend() -> None:
    """List available backends and use a specific one."""
    from pdf2md import Pipeline, list_available

    print("Available backends:", list_available())

    pipe = Pipeline(backend="marker")
    result = pipe.convert("document.pdf", force_ocr=True)
    print(f"Converted with {result.backend_used}")
    print(result.markdown[:500])


def example_classify_first() -> None:
    """Classify a PDF and choose a backend based on its type."""
    from pdf2md import Pipeline, classify_pdf

    info = classify_pdf("document.pdf")
    print(f"Classification: {info.classification}")
    print(f"Pages: {info.page_count}")
    print(f"Avg chars/page: {info.avg_chars_per_page:.0f}")
    print(f"Has images: {info.has_images}")

    if info.is_scanned:
        pipe = Pipeline(backend="marker")
    else:
        pipe = Pipeline(backend="pdfplumber")

    result = pipe.convert("document.pdf")
    print(result.markdown[:500])


def example_batch() -> None:
    """Batch-convert a directory of PDFs with parallel workers."""
    from pdf2md import Pipeline

    pipe = Pipeline()
    results = pipe.convert_batch(
        "input_pdfs/",
        output_dir="output_markdown/",
        workers=4,
        validate_output=True,
    )

    for r in results:
        status = "PASS" if r.validation and r.validation.passed else "FAIL"
        print(f"  {r.source.name}: {status} ({r.token_estimate:,} tokens)")


def example_pipeline_for_llm() -> None:
    """Full pipeline building an LLM context string with converted markdown."""
    from pdf2md import Pipeline

    pipe = Pipeline()
    result = pipe.convert("report.pdf", validate_output=True)

    context = (
        f"The following document was extracted from {result.source.name} "
        f"({result.page_count} pages, ~{result.token_estimate:,} tokens).\n\n"
        f"{result.markdown}"
    )

    if result.validation and not result.validation.passed:
        context += (
            "\n\n[NOTE: Document extraction quality is below threshold. "
            "Some content may be missing or inaccurate.]"
        )

    print(f"Context length: {len(context):,} chars")
    print(f"Estimated tokens: {len(context) // 4:,}")

    output = Path("llm_context.txt")
    output.write_text(context, encoding="utf-8")
    print(f"Saved to {output}")
