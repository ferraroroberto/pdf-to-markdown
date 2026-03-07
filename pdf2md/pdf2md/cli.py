"""Command-line interface for pdf2md (Click + Rich)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from pdf2md.backends import BACKEND_REGISTRY, list_available
from pdf2md.pipeline import Pipeline
from pdf2md.validation import validate as run_validation

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


@click.group()
@click.version_option(package_name="pdf2md")
def main() -> None:
    """pdf2md — Convert PDF documents into clean Markdown for LLMs."""


@main.command()
@click.argument("input_path", type=click.Path(exists=True))
@click.option("-o", "--output", "output_path", type=click.Path(), default=None,
              help="Output file or directory.")
@click.option("-b", "--backend",
              type=click.Choice(["marker", "docling", "pdfplumber", "auto"]),
              default="auto", help="Extraction backend.")
@click.option("--validate", "validate_output", is_flag=True,
              help="Run quality validation on the output.")
@click.option("--workers", type=int, default=1,
              help="Number of parallel workers for batch processing.")
@click.option("--force-ocr", is_flag=True, help="Force OCR even on born-digital PDFs.")
@click.option("--page-range", type=str, default=None,
              help="Page range to convert (e.g. '0-5').")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def convert(
    input_path: str,
    output_path: str | None,
    backend: str,
    validate_output: bool,
    workers: int,
    force_ocr: bool,
    page_range: str | None,
    verbose: bool,
) -> None:
    """Convert a single PDF or a directory of PDFs to Markdown."""
    _setup_logging(verbose)

    backend_name = None if backend == "auto" else backend
    pipe = Pipeline(backend=backend_name)

    input_p = Path(input_path)
    backend_kwargs: dict = {}
    if force_ocr:
        backend_kwargs["force_ocr"] = True
    if page_range:
        backend_kwargs["page_range"] = page_range

    if input_p.is_dir():
        if not output_path:
            console.print("[red]Error:[/red] --output is required for directory input.")
            sys.exit(1)
        results = pipe.convert_batch(
            input_p, output_dir=output_path, workers=workers,
            validate_output=validate_output, **backend_kwargs,
        )
        console.print(f"\n[green]Converted {len(results)} file(s)[/green]")
        for r in results:
            _print_summary(r)
    else:
        result = pipe.convert(
            input_p, validate_output=validate_output, **backend_kwargs,
        )
        if output_path:
            out = Path(output_path)
            if out.is_dir():
                out = out / (input_p.stem + ".md")
            result.save(out)
            console.print(f"[green]Saved to {out}[/green]")
        else:
            console.print(result.markdown)

        _print_summary(result)


@main.command()
@click.argument("pdf_path", type=click.Path(exists=True))
@click.argument("markdown_path", type=click.Path(exists=True))
def validate(pdf_path: str, markdown_path: str) -> None:
    """Run validation on an existing Markdown file against its source PDF."""
    md_text = Path(markdown_path).read_text(encoding="utf-8")
    report = run_validation(pdf_path, md_text)
    console.print(report.summary())
    if not report.passed:
        sys.exit(1)


@main.command()
def backends() -> None:
    """List available extraction backends."""
    available = set(list_available())
    table = Table(title="pdf2md Backends")
    table.add_column("Backend", style="cyan")
    table.add_column("Status")
    table.add_column("Scanned PDF Support")

    for cls in BACKEND_REGISTRY:
        name = cls.name
        status = "[green]Available[/green]" if name in available else "[red]Not installed[/red]"
        scanned = "Yes" if cls().supports_scanned() else "No"
        table.add_row(name, status, scanned)

    console.print(table)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _print_summary(result) -> None:  # noqa: ANN001
    """Print a short summary of a conversion result."""
    console.print(f"\n  Source  : {result.source}")
    console.print(f"  Backend : {result.backend_used}")
    if result.page_count is not None:
        console.print(f"  Pages   : {result.page_count}")
    console.print(f"  Chars   : {len(result.markdown):,}")
    console.print(f"  Tokens  : ~{result.token_estimate:,}")
    if result.validation:
        status = "[green]PASS[/green]" if result.validation.passed else "[red]FAIL[/red]"
        console.print(f"  Valid.  : {status} ({result.validation.char_similarity:.1%} similarity)")
