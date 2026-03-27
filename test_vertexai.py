"""Smoke-tests for the VertexAI backend.

Runs two conversions of the same PDF:
  - Test A: 0 iterations (extraction only)
  - Test B: 3 iterative refinement passes
"""

import logging
import sys
from pathlib import Path

# UTF-8 console output (avoids CP1252 issues on Windows)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Load .env before anything else
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# Make src.* importable
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s  %(name)s: %(message)s",
)

from src.backends.vertexai_backend import VertexAIBackend  # noqa: E402

PDF = Path("tmp/MakingOfAnExpert.pdf")

if not PDF.exists():
    print(f"ERROR: PDF not found at {PDF.absolute()}")
    sys.exit(1)

backend = VertexAIBackend()

print(f"\n{'='*60}")
print(f"  is_available    : {VertexAIBackend.is_available()}")
print(f"  supports_scanned: {backend.supports_scanned()}")
print(f"  PDF             : {PDF}  ({PDF.stat().st_size / 1024:.1f} KB)")
print(f"{'='*60}")


def run_test(label: str, refine_iterations: int, out_suffix: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")

    md, meta = backend.convert(PDF, refine_iterations=refine_iterations)

    print(f"\n  Markdown length  : {len(md):,} chars")
    print(f"  Input tokens     : {meta['total_input_tokens']:,}")
    print(f"  Output tokens    : {meta['total_output_tokens']:,}")
    print(f"  Total tokens     : {meta['total_tokens']:,}")
    print(f"  Model            : {meta['model']}")
    print(f"  Iterations done  : {meta['iterations_completed']}")
    print(f"  Final verdict    : {meta['final_verdict']}")

    if meta["refinement_log"]:
        print()
        print("  Iteration | Errors | Critical | Moderate | Minor | Verdict")
        print("  ----------|--------|----------|----------|-------|--------")
        for row in meta["refinement_log"]:
            print(
                f"  {row['iteration']:9} | {row['errors_found']:6} | "
                f"{row['critical']:8} | {row['moderate']:8} | "
                f"{row['minor']:5} | {row['verdict']}"
            )

    print("\n  --- First 500 chars ---")
    print(md[:500])

    out = PDF.parent / f"{PDF.stem}.vertexai{out_suffix}.md"
    out.write_text(md, encoding="utf-8")
    print(f"\n  Saved -> {out}")


run_test("TEST A: 0 iterations (extraction only)", refine_iterations=0, out_suffix="_0iter")
run_test("TEST B: 3 refinement passes",            refine_iterations=3, out_suffix="_3iter")

print(f"\n{'='*60}")
print("  All tests complete.")
print(f"{'='*60}\n")
