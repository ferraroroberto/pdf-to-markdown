"""Path 1 of the #65 translation spike — structural-fidelity translation.

Pipeline shape::

    source PDF
      → [existing] extract + refine   (src.pipeline.Pipeline, hubgemini backend)
      → [new]      translate          (glossary-constrained, PDF supplied as context)
      → [new]      refine translation (structured JSON audit, CLEAN early-stop)
      → [new]      render             (Markdown → HTML → PDF, PyMuPDF Story)

The interesting claim this script exists to test is **not** "an LLM can translate
Markdown" — it obviously can. It is that the translate-then-refine half needs no
new orchestration: :func:`src.refinement.run_conversion` is already
transport-agnostic, so translation drops in as a transport swap. Everything the
extraction loop gives you — per-step token accounting, the ``refinement_log``
track record, the ``CLEAN`` early stop, the diminishing-returns stop, verbose
``raw_step_NN`` artifacts — comes along unchanged. ``extract_fn`` becomes
"translate this", ``refine_fn`` becomes "audit this translation".

One wrinkle worth knowing: ``run_conversion`` loads and hashes the prompt *file*,
so the hash recorded in the metadata is the hash of the **template**, before
``{{TARGET_LANGUAGE}}`` and ``{{GLOSSARY}}`` are substituted. Two runs with
different glossaries therefore share a prompt hash. For a spike that is fine; a
production version would need the glossary hashed into the execution-log row.

Usage (from the project root)::

    & .\\.venv\\Scripts\\python.exe -m spikes.translation.path1_translate \\
        tmp/translation_spike/act_scanned.pdf --refine-translation 2

Outputs land next to the input as ``<stem>.source.md``, ``<stem>.translated.md``,
``<stem>.translated.pdf`` and ``<stem>.report.json``.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF

from src.logging_config import setup_logging
from src.pipeline import Pipeline
from src.refinement import run_conversion

logger = logging.getLogger("path1_translate")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPIKE_DIR = Path(__file__).resolve().parent
TRANSLATE_PROMPT = "spikes/translation/prompts/translate.md"
TRANSLATE_REFINE_PROMPT = "spikes/translation/prompts/translate_refine.md"

HUB_BASE_URL = "http://127.0.0.1:8000"
HUB_API_KEY = "local-dummy"
DEFAULT_MODEL = "gemini_pro"
MAX_OUTPUT_TOKENS = 65536
REQUEST_TIMEOUT_S = 1800.0

# Fonts embedded into the rendered PDF. Arial covers Latin + Cyrillic + Greek;
# it does NOT cover CJK, which is exactly the gap the write-up records.
RENDER_FONTS = {
    "regular": Path("C:/Windows/Fonts/arial.ttf"),
    "bold": Path("C:/Windows/Fonts/arialbd.ttf"),
    "italic": Path("C:/Windows/Fonts/ariali.ttf"),
}


# ── Glossary ────────────────────────────────────────────────────────────────


def load_glossary(path: Path) -> tuple[str, str, int]:
    """Return ``(target_language, glossary_markdown, term_count)`` from a glossary JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    target = str(data.get("target_language", "English"))
    terms: dict[str, str] = data.get("terms", {})
    if not terms:
        return target, "_(no glossary supplied)_", 0
    rows = "\n".join(f"| {src} | {dst} |" for src, dst in terms.items())
    table = f"| Source term | Required target term |\n|---|---|\n{rows}"
    logger.info("ℹ️ Glossary: %d mandatory terms → %s", len(terms), target)
    return target, table, len(terms)


def _render_prompt(template: str, target_language: str, glossary: str) -> str:
    return template.replace("{{TARGET_LANGUAGE}}", target_language).replace(
        "{{GLOSSARY}}", glossary
    )


# ── Hub transport ───────────────────────────────────────────────────────────


def _hub_client() -> object:
    from anthropic import Anthropic

    return Anthropic(
        api_key=HUB_API_KEY,
        base_url=HUB_BASE_URL,
        timeout=REQUEST_TIMEOUT_S,
        max_retries=0,
    )


def _pdf_block(pdf_bytes: bytes) -> dict:
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(pdf_bytes).decode("ascii"),
        },
    }


def _call_hub(client: object, model_id: str, blocks: list[dict],
              system: str | None = None) -> tuple[str, dict]:
    kwargs: dict = {
        "model": model_id,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [{"role": "user", "content": blocks}],
    }
    if system:
        kwargs["system"] = system
    try:
        response = client.messages.create(**kwargs)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Hub call failed: %s: %s", type(exc).__name__, exc)
        raise RuntimeError(f"Hub call failed: {exc}") from exc

    text = "".join(
        getattr(b, "text", "") or ""
        for b in (getattr(response, "content", []) or [])
        if getattr(b, "type", None) == "text"
    )
    usage_obj = getattr(response, "usage", None)
    return text, {
        "input_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
    }


# ── Stage A: extract (existing pipeline, unchanged) ─────────────────────────


