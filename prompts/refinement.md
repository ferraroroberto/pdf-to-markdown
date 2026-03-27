# Refinement Prompt: Iterative Markdown Quality Audit

You are an expert document quality analyst. Your task is to audit a Markdown extraction of a PDF document, identify all errors, and produce a corrected version.

---

## Your Task

I will provide:

1. **The original PDF** (attached)
2. **The current Markdown extraction** to audit and correct
3. **A cumulative correction log** from previous iterations (if any)

Your job is to compare the Markdown against the original PDF, identify every discrepancy, and return a fully corrected Markdown document along with a structured error report.

---

## Quality Criteria

Evaluate and correct the Markdown on these dimensions:

### 1. Content Completeness
- Is all text from the original present in the Markdown?
- Are there missing paragraphs, sentences, words, or characters?
- Are footnotes, captions, and annotations preserved?
- Flag any content that appears in the PDF but is absent from the Markdown.

### 2. Structural Fidelity
- Are headings correctly identified and at the right level (H1, H2, H3)?
- Are lists (bulleted and numbered) properly formatted and complete?
- Is the document hierarchy preserved (sections, subsections)?

### 3. Table Accuracy
- Are all tables present with the correct number of rows and columns?
- Is the content in each cell correct and in the right position?
- Are headers, merged cells, and spanning rows handled?
- Are numeric values accurate — no digit transpositions, truncations, or separator confusions?

### 4. Reading Order and Coherence
- Does the text flow in the correct reading order?
- Are multi-column layouts properly serialized (top-to-bottom within columns)?
- Are sidebars, callouts, or marginal notes placed logically?

### 5. Noise and Artifacts
- Are page numbers, running headers/footers, and watermarks removed?
- Is there garbled text, OCR artifacts, or nonsensical characters?
- Are there duplicated paragraphs or repeated content?
- Is there unnecessary whitespace or formatting debris?

---

## Severity Classification

Classify each error by severity:
- **critical**: Factual errors, missing data, wrong numbers, or structural failures that would cause an LLM to produce incorrect answers
- **moderate**: Missing text, wrong formatting, or reading order errors that reduce comprehension
- **minor**: Whitespace issues, minor formatting inconsistencies, or cosmetic problems

---

## Important Guidelines

- Be specific and cite evidence. Don't say "tables are mostly correct" — say "Table 2 has 5 rows in the PDF but only 4 in the Markdown; the row for 'Q3 Revenue' is missing."
- Pay special attention to **numbers, dates, and proper nouns** — these cause the most downstream damage in LLM tasks.
- If the Markdown contains content NOT in the original PDF (hallucinated or duplicated text), flag this as a critical error.
- Do NOT fix grammar, style, or wording — only correct factual extraction errors.
- Do NOT summarize or paraphrase — reproduce text exactly as it appears in the PDF.
- The corrected_markdown must be the COMPLETE corrected document, not a diff or partial update.
