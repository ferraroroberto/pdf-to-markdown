# Layout-preserving PDF translation — spike findings

Answers [issue #65](https://github.com/ferraroroberto/pdf-to-markdown/issues/65). Spike code lives in [`spikes/translation/`](../spikes/translation); it is evidence, not shipped functionality, and nothing in `src/` or `app/` imports it.

Run on 2026-07-25 against the local LLM hub.

## Recommendation, up front

**Do not build the custom pixel-fidelity pipeline (path 2).** Build path 1 into the product *if* someone actually needs translated documents; otherwise leave both as spikes.

The reasoning in one paragraph: path 2's expensive, differentiated half is not the redact-and-reinsert mechanics — those are a weekend with PyMuPDF, and `pdf2zh` already does them well. The expensive half is getting reliable per-block bounding boxes out of a **scanned** page, and `pdf2zh` supplies none of that: it has no OCR whatsoever and refuses image-only PDFs outright. So adopting it does not shortcut path 2 for the input this fleet actually has (scans, phone photos, faxed forms) — it only covers born-digital PDFs, where it is already better than a from-scratch build would be. Meanwhile path 1 handles the scanned case today, end-to-end, and needed no new orchestration to do it: the translate-and-audit core is a ~35-line transport factory handed to the loop `src/refinement.py` already owns.

| | Path 1 (structural) | `pdf2zh` (path 2, off the shelf) | Path 2 custom build |
|---|---|---|---|
| Scanned / image-only input | ✅ works | ❌ refuses, no OCR | would have to be built |
| Visual fidelity to source | ❌ content only | ✅ genuinely good | ✅ (the point) |
| Editable intermediate before render | ✅ Markdown | ❌ none | ✅ (the point) |
| Refinement loop | ✅ reuses `src/refinement.py` | ❌ one-shot | would have to be built |
| New dependencies | none | ~330 MB assets + 2nd Python | PyMuPDF (have it) + a box-aware OCR |
| Effort to production | small | moderate — 2nd interpreter + a wrapper | large, and unproven |

## What was built

| File | Role |
|---|---|
| `spikes/translation/make_sample.py` | Synthesises the test corpus — a Russian completed-works certificate, emitted born-digital **and** rasterised to an image-only PDF |
| `spikes/translation/path1_translate.py` | Path 1 end-to-end: extract → translate → audit → render |
| `spikes/translation/pdf2zh_trial.py` | Drives `pdf2zh` against the same corpus and the same hub model, and classifies its failures |
| `spikes/translation/prompts/translate.md` | Translation system prompt (glossary-mandatory, structure-preserving) |
| `spikes/translation/prompts/translate_refine.md` | Bilingual audit prompt, same JSON contract the extraction refinement loop already uses |
| `spikes/translation/glossary.ru_en.json` | 25 mandatory RU→EN terms, shared by both paths so the comparison is like-for-like |

Reproduce:

```powershell
& .\.venv\Scripts\python.exe -m spikes.translation.make_sample
& .\.venv\Scripts\python.exe -m spikes.translation.path1_translate `
    tmp/translation_spike/act_scanned.pdf --model claude_sonnet `
    --refine-extraction 1 --refine-translation 2

# pdf2zh needs its own interpreter — see "Hard blockers" below
uv venv --python 3.12 E:/tmp/pdf2zh-trial/.venv
uv pip install --python E:/tmp/pdf2zh-trial/.venv/Scripts/python.exe pdf2zh-next
& .\.venv\Scripts\python.exe -m spikes.translation.pdf2zh_trial `
    --pdf2zh E:/tmp/pdf2zh-trial/.venv/Scripts/pdf2zh_next.exe --model claude_sonnet
```

### Test corpus

The repo's existing fixtures are all English and all born-digital, so neither exercises what this spike needed to measure. The synthetic sample is a two-page Russian commercial document with a letterhead, a five-column priced table, a 22 mm fixed-width `Код: 42-А` box, two prose sections, a signature block and a round vector stamp. It is emitted twice:

- `act_digital.pdf` — real text layer. Best case for anything that reads PDF text objects.
- `act_scanned.pdf` — the same pages rasterised at 200 DPI, **0 characters of text layer** (asserted at generation time). This is the case the fleet actually has.

The narrow `Код: 42-А` box is deliberate: it is the cheapest possible instance of the length-expansion risk issue #65 names, because any layout-preserving reinsertion has to fit the translated string into that exact rectangle.

### A note on the model used

The spike ran on `claude_sonnet`, not `gemini_pro`. On the day, the hub's Gemini route (`agy`) returned `502 … model 'Gemini 3.1 Pro (High)' is not offered by agy` for every Gemini alias. That is an environment condition, not a repo defect, and it does not affect the conclusions — both are hub aliases behind the same Anthropic-shape transport, and `claude_sonnet` demonstrably OCRs the image-only PDF correctly.

## Path 1 — structural fidelity

Ran end-to-end **on the scanned sample**, so every number below came out of an image, not a text layer. Two runs of the identical command are shown, because the difference between them is the point.

| Measure | Run A | Run B |
|---|---|---|
| Wall clock, extract → render | 262.5 s | 314.2 s |
| Extraction | 51.0 s, 1 pass, `CLEAN` | 50.5 s, 1 pass, `CLEAN` |
| Translation | 25.8 s | 23.8 s |
| Translation audit | 1 pass, `CLEAN`, 0 corrections | 2 passes — `NEEDS_WORK` (1 moderate) → `CLEAN` |
| Source Markdown | 4,331 chars | 3,498 chars |
| Translated Markdown | 4,559 chars (+5.3 %) | 3,702 chars (+5.8 %) |
| Output tokens | ~17,500 | ~20,700 |

Content fidelity was clean in both: all five table rows, every figure in them, and all three totals survived with identical values; the glossary was applied; identifiers (`ЩР-24`, the IBAN/BIC/account numbers, `41/СМ`) were correctly left alone; the round stamp arrived as `*[Stamp: STROYMONTAZH PLUS, OGRN … 1157746321098]*`.

### The audit loop earns its keep

Run B's audit caught this and fixed it:

> **H1 heading — company name**, moderate. The lowercase `м` in `Stroyмontazh` is a Cyrillic character (U+043C) rather than a Latin `m`, producing a mixed-script company name — a copy-paste artefact from the source that was not replaced during transliteration.
> `# Stroyмontazh Plus LLC` → `# Stroymontazh Plus LLC`

That is worth dwelling on. It is invisible to a human proof-reader, it is exactly the defect class `pdf2zh` produced independently (half-transliterated seal text, `Kovalyov`/`Kovalev` drift), and it is the class `pdf2zh` structurally cannot catch, because there is no intermediate to audit. Both tools are non-deterministic — path 1's own extraction varied by 19 % in length between runs — but only one of them has a mechanism that converges.

### The finding that actually matters

**Translate-with-refinement needed no new orchestration.** `src/refinement.py::run_conversion` is already transport-agnostic — it asks a backend only for an `extract() -> (text, usage)` and a `refine(user_message) -> (text, usage)`. Handing it a transport whose "extraction" is a translation and whose "refinement" is a bilingual audit works unchanged, and the whole loop comes along for free: per-step token accounting, the `refinement_log` track record, the `CLEAN` early stop, the diminishing-returns stop, and the verbose `raw_step_NN` artifacts. Issue #65 guessed this was "the right shape to reuse"; it is stronger than that — it is a transport swap, not a fork.

Most of `path1_translate.py` is therefore *not* the interesting part: it is argument parsing, glossary loading, and the Markdown → HTML → PDF renderer.

### Where path 1 falls down

The rendered output is content-complete and readable but is visibly **not** the source document:

- Two source pages collapse to one rendered page. Pagination is a function of the rendered content, not of the source.
- The `Код: 42-А` box loses its box — extraction inlines it into the preceding sentence, so nothing downstream knows it was a fixed-width field.
- The stamp becomes an italic text description. Correct for RAG, wrong for anything a human is meant to recognise as a sealed document.
- Signature-block alignment collapses. Extraction encodes horizontal spacing as `&nbsp;` runs, which the renderer folds.

### Two real defects worth recording

1. **`ООО` was left untranslated** in run A's letterhead and party block, while `АО` in the next line became `AO`. The glossary had no entry for either legal form, so the model chose inconsistently across two adjacent lines. (Run B transliterated both, which is the non-determinism again.)
2. **Run A's audit returned `CLEAN` anyway** — zero corrections, with that inconsistency sitting in the document. So the refinement loop is not a safety net for an incomplete glossary; it audits against the contract it was given, and legal-form abbreviations were not in it. A production version needs the glossary treated as part of the contract, and — see below — hashed into the execution-log row.

### One thing a production version must fix

`run_conversion` hashes the prompt **file**, so the recorded `extraction_prompt_hash` is the hash of the *template*, taken before `{{TARGET_LANGUAGE}}` and `{{GLOSSARY}}` are substituted. Two runs with different glossaries share a prompt hash and are indistinguishable in `tmp/exec_log.jsonl`. Fine for a spike, not fine for a feature — the glossary would need its own hash column.

Separately, the spike's renderer embeds Arial, which covers Latin, Cyrillic and Greek but **not CJK**. Rendering a Chinese or Japanese target would need a Noto/Source Han font. Not a design problem, just an unfinished edge.

## `pdf2zh` trial

Tested `pdf2zh-next` 2.9.0 (BabelDOC 0.6.2) — the maintained successor to PDFMathTranslate v1 — pointed at this project's own hub via its OpenAI-compatible endpoint (`--openai-base-url http://127.0.0.1:8000/v1 --openai-model claude_sonnet`), with the same 25-term glossary converted to BabelDOC's `source,target,tgt_lng` CSV.

| Case | Result | Time |
|---|---|---|
| Born-digital | ✅ 2-page `mono` + 2-page `dual` PDF, 65 paragraphs translated, 4,411 tokens | 91.5 s |
| Scanned, as any user would run it | ❌ `Scanned PDF detected.` — refused | 24.0 s |
| Scanned, with `--skip-scanned-detection --ocr-workaround` | ❌ `The document contains no paragraphs.` | 26.2 s |

All three exit `0`. The trial harness classifies them from the log and asserts on produced files (`tmp/translation_spike/pdf2zh/pdf2zh_trial_report.json`).

### On born-digital input it is genuinely good

Page count, the table grid, the stamp's vector art, the letterhead rule and the narrow `Code: 42-A` box all survive, and the letterhead auto-shrinks to fit its expanded translation. This is a better result than a from-scratch redact-and-reinsert build would reach quickly, and it validates the mechanics issue #65 sketched for path 2.

### But the defects are the kind that matter in a commercial document

- **Three number formats inside one money column.** The Amount column came out as `30,000.00`, `85,440.00`, `50 220.00`, `18 900.00`, `27 400,00` — UK convention, then a space-thousands/dot-decimal hybrid that is neither convention, then the Russian original. Values are all correct; the formatting is not. Three decimal-separator conventions in one column of an invoice is a correctness defect, not a cosmetic one.
- **…and it is not deterministic.** Two runs of the identical command produced *different* format splits across the same column, and transliterated the same director's name as `Kovalyov` in one run and `Kovalev` in the other. Because each paragraph is translated as an independent call with no document-level consistency pass, per-run variation is structural, not a fluke.
- **Cell overflow.** `162 м` wrapped so that the unit sits alone on a second line inside the cell, and row 5's description overflows its row height. The `м²` superscript survived in one run and was flattened to `m2` in another — same non-determinism.
- **Glossary applied inconsistently.** The right-hand `М.П.` became `L.S. (seal)`; the identical left-hand `М.П.` stayed Cyrillic.
- **Half-translated stamp.** `СТРОЙМОНТАЖ` → `STROYMONTAZH`, but `ПЛЮС` and `ОГРН` stayed Cyrillic — mixed script inside a single seal.
- **Title glossary term missed.** `Акт выполненных работ` is a glossary entry, but the source splits it over two lines, so it rendered as `ACT No 2026-0417 OF COMPLETED WORKS` rather than the mandated term.
- **An advertising banner is injected into the output** — a Chinese-language BabelDOC promo line at the top of page 1. There is a `--watermark-output-mode` flag, but it is on by default.

Apart from the cell overflow, every one of these is a *translation-quality* defect rather than a layout defect — which is the interesting part. They are exactly what a refinement pass over an editable intermediate would catch, and exactly what `pdf2zh` has no way to express. That confirms issue #65's premise about off-the-shelf tools: the layout problem is the one they solved, and the reviewability problem is the one they did not.

### Hard blockers

- **No OCR, at all.** This is the decisive one. It refuses image-only PDFs by policy, and forcing past that guard proves the policy is honest: there is nothing to translate. The fleet's real input is unreachable.
- **No editable intermediate and no refinement loop.** It exposes `--glossaries` and `--custom-system-prompt`, but the run is one-shot: prompt in, final PDF out. Nothing to review or correct before render.
- **Exits `0` on a failed document**, producing no output file. Any wrapper must assert on produced artifacts, never on the return code — the trial harness does.
- **Needs a second Python interpreter.** `pdf2zh-next` requires `<3.14`, `pdf2zh` v1 requires `<3.13`; this repo runs 3.14.3. Adopting it means shipping and maintaining a separate venv.
- **~330 MB of downloaded assets** on first run (a 75 MB DocLayout-YOLO ONNX model plus the full CJK font set), on top of an onnxruntime-class dependency tree.
- **Deadlocks when its stdout is a pipe.** `subprocess.run(capture_output=True)` hangs it indefinitely at 0 s of CPU — its `rich` live-progress display never drains. It looks exactly like a hung translation. The trial harness redirects to a file instead; anyone wrapping this tool will hit the same wall.

## If pixel fidelity is later required

The follow-up issue should **not** be "build path 2 as scoped in #65". Path 2 as scoped assumes the bounding boxes are available; this spike shows that for scanned input they are the whole problem, and it did not test whether they can be obtained.

The right first question for a follow-up spike, in order:

1. **Can the vision model return reliable per-block bounding boxes for a scanned page?** Untested here. Everything downstream depends on it. If the answer is no, path 2 dies for scanned input regardless of how good the redact/insert code is, and the alternative is a dedicated layout OCR (Azure Document Intelligence Layout, Docling) — which is a new external dependency and, for Azure, a new credential and a data-residency question.
2. Only if (1) succeeds: prototype redact + reinsert with PyMuPDF `add_redact_annot` / `insert_textbox` on the `Код: 42-А` box specifically, with a font-shrink-with-floor strategy, and measure how far a translation can expand before it becomes illegible.
3. Only if (2) succeeds: decide whether the born-digital path should call `pdf2zh` rather than duplicate it — behind a wrapper that asserts on produced files, disables the watermark, and adds the refinement pass it lacks.

Note that (1) and (2) are independently useful and independently shippable, so they should be separate issues rather than one "build path 2".

## Not covered

Stated plainly so nobody assumes otherwise:

- Only one language pair (RU → EN) and one document, synthetic. No CJK target was rendered; no right-to-left script was tried.
- Two runs per tool is enough to demonstrate non-determinism and nowhere near enough to characterise it. Nothing here should be read as a rate.
- The EN→RU direction — where translated text genuinely *grows* and overflow bites hardest — was not measured. RU→EN grew 5 % here, which is the mild direction.
- No cost figures. The hub's `claude_sonnet` path does not surface input-token counts (`total_input_tokens` reads 6 for a run that clearly consumed thousands), so the `$` comparison in issue #65's framing cannot be made from this data.
- `pdf2zh` was run with default typesetting flags. Several of the wrapping defects may be tunable via `--split-short-lines`, `--primary-font-family` or `--disable-rich-text-translate`; that was not explored.
- No throughput or concurrency testing, and no multi-hundred-page document.