def extract_markdown(pdf_path: Path, *, backend: str, model_id: str,
                     refine_iterations: int) -> tuple[str, dict]:
    """Run the project's own extraction pipeline and return ``(markdown, metadata)``."""
    logger.info("ℹ️ Stage A — extraction (%s / %s, refine=%d)", backend, model_id, refine_iterations)
    result = Pipeline(backend=backend).convert(
        pdf_path,
        model_id=model_id,
        refine_iterations=refine_iterations,
        extraction_prompt_file="prompts/extraction_rag.md",
        refinement_prompt_file="prompts/refinement_rag.md",
    )
    logger.info("ℹ️ Stage A done — %d chars of source Markdown", len(result.markdown))
    return result.markdown, dict(result.metadata)


# ── Stages B + C: translate + refine, via the existing orchestrator ─────────


def translate_with_refinement(
    *,
    pdf_path: Path,
    source_markdown: str,
    target_language: str,
    glossary: str,
    model_id: str,
    refine_iterations: int,
    verbose_dir: Path | None,
    verbose_stem: str,
) -> tuple[str, dict]:
    """Translate *source_markdown*, then audit the translation, reusing ``run_conversion``.

    ``run_conversion`` believes it is doing extraction-then-refinement. We hand it
    a transport whose "extraction" call is a translation and whose "refinement"
    call is a bilingual audit; every loop control it owns (early stop, token
    accounting, track record, verbose artifacts) applies unchanged.
    """
    logger.info("ℹ️ Stages B+C — translation → %s (refine=%d)", target_language, refine_iterations)

    def build_transports(translate_template: str, refine_template: str):
        client = _hub_client()
        block = _pdf_block(pdf_path.read_bytes())
        translate_system = _render_prompt(translate_template, target_language, glossary)
        refine_system = _render_prompt(refine_template, target_language, glossary)

        def extract_fn() -> tuple[str, dict]:
            return _call_hub(
                client, model_id,
                blocks=[
                    block,
                    {"type": "text",
                     "text": "## Source-language Markdown to translate\n\n" + source_markdown},
                ],
                system=translate_system,
            )

        def refine_fn(user_message: str) -> tuple[str, dict]:
            # user_message already carries the current translation; prepend the
            # source Markdown so the auditor sees both sides plus the PDF.
            return _call_hub(
                client, model_id,
                blocks=[
                    block,
                    {"type": "text",
                     "text": "## Source-language Markdown (ground truth)\n\n"
                             + source_markdown
                             + "\n\n---\n\n" + user_message},
                ],
                system=refine_system,
            )

        return extract_fn, refine_fn

    return run_conversion(
        backend_name="spike-translate",
        display_name="Translation (path 1)",
        model_id=model_id,
        auth_mode="hub",
        model_phrase="via hub model=",
        pdf_path=pdf_path,
        refine_iterations=refine_iterations,
        clean_stop_max_errors=0,
        diminishing_returns_enabled=True,
        dry_run=False,
        extraction_prompt_file=TRANSLATE_PROMPT,
        refinement_prompt_file=TRANSLATE_REFINE_PROMPT,
        verbose_save_dir=verbose_dir,
        verbose_file_stem=verbose_stem,
        build_transports=build_transports,
    )


# ── Stage D: render ─────────────────────────────────────────────────────────

_RENDER_CSS = """
@font-face { font-family: doc; src: url(arial.ttf); }
@font-face { font-family: doc; font-weight: bold; src: url(arialbd.ttf); }
@font-face { font-family: doc; font-style: italic; src: url(ariali.ttf); }
body { font-family: doc; font-size: 9.5px; color: #0d0d14; }
h1 { font-size: 15px; text-align: center; margin: 0 0 4px 0; }
h2 { font-size: 11.5px; margin: 12px 0 3px 0; }
h3 { font-size: 10px; margin: 9px 0 3px 0; }
p  { margin: 0 0 6px 0; }
table { width: 100%; border: 1px solid #b8b8c2; }
th { background-color: #eeeff7; font-weight: bold; text-align: left;
     border: 1px solid #b8b8c2; padding: 3px; font-size: 8.5px; }
td { border: 1px solid #b8b8c2; padding: 3px; font-size: 8.5px; }
hr { margin: 8px 0; }
"""


def _font_archive() -> object:
    """Copy the render fonts into a scratch dir and wrap it in a PyMuPDF Archive.

    Story resolves ``@font-face { src: url(arial.ttf) }`` against the archive, so
    the fonts have to live in one directory it can see. Arial covers Latin,
    Cyrillic and Greek but **not** CJK — a CJK target would need Noto/Source Han.
    """
    font_dir = PROJECT_ROOT / "tmp" / "translation_spike" / "_fonts"
    font_dir.mkdir(parents=True, exist_ok=True)
    for src in RENDER_FONTS.values():
        if not src.exists():
            raise FileNotFoundError(f"Render font not found: {src}")
        dest = font_dir / src.name.lower()
        if not dest.exists():
            shutil.copyfile(src, dest)
    return fitz.Archive(str(font_dir))


