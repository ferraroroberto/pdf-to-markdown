"""Path 2 reconnaissance for the #65 spike — drive PDFMathTranslate (``pdf2zh``).

Issue #65's first concrete step is "trial the existing open-source tool before
writing any custom redact/insert code". This script is that trial, made
repeatable: it points ``pdf2zh`` at *this project's* LLM hub (so the comparison
against Path 1 is like-for-like on the model), feeds it the same glossary Path 1
uses, and runs three cases — the born-digital sample, the scanned sample as any
user would run it, and the scanned sample with ``pdf2zh``'s own escape hatches
engaged, so the write-up can tell "refuses by policy" apart from "cannot do it".

``pdf2zh`` exits ``0`` on a failed document and produces no file, and its
``rich`` progress display deadlocks if stdout is a pipe. Both are handled here;
both bite anyone who wraps this tool naively.

``pdf2zh`` cannot be installed into this project's virtualenv — the repo runs
Python 3.14 and ``pdf2zh-next`` caps at ``<3.14`` (``pdf2zh`` v1 caps at
``<3.13``). That is a finding in its own right, not an obstacle to work around:
adopting it means adopting a second interpreter. The trial therefore runs
against a throwaway virtualenv created outside the repo::

    uv venv --python 3.12 E:/tmp/pdf2zh-trial/.venv
    uv pip install --python E:/tmp/pdf2zh-trial/.venv/Scripts/python.exe pdf2zh-next

Then, from the project root::

    & .\\.venv\\Scripts\\python.exe -m spikes.translation.pdf2zh_trial \\
        --pdf2zh E:/tmp/pdf2zh-trial/.venv/Scripts/pdf2zh_next.exe

Nothing here is imported by ``src/`` — it shells out and reports.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("pdf2zh_trial")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPIKE_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = PROJECT_ROOT / "tmp" / "translation_spike"
OUT_DIR = SAMPLE_DIR / "pdf2zh"

HUB_OPENAI_BASE = "http://127.0.0.1:8000/v1"
HUB_API_KEY = "local-dummy"

# Windows: keep every spawned child console-less (fleet convention).
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def glossary_to_csv(glossary_json: Path, dest: Path) -> int:
    """Write the spike glossary out in babeldoc's ``source,target,tgt_lng`` CSV shape.

    ``tgt_lng`` is left empty so the entry applies to whatever ``--lang-out`` is
    requested — babeldoc only filters when the column is non-blank.
    """
    data = json.loads(glossary_json.read_text(encoding="utf-8"))
    terms: dict[str, str] = data.get("terms", {})
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["source", "target", "tgt_lng"])
        writer.writeheader()
        for source, target in terms.items():
            writer.writerow({"source": source, "target": target, "tgt_lng": ""})
    logger.info("ℹ️ Glossary CSV → %s (%d terms)", dest, len(terms))
    return len(terms)


# pdf2zh renders its log through ``rich``, which hard-wraps mid-word inside a
# fixed-width column ("Err\nor details:"), so the log cannot be parsed line by
# line. It also exits **0** on a failed document. Classification is therefore by
# substring signature against a whitespace-collapsed log, which is a heuristic —
# an unrecognised failure shows up as ``unclassified-error``, never as success.
_FAILURE_SIGNATURES: dict[str, str] = {
    "Scanned PDF detected": "scanned-pdf-refused",
    "document contains no paragraphs": "no-text-layer-to-translate",
    "Connection error": "translation-backend-unreachable",
}


def classify_failures(log_text: str) -> list[str]:
    """Return the failure signatures present in *log_text*, most specific first."""
    collapsed = " ".join(log_text.split())
    found = [tag for needle, tag in _FAILURE_SIGNATURES.items() if needle in collapsed]
    if not found and "ERROR" in collapsed:
        found = ["unclassified-error"]
    return found


def describe_pdf(path: Path) -> dict:
    """Return a small fingerprint of *path* — pages, text-layer size, image count."""
    doc = fitz.open(str(path))
    try:
        chars = sum(len(p.get_text().strip()) for p in doc)
        images = sum(len(p.get_images(full=True)) for p in doc)
        return {
            "pages": doc.page_count,
            "text_layer_chars": chars,
            "images": images,
            "bytes": path.stat().st_size,
        }
    finally:
        doc.close()


def run_pdf2zh(
    *,
    exe: Path,
    pdf: Path,
    out_dir: Path,
    glossary_csv: Path,
    model: str,
    lang_in: str,
    lang_out: str,
    qps: int,
    extra_args: list[str],
    timeout_s: int,
    label: str,
) -> dict:
    """Invoke pdf2zh once and return a structured result record.

    Output is redirected to a **file**, never to a pipe. ``pdf2zh`` drives a
    ``rich`` live-progress display; with ``capture_output=True`` it deadlocks
    before doing any work at all (0 s of CPU, indefinitely). That looks exactly
    like a hung translation and is purely an artifact of the harness — worth
    knowing if you ever wrap this tool in your own subprocess call.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in out_dir.glob("*.pdf")}
    log_path = out_dir / f"{label}.log"

    cmd = [
        str(exe), str(pdf),
        "--lang-in", lang_in,
        "--lang-out", lang_out,
        "--output", str(out_dir),
        "--openai",
        "--openai-base-url", HUB_OPENAI_BASE,
        "--openai-api-key", HUB_API_KEY,
        "--openai-model", model,
        "--qps", str(qps),
        "--pool-max-workers", str(qps),
        "--no-auto-extract-glossary",
        "--glossaries", str(glossary_csv),
        "--ignore-cache",
        *extra_args,
    ]
    logger.info("ℹ️ Running pdf2zh — %s (%s) …", label, pdf.name)
    logger.debug("cmd: %s", " ".join(cmd))

    started = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write(" ".join(cmd) + "\n\n")
        log.flush()
        try:
            proc = subprocess.run(
                cmd, stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout_s, creationflags=_NO_WINDOW,
            )
            returncode, timed_out = proc.returncode, False
        except subprocess.TimeoutExpired:
            returncode, timed_out = -1, True

    elapsed = round(time.time() - started, 1)
    produced = sorted({p.name for p in out_dir.glob("*.pdf")} - before)
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    errors = classify_failures(log_text)

    record = {
        "label": label,
        "input": pdf.name,
        "input_fingerprint": describe_pdf(pdf),
        "extra_args": extra_args,
        "returncode": returncode,
        "timed_out": timed_out,
        "elapsed_s": elapsed,
        "produced": produced,
        "produced_fingerprints": {
            name: describe_pdf(out_dir / name) for name in produced
        },
        "errors": errors,
        "log": str(log_path),
    }
    status = "✅" if returncode == 0 and produced else "❌"
    logger.info(
        "%s %s — rc=%s, %.1fs, produced=%s%s",
        status, label, returncode, elapsed, produced or "nothing",
        f", errors={errors}" if errors else "",
    )
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trial pdf2zh for issue #65.")
    parser.add_argument("--pdf2zh", required=True, help="Path to the pdf2zh_next executable.")
    parser.add_argument("--model", default="claude_sonnet", help="Hub model alias.")
    parser.add_argument("--lang-in", default="ru")
    parser.add_argument("--lang-out", default="en")
    parser.add_argument("--qps", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=1800, help="Per-document timeout (s).")
    parser.add_argument("--glossary", default="glossary.ru_en.json")
    args = parser.parse_args(argv)

    exe = Path(args.pdf2zh)
    if not exe.exists():
        logger.error("❌ pdf2zh executable not found: %s", exe)
        return 2

    glossary_json = Path(args.glossary)
    if not glossary_json.is_absolute():
        glossary_json = SPIKE_DIR / glossary_json
    glossary_csv = OUT_DIR / "glossary.csv"
    term_count = glossary_to_csv(glossary_json, glossary_csv)

    # Three cases. The scanned document is run twice on purpose: once as any
    # user would run it, and once with pdf2zh's own escape hatches engaged, so
    # the write-up can distinguish "refuses by policy" from "cannot do it".
    cases: list[tuple[str, str, list[str]]] = [
        ("digital", "act_digital.pdf", []),
        ("scanned", "act_scanned.pdf", []),
        ("scanned_forced", "act_scanned.pdf",
         ["--skip-scanned-detection", "--ocr-workaround"]),
    ]

    results = []
    for label, name, extra in cases:
        pdf = SAMPLE_DIR / name
        if not pdf.exists():
            logger.error("❌ Sample not found: %s — run make_sample first.", pdf)
            return 2
        results.append(run_pdf2zh(
            exe=exe, pdf=pdf, out_dir=OUT_DIR / label,
            glossary_csv=glossary_csv, model=args.model,
            lang_in=args.lang_in, lang_out=args.lang_out,
            qps=args.qps, extra_args=extra, timeout_s=args.timeout, label=label,
        ))

    report = {
        "tool": "pdf2zh-next",
        "executable": str(exe),
        "model": args.model,
        "hub_base_url": HUB_OPENAI_BASE,
        "glossary_terms": term_count,
        "results": results,
    }
    report_path = OUT_DIR / "pdf2zh_trial_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("ℹ️ Report → %s", report_path)
    # A non-zero exit on the scanned cases is the expected, informative result —
    # the trial itself succeeded as long as the born-digital case worked.
    digital = next(r for r in results if r["label"] == "digital")
    return 0 if digital["returncode"] == 0 and digital["produced"] else 1


if __name__ == "__main__":
    sys.exit(main())
