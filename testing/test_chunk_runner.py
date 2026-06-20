"""Tests for src/chunk_runner.py — shared chunk-conversion orchestration.

Drives ``convert_chunked`` with a fake pipe (no API, no Streamlit) to lock the
behavior the three real drivers (Execute tab, batch, CLI) depend on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.chunk_runner import ChunkOutcome, ChunkSpec, convert_chunked
from src.models import ConversionResult


class FakePipe:
    """Minimal ``Pipeline`` stand-in: records calls, returns canned results."""

    def __init__(self, fail_idxs: tuple[int, ...] = (), backend: str = "fakebackend"):
        self.fail_idxs = set(fail_idxs)
        self.backend = backend
        self.calls: list[tuple[Path, bool, dict]] = []

    def convert(self, path: Path, validate_output: bool = False, **kwargs) -> ConversionResult:
        n = len(self.calls)
        self.calls.append((Path(path), validate_output, kwargs))
        if n in self.fail_idxs:
            raise RuntimeError(f"boom-{n}")
        return ConversionResult(
            source=Path(path),
            markdown=f"# chunk {n}\n\nbody {n}",
            backend_used=self.backend,
            metadata={"page_count": 1, "call_index": n},
        )


# ---------------------------------------------------------------------------
# Basic split → convert → merge
# ---------------------------------------------------------------------------


class TestBasicFlow:
    def test_three_chunks_converted_and_merged(self, minimal_pdf):
        pipe = FakePipe()
        outcomes, merged = convert_chunked(minimal_pdf, pipe, {}, chunk_size=2, chunk_overlap=0)
        # 5 pages / 2 = 3 chunks
        assert len(outcomes) == 3
        assert len(pipe.calls) == 3
        for o in outcomes:
            assert o.error is None
            assert o.backend_used == "fakebackend"
        assert "chunk 0" in merged and "chunk 1" in merged and "chunk 2" in merged

    def test_validate_output_forwarded(self, minimal_pdf):
        pipe = FakePipe()
        convert_chunked(minimal_pdf, pipe, {}, chunk_size=2, chunk_overlap=0, validate_output=True)
        assert all(call[1] is True for call in pipe.calls)

    def test_backend_kwargs_forwarded(self, minimal_pdf):
        pipe = FakePipe()
        convert_chunked(minimal_pdf, pipe, {"force_ocr": True}, chunk_size=5, chunk_overlap=0)
        assert pipe.calls[0][2] == {"force_ocr": True}

    def test_chunk_kwargs_overrides_merged(self, minimal_pdf):
        pipe = FakePipe()
        convert_chunked(
            minimal_pdf, pipe, {"base": 1}, chunk_size=2, chunk_overlap=0,
            chunk_kwargs=lambda spec: {"stem": f"c{spec.num}"},
        )
        assert pipe.calls[0][2] == {"base": 1, "stem": "c1"}
        assert pipe.calls[1][2] == {"base": 1, "stem": "c2"}

    def test_pages_label_uses_dash(self, minimal_pdf):
        seen: list[str] = []
        convert_chunked(
            minimal_pdf, FakePipe(), {}, chunk_size=2, chunk_overlap=0,
            pages_dash="–", on_chunk=lambda o: seen.append(o.spec.pages_label),
        )
        assert all("–" in label for label in seen)


# ---------------------------------------------------------------------------
# max_chunks slicing
# ---------------------------------------------------------------------------


class TestMaxChunks:
    def test_slices_to_first_n(self, minimal_pdf):
        pipe = FakePipe()
        outcomes, _ = convert_chunked(
            minimal_pdf, pipe, {}, chunk_size=2, chunk_overlap=0, max_chunks=1,
        )
        assert len(outcomes) == 1
        assert len(pipe.calls) == 1

    def test_on_split_sees_total_and_processed(self, minimal_pdf):
        captured: dict = {}

        def _on_split(specs: list[ChunkSpec], total: int) -> None:
            captured["processed"] = len(specs)
            captured["total"] = total

        convert_chunked(
            minimal_pdf, FakePipe(), {}, chunk_size=2, chunk_overlap=0,
            max_chunks=1, on_split=_on_split,
        )
        assert captured == {"processed": 1, "total": 3}

    def test_zero_max_chunks_processes_all(self, minimal_pdf):
        outcomes, _ = convert_chunked(
            minimal_pdf, FakePipe(), {}, chunk_size=2, chunk_overlap=0, max_chunks=0,
        )
        assert len(outcomes) == 3


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class TestResume:
    def test_resume_skips_conversion(self, minimal_pdf):
        pipe = FakePipe()
        starts: list[int] = []

        def _resume(spec: ChunkSpec) -> str | None:
            return "SAVED" if spec.idx == 0 else None

        outcomes, merged = convert_chunked(
            minimal_pdf, pipe, {}, chunk_size=2, chunk_overlap=0,
            resume_lookup=_resume,
            on_chunk_start=lambda spec: starts.append(spec.idx),
        )
        # First chunk resumed → not converted, no on_chunk_start
        assert outcomes[0].resumed is True
        assert outcomes[0].markdown == "SAVED"
        assert 0 not in starts
        assert len(pipe.calls) == 2  # chunks 1 and 2 only
        assert "SAVED" in merged

    def test_resumed_outcome_still_fires_on_chunk(self, minimal_pdf):
        seen: list[bool] = []

        def _resume(spec: ChunkSpec) -> str | None:
            return "X" if spec.idx == 0 else None

        convert_chunked(
            minimal_pdf, FakePipe(), {}, chunk_size=2, chunk_overlap=0,
            resume_lookup=_resume, on_chunk=lambda o: seen.append(o.resumed),
        )
        assert seen[0] is True
        assert seen[1] is False


# ---------------------------------------------------------------------------
# Error policy
# ---------------------------------------------------------------------------


class TestErrorPolicy:
    def test_record_error_default_empty_markdown(self, minimal_pdf):
        pipe = FakePipe(fail_idxs=(1,))
        outcomes, merged = convert_chunked(
            minimal_pdf, pipe, {}, chunk_size=2, chunk_overlap=0,
        )
        assert outcomes[1].error is not None
        assert outcomes[1].markdown == ""
        # Failed chunk drops out of the merge (empty filtered)
        assert "chunk 1" not in merged
        assert "chunk 0" in merged and "chunk 2" in merged

    def test_failed_markdown_contributes_placeholder(self, minimal_pdf):
        pipe = FakePipe(fail_idxs=(1,))
        outcomes, merged = convert_chunked(
            minimal_pdf, pipe, {}, chunk_size=2, chunk_overlap=0,
            failed_markdown=lambda spec, exc: f"FAILED {spec.num}: {exc}",
        )
        assert "FAILED 2" in outcomes[1].markdown
        assert "FAILED 2" in merged

    def test_raise_on_chunk_error_propagates(self, minimal_pdf):
        pipe = FakePipe(fail_idxs=(0,))
        with pytest.raises(RuntimeError, match="boom-0"):
            convert_chunked(
                minimal_pdf, pipe, {}, chunk_size=2, chunk_overlap=0,
                raise_on_chunk_error=True,
            )


# ---------------------------------------------------------------------------
# Merge fallback
# ---------------------------------------------------------------------------


class TestMergeFallback:
    def test_fallback_used_when_merge_raises(self, minimal_pdf, monkeypatch):
        def _boom(markdowns, chunk_overlap=0):
            raise ValueError("merge exploded")

        monkeypatch.setattr("src.chunk_runner.merge_chunks", _boom)
        pipe = FakePipe()
        _, merged = convert_chunked(
            minimal_pdf, pipe, {}, chunk_size=2, chunk_overlap=0, merge_fallback=True,
        )
        # Plain-join fallback still stitches the chunk bodies together
        assert "chunk 0" in merged and "chunk 2" in merged
        assert "---" in merged

    def test_no_fallback_lets_merge_raise(self, minimal_pdf, monkeypatch):
        def _boom(markdowns, chunk_overlap=0):
            raise ValueError("merge exploded")

        monkeypatch.setattr("src.chunk_runner.merge_chunks", _boom)
        with pytest.raises(ValueError, match="merge exploded"):
            convert_chunked(
                minimal_pdf, FakePipe(), {}, chunk_size=2, chunk_overlap=0,
                merge_fallback=False,
            )


# ---------------------------------------------------------------------------
# Chunk layout
# ---------------------------------------------------------------------------


class TestLayout:
    def test_flat_layout_when_output_dir_and_stem(self, minimal_pdf, tmp_path):
        out = tmp_path / "out"
        outcomes, _ = convert_chunked(
            minimal_pdf, FakePipe(), {}, chunk_size=2, chunk_overlap=0,
            output_dir=out, file_stem="doc",
        )
        assert (out / "doc.chunk_001.pdf").exists()
        assert outcomes[0].spec.path.name == "doc.chunk_001.pdf"

    def test_legacy_layout_when_omitted(self, minimal_pdf):
        convert_chunked(minimal_pdf, FakePipe(), {}, chunk_size=2, chunk_overlap=0)
        legacy = minimal_pdf.parent / f"_chunks_{minimal_pdf.stem}"
        assert legacy.exists()


# ---------------------------------------------------------------------------
# Hook ordering
# ---------------------------------------------------------------------------


def test_hook_order(minimal_pdf):
    events: list[str] = []
    convert_chunked(
        minimal_pdf, FakePipe(), {}, chunk_size=5, chunk_overlap=0,
        on_split=lambda specs, total: events.append("split"),
        on_chunk_start=lambda spec: events.append("start"),
        on_chunk=lambda o: events.append("done"),
    )
    # Single chunk (5 pages, chunk_size 5): split, then start, then done
    assert events == ["split", "start", "done"]