def render_pdf(markdown_text: str, dest: Path, *, title: str) -> Path:
    """Render *markdown_text* to a paginated PDF at *dest* (Markdown → HTML → Story).

    ``markdown_it`` is not a declared dependency of this project — it arrives
    transitively via ``rich``. That is acceptable for a spike (the rule is "add
    nothing to requirements.txt") but a production renderer would have to
    declare it, or hand-roll the small Markdown subset it needs.
    """
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:  # pragma: no cover — spike-only path
        raise ImportError(
            "markdown-it-py is required to render. It normally arrives with "
            "`rich`; install it explicitly with: pip install markdown-it-py"
        ) from exc

    html_body = MarkdownIt("commonmark").enable("table").render(markdown_text)
    html = f"<html><body><h1 style='display:none'>{title}</h1>{html_body}</body></html>"

    story = fitz.Story(html=html, user_css=_RENDER_CSS, em=12, archive=_font_archive())

    page_rect = fitz.Rect(0, 0, *fitz.paper_size("a4"))
    content_rect = page_rect + (56, 56, -56, -56)

    writer = fitz.DocumentWriter(str(dest))
    more = 1
    pages = 0
    while more:
        device = writer.begin_page(page_rect)
        more, _filled = story.place(content_rect)
        story.draw(device)
        writer.end_page()
        pages += 1
        if pages > 200:  # runaway guard — a malformed story can loop forever
            raise RuntimeError("Render exceeded 200 pages; aborting.")
    writer.close()
    logger.info("ℹ️ Stage D — rendered %d page(s) → %s", pages, dest)
    return dest


# ── Orchestration ───────────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> int:
    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        logger.error("❌ Input not found: %s", pdf_path)
        return 2

    glossary_path = Path(args.glossary)
    if not glossary_path.is_absolute():
        glossary_path = SPIKE_DIR / glossary_path
    target_language, glossary, glossary_terms = load_glossary(glossary_path)

    out_dir = Path(args.output) if args.output else pdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem

    started = time.time()

    source_md, extract_meta = extract_markdown(
        pdf_path,
        backend=args.backend,
        model_id=args.model,
        refine_iterations=args.refine_extraction,
    )
    (out_dir / f"{stem}.source.md").write_text(source_md, encoding="utf-8")

    translated_md, translate_meta = translate_with_refinement(
        pdf_path=pdf_path,
        source_markdown=source_md,
        target_language=target_language,
        glossary=glossary,
        model_id=args.model,
        refine_iterations=args.refine_translation,
        verbose_dir=out_dir,
        verbose_stem=f"{stem}.translate",
    )
    (out_dir / f"{stem}.translated.md").write_text(translated_md, encoding="utf-8")

    rendered = render_pdf(
        translated_md, out_dir / f"{stem}.translated.pdf", title=stem
    )

    report = {
        "source_pdf": str(pdf_path),
        "target_language": target_language,
        "glossary_terms": glossary_terms,
        "elapsed_s": round(time.time() - started, 1),
        "source_markdown_chars": len(source_md),
        "translated_markdown_chars": len(translated_md),
        "rendered_pdf": str(rendered),
        "extraction": {
            k: extract_meta.get(k)
            for k in ("backend", "model", "iterations_completed", "final_verdict",
                      "total_input_tokens", "total_output_tokens", "page_count")
        },
        "translation": {
            k: translate_meta.get(k)
            for k in ("model", "iterations_completed", "final_verdict",
                      "total_input_tokens", "total_output_tokens")
        },
        "translation_track_record": translate_meta.get("refinement_log", []),
        "translation_corrections": translate_meta.get("all_corrections", []),
    }
    report_path = out_dir / f"{stem}.report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(
        "✅ Path 1 complete in %.1fs — %d → %d chars, %d translation pass(es), verdict=%s",
        report["elapsed_s"], len(source_md), len(translated_md),
        translate_meta.get("iterations_completed", 0),
        translate_meta.get("final_verdict"),
    )
    logger.info("ℹ️ Report → %s", report_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Path 1 (structural-fidelity) translation spike for issue #65."
    )
    parser.add_argument("pdf", help="Source PDF (or any file the pipeline accepts).")
    parser.add_argument("-o", "--output", default=None,
                        help="Output directory (default: alongside the input).")
    parser.add_argument("--glossary", default="glossary.ru_en.json",
                        help="Glossary JSON (relative paths resolve inside the spike dir).")
    parser.add_argument("--backend", default="hubgemini", choices=["hubgemini", "vertexai"])
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Hub model alias.")
    parser.add_argument("--refine-extraction", type=int, default=1,
                        help="Refinement passes on the source extraction.")
    parser.add_argument("--refine-translation", type=int, default=2,
                        help="Audit passes on the translation.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
