"""Command-line interface for pdf2md (Click + Rich)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from src.backends import BACKEND_REGISTRY, get_best_available, list_available
from src.classifier import classify_pdf
from src.pipeline import Pipeline
from src.validation import validate as run_validation

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


@click.group()
@click.version_option(version="0.1.0")
def main() -> None:
    """Convert PDF documents into clean Markdown for LLMs."""


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
@click.option("--device", type=click.Choice(["cuda", "cpu", "mps"]), default=None,
              help="Device for marker backend (cuda=m GPU, mps=Apple Silicon). Default: auto (GPU if available).")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def convert(
    input_path: str,
    output_path: str | None,
    backend: str,
    validate_output: bool,
    workers: int,
    force_ocr: bool,
    page_range: str | None,
    device: str | None,
    verbose: bool,
) -> None:
    """Convert a single PDF or a directory of PDFs to Markdown."""
    _setup_logging(verbose)

    input_p = Path(input_path)
    if backend == "auto":
        backend_name = _prompt_backend_choice(input_p)
    else:
        backend_name = backend
    pipe = Pipeline(backend=backend_name)
    backend_kwargs: dict = {}
    if force_ocr:
        backend_kwargs["force_ocr"] = True
    if page_range:
        backend_kwargs["page_range"] = page_range
    if device is not None:
        backend_kwargs["device"] = device

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
    table = Table(title="Backends")
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


def _prompt_backend_choice(input_path: Path) -> str:
    """Ask user to pick a backend; recommend one based on input (and PDF type if file)."""
    available = list_available()
    if not available:
        raise RuntimeError(
            "No PDF extraction backend is available. "
            "Install at least pdfplumber (pip install pdfplumber)."
        )

    needs_ocr = False
    if input_path.is_file() and input_path.suffix.lower() == ".pdf":
        try:
            pdf_info = classify_pdf(input_path)
            needs_ocr = pdf_info.is_scanned
        except Exception:
            pass
    elif input_path.is_dir():
        first_pdf = next(input_path.rglob("*.pdf"), None)
        if first_pdf:
            try:
                pdf_info = classify_pdf(first_pdf)
                needs_ocr = pdf_info.is_scanned
            except Exception:
                pass

    recommended = get_best_available(needs_ocr=needs_ocr)
    recommended_name = recommended.name
    # Order: recommended first, then rest of available
    ordered = [recommended_name] + [b for b in available if b != recommended_name]
    labels = []
    for i, name in enumerate(ordered, 1):
        tag = " (recommended)" if name == recommended_name else ""
        labels.append(f"{i}. {name}{tag}")
    console.print("\n[bold]Choose extraction backend:[/bold]")
    console.print("  " + "  ".join(labels))
    choice = click.prompt(
        "Select",
        type=click.IntRange(1, len(ordered)),
        default=1,
        show_default=True,
    )
    return ordered[choice - 1]


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
