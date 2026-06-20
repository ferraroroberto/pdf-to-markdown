"""Shared chunk-conversion orchestration.

One ``convert_chunked`` core used by all three chunk drivers — the Execute tab
worker (``app/execute.py``), the batch processor (``src/batch.py``), and the
CLI (``src/cli.py``).  Before this module those three carried near-identical,
already-drifted copies of the "split → per-chunk convert → merge with fallback"
loop (see ``ferraroroberto/pdf-to-markdown#43``).

The orchestrator owns only what is genuinely common — splitting, the optional
``max_chunks`` slice, the per-chunk ``pipe.convert`` call, markdown collection,
and the ``merge_chunks``-with-plain-join fallback.  Everything caller-specific
(resume, verbose artifact saving, progress strings, exec logging, per-chunk
error policy, metadata aggregation) is supplied through keyword hooks so each
driver keeps its exact observable behavior:

- ``resume_lookup(spec)``     — return saved markdown to skip the API call (Execute).
- ``chunk_kwargs(spec)``      — per-chunk backend-kwarg overrides (Execute verbose).
- ``failed_markdown(spec,exc)`` — text a failed chunk contributes to the merge.
- ``raise_on_chunk_error``    — re-raise instead of recording (CLI).
- ``on_split(specs, total)``  — fired once after the slice (progress, resume banner).
- ``on_chunk_start(spec)``    — fired before each conversion (live progress).
- ``on_chunk(outcome)``       — fired after each chunk (artifacts, logging, results).
- ``merge_fallback``          — wrap ``merge_chunks`` in a plain-join fallback (Execute).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from src.chunker import merge_chunks, split_pdf

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.pipeline import Pipeline

logger = logging.getLogger("chunk_runner")

# Plain-join separator used only when merge_chunks() itself raises.
_PLAIN_JOIN_SEPARATOR = "\n\n---\n\n"


@dataclass
class ChunkSpec:
    """A single page-range chunk to convert."""

    idx: int            # 0-based chunk index
    num: int            # 1-based chunk number (idx + 1)
    path: Path          # chunk PDF slice on disk
    start_page: int     # 0-based, inclusive (includes overlap)
    end_page: int       # 0-based, inclusive
    pages_label: str    # f"{start_page}{dash}{end_page}" — caller chooses the dash


@dataclass
class ChunkOutcome:
    """Result of processing one chunk, handed to ``on_chunk``."""

    spec: ChunkSpec
    markdown: str                       # text this chunk contributes to the merge
    metadata: dict = field(default_factory=dict)
    backend_used: str = ""
    error: Optional[str] = None
    resumed: bool = False


def convert_chunked(
    working_pdf: Path,
    pipe: "Pipeline",
    backend_kwargs: dict,
    chunk_size: int,
    chunk_overlap: int,
    *,
    validate_output: bool = False,
    max_chunks: int = 0,
    output_dir: Optional[Path] = None,
    file_stem: Optional[str] = None,
    pages_dash: str = "-",
    raise_on_chunk_error: bool = False,
    failed_markdown: Optional[Callable[[ChunkSpec, Exception], str]] = None,
    resume_lookup: Optional[Callable[[ChunkSpec], Optional[str]]] = None,
    chunk_kwargs: Optional[Callable[[ChunkSpec], dict]] = None,
    on_split: Optional[Callable[[list[ChunkSpec], int], None]] = None,
    on_chunk_start: Optional[Callable[[ChunkSpec], None]] = None,
    on_chunk: Optional[Callable[[ChunkOutcome], None]] = None,
    merge_fallback: bool = False,
) -> tuple[list[ChunkOutcome], str]:
    """Split *working_pdf* into chunks, convert each via *pipe*, and merge.

    Parameters
    ----------
    working_pdf:
        PDF to split.  Non-PDF inputs must already be pre-converted by the
        caller (the pre-conversion policy differs per driver, so it stays
        caller-side).
    pipe:
        A ``Pipeline`` (or any object exposing ``convert(path, validate_output,
        **kwargs)``).
    backend_kwargs:
        Base keyword arguments forwarded to ``pipe.convert`` for every chunk.
    chunk_size, chunk_overlap:
        Forwarded to :func:`src.chunker.split_pdf`.
    validate_output:
        Passed through to each ``pipe.convert`` call.
    max_chunks:
        If ``> 0`` and smaller than the number of chunks, only the first
        ``max_chunks`` are processed.
    output_dir, file_stem:
        When both are given, chunk PDFs are written with the flat
        ``{file_stem}.chunk_NNN.pdf`` layout (Execute tab); otherwise the legacy
        ``_chunks_<stem>/`` layout is used.
    pages_dash:
        Character placed between start/end page numbers in ``ChunkSpec.pages_label``.
    raise_on_chunk_error:
        Re-raise a chunk conversion error instead of recording it (CLI).
    failed_markdown:
        ``(spec, exc) -> str`` producing the text a failed chunk contributes to
        the merge.  Defaults to an empty string (the chunk drops out of the merge).
    resume_lookup:
        ``(spec) -> str | None``.  When it returns a string, that markdown is
        used and the API call is skipped (the outcome is marked ``resumed``).
    chunk_kwargs:
        ``(spec) -> dict`` of per-chunk overrides merged onto *backend_kwargs*.
    on_split:
        ``(specs, total_available) -> None`` fired once after slicing — *specs*
        is the list actually processed, *total_available* the pre-slice count.
    on_chunk_start:
        ``(spec) -> None`` fired before each conversion (skipped for resumed chunks).
    on_chunk:
        ``(outcome) -> None`` fired after every chunk (including resumed/failed).
    merge_fallback:
        Wrap the final ``merge_chunks`` in a plain ``---`` join fallback if it raises.

    Returns
    -------
    ``(outcomes, merged_markdown)`` — the per-chunk outcomes in order and the
    merged document.  Callers build their own final result/metadata from the
    outcomes.
    """
    raw = split_pdf(
        working_pdf,
        chunk_size=chunk_size,
        overlap=chunk_overlap,
        output_dir=output_dir,
        file_stem=file_stem,
    )
    total_available = len(raw)
    specs = [
        ChunkSpec(idx, idx + 1, path, start, end, f"{start}{pages_dash}{end}")
        for (idx, path, start, end) in raw
    ]
    if max_chunks > 0 and max_chunks < total_available:
        specs = specs[:max_chunks]

    if on_split is not None:
        on_split(specs, total_available)

    outcomes: list[ChunkOutcome] = []
    for spec in specs:
        # --- Resume: reuse saved markdown, skip the API call ---
        if resume_lookup is not None:
            existing = resume_lookup(spec)
            if existing is not None:
                outcome = ChunkOutcome(spec=spec, markdown=existing, resumed=True)
                outcomes.append(outcome)
                if on_chunk is not None:
                    on_chunk(outcome)
                continue

        if on_chunk_start is not None:
            on_chunk_start(spec)

        kwargs = dict(backend_kwargs)
        if chunk_kwargs is not None:
            kwargs.update(chunk_kwargs(spec))

        try:
            result = pipe.convert(spec.path, validate_output=validate_output, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if raise_on_chunk_error:
                raise
            md = failed_markdown(spec, exc) if failed_markdown is not None else ""
            outcome = ChunkOutcome(spec=spec, markdown=md, error=str(exc))
            outcomes.append(outcome)
            if on_chunk is not None:
                on_chunk(outcome)
            continue

        outcome = ChunkOutcome(
            spec=spec,
            markdown=result.markdown,
            metadata=result.metadata,
            backend_used=result.backend_used,
        )
        outcomes.append(outcome)
        if on_chunk is not None:
            on_chunk(outcome)

    markdowns = [o.markdown for o in outcomes]
    if merge_fallback:
        try:
            merged = merge_chunks(markdowns, chunk_overlap=chunk_overlap)
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ merge_chunks failed (%s) — falling back to plain join", exc)
            merged = _PLAIN_JOIN_SEPARATOR.join(m for m in markdowns if m and m.strip())
    else:
        merged = merge_chunks(markdowns, chunk_overlap=chunk_overlap)

    return outcomes, merged
