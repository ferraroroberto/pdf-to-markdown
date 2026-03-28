"""Command-line interface for pdf2md (Click + Rich)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from src.backends import BACKEND_REGISTRY, list_available
from src.config import load_settings
from src.models import ConversionResult
from src.validation import validate as run_validation

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


@click.group()
@click.version_option(version="0.2.0")
def main() -> None:
    """Convert PDF documents into clean Markdown for LLMs."""


@main.command()
@click.argument("input_path", type=click.Path(exists=True))
@click.option("-o", "--output", "output_path", type=click.Path(), default=None,
              help="Output file or directory.")
@click.option("-b", "--backend",
              type=click.Choice(["marker", "pdfplumber", "vertexai"]),
              default=None,
              help="Extraction backend. Default: from config.json.")
@click.option("--auth-mode",
              type=click.Choice(["api", "gcloud"]),
              default=None,
              help="Vertex AI auth mode. Default: from config.json.")
@click.option("--project-id", default=None,
              help="Google Cloud project ID. Falls back to PROJECT_ID env var.")
@click.option("--location", default=None,
              help="Vertex AI region, e.g. europe-west3.")
@click.option("--model", default=None,
              help="Gemini model ID, e.g. gemini-2.5-pro.")
@click.option("--refine-iterations", type=int, default=None,
              help="Iterative refinement passes (0 = extraction only).")
@click.option("--chunk-size", type=int, default=None,
              help="Pages per chunk (0 = no chunking). Enables large-PDF splitting.")
@click.option("--chunk-overlap", type=int, default=None,
              help="Overlap pages between chunks to preserve context (default: 1).")
@click.option("--workers", type=int, default=None,
              help="Parallel workers for batch processing.")
@click.option("--extensions", default=None,
              help="Comma-separated file extensions for batch (e.g. '.pdf,.docx,.pptx'). Default: from config.")
@click.option("--validate", "validate_output", is_flag=True,
              help="Run quality validation on the output.")
@click.option("--dry-run", is_flag=True,
              help="Estimate token counts and cost without calling the API.")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def convert(
    input_path: str,
    output_path: str | None,
    backend: str | None,
    auth_mode: str | None,
    project_id: str | None,
    location: str | None,
    model: str | None,
    refine_iterations: int | None,
    chunk_size: int | None,
    chunk_overlap: int | None,
    workers: int | None,
    extensions: str | None,
    validate_output: bool,
    dry_run: bool,
    verbose: bool,
) -> None:
    """Convert a single PDF/Word/PowerPoint/image or a directory of files to Markdown."""
    _setup_logging(verbose)

    # Build CLI override dict — only include explicitly provided values
    cli_overrides: dict = {}
    vai_overrides: dict = {}
    proc_overrides: dict = {}

    if auth_mode is not None:
        vai_overrides["auth_mode"] = auth_mode
    if project_id is not None:
        vai_overrides["project_id"] = project_id
    elif os.getenv("PROJECT_ID"):
        vai_overrides["project_id"] = os.environ["PROJECT_ID"]
    if location is not None:
        vai_overrides["location"] = location
    if model is not None:
        vai_overrides["model"] = model
    if refine_iterations is not None:
        vai_overrides["refine_iterations"] = refine_iterations
    if backend is not None:
        proc_overrides["backend"] = backend
    if chunk_size is not None:
        proc_overrides["chunk_size"] = chunk_size
    if chunk_overlap is not None:
        proc_overrides["chunk_overlap"] = chunk_overlap
    if workers is not None:
        proc_overrides["workers"] = workers

    batch_overrides: dict = {}
    if extensions is not None:
        batch_overrides["extensions"] = [e.strip() for e in extensions.split(",")]

    if vai_overrides:
        cli_overrides["vertexai"] = vai_overrides
    if proc_overrides:
        cli_overrides["processing"] = proc_overrides
    if batch_overrides:
        cli_overrides["batch"] = batch_overrides

    settings = load_settings(cli_overrides)

    input_p = Path(input_path)

    if input_p.is_dir():
        _run_batch(input_p, output_path, settings, validate_output, dry_run)
    else:
        _run_single(input_p, output_path, settings, validate_output, dry_run)


def _run_single(
    pdf_path: Path,
    output_path: str | None,
    settings,
    validate_output: bool,
    dry_run: bool,
) -> None:
    from src.chunker import split_pdf, merge_chunks
    from src.pipeline import Pipeline

    backend_name = settings.processing.backend
    chunk_size = settings.processing.chunk_size
    chunk_overlap = settings.processing.chunk_overlap

    backend_kwargs = _build_backend_kwargs(settings, dry_run)
    pipe = Pipeline(backend=backend_name)

    from src.file_converter import needs_conversion
    if needs_conversion(pdf_path) and chunk_size > 0:
        console.print(f"[yellow]Note:[/yellow] Chunking is not supported for {pdf_path.suffix} files — ignoring chunk size.")
        chunk_size = 0

    if chunk_size > 0:
        console.print(f"[cyan]Chunking enabled:[/cyan] {chunk_size} pages/chunk, overlap={chunk_overlap}")
        chunks = split_pdf(pdf_path, chunk_size=chunk_size, overlap=chunk_overlap)
        console.print(f"  Split into {len(chunks)} chunk(s)")

        results = []
        for chunk_idx, chunk_path, start_page, end_page in chunks:
            console.print(f"  Processing chunk {chunk_idx + 1}/{len(chunks)} (pages {start_page}–{end_page})…")
            r = pipe.convert(chunk_path, validate_output=validate_output, **backend_kwargs)
            results.append(r)

        merged_md = merge_chunks([r.markdown for r in results])
        last = results[-1]
        result = ConversionResult(
            source=pdf_path,
            markdown=merged_md,
            backend_used=last.backend_used,
            metadata={
                **last.metadata,
                "page_count": sum(r.metadata.get("page_count", 0) for r in results),
                "chunks": len(results),
            },
        )
    else:
        result = pipe.convert(pdf_path, validate_output=validate_output, **backend_kwargs)

    out = _resolve_output(pdf_path, output_path)
    if out:
        result.save(out)
        console.print(f"[green]Saved to {out}[/green]")
    else:
        console.print(result.markdown)

    _print_summary(result)


def _run_batch(
    folder: Path,
    output_path: str | None,
    settings,
    validate_output: bool,
    dry_run: bool,
) -> None:
    from src.batch import run_batch

    if not output_path:
        console.print("[red]Error:[/red] --output is required for directory input.")
        sys.exit(1)

    results = run_batch(
        folder=folder,
        output_dir=Path(output_path),
        settings=settings,
        validate_output=validate_output,
        dry_run=dry_run,
        on_progress=lambda msg: console.print(f"  {msg}"),
    )
    console.print(f"\n[green]Batch complete: {len(results)} file(s)[/green]")
    for r in results:
        _print_summary(r)


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


def _build_backend_kwargs(settings, dry_run: bool = False) -> dict:
    """Build kwargs dict for the Vertex AI backend from resolved settings."""
    vai = settings.vertexai
    return {
        "project_id": vai.project_id or os.getenv("PROJECT_ID", ""),
        "location": vai.location,
        "model_id": vai.model,
        "auth_mode": vai.auth_mode,
        "refine_iterations": vai.refine_iterations,
        "clean_stop_max_errors": vai.clean_stop_max_errors,
        "extraction_prompt_file": vai.extraction_prompt,
        "refinement_prompt_file": vai.refinement_prompt,
        "dry_run": dry_run,
    }


def _resolve_output(pdf_path: Path, output_path: str | None) -> Path | None:
    if output_path is None:
        return None
    out = Path(output_path)
    if out.is_dir():
        return out / (pdf_path.stem + ".md")
    return out


def _print_summary(result: ConversionResult) -> None:
    console.print(f"\n  Source  : {result.source}")
    console.print(f"  Backend : {result.backend_used}")
    if result.page_count is not None:
        console.print(f"  Pages   : {result.page_count}")
    console.print(f"  Chars   : {len(result.markdown):,}")
    console.print(f"  Tokens  : ~{result.token_estimate:,}")
    if result.validation:
        status = "[green]PASS[/green]" if result.validation.passed else "[red]FAIL[/red]"
        console.print(f"  Valid.  : {status} ({result.validation.char_similarity:.1%} similarity)")
