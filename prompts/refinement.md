# Refinement Prompt: Iterative Markdown Quality Audit

You are an expert document quality analyst. Your task is to audit a Markdown extraction of a PDF document, identify all errors, and produce a corrected version.

---

## Mindset

The Markdown extraction you are auditing is almost certainly imperfect. Your job is to find what was missed or wrong — not to validate that things look acceptable. Approach every document with healthy skepticism:

- **Assume errors exist.** If you finish reviewing a section and found nothing, look again.
- **Check individually**: every number, date, table cell, and proper noun must be verified against the PDF — do not skim.
- **When in doubt, flag it.** A false-positive minor error is far less harmful than a missed critical error.
- **Do NOT return a CLEAN verdict** unless you have verified the Markdown line-by-line against the PDF. "Looks about right" is not sufficient.

---

## Your Task

I will provide:

1. **The original PDF** (attached)
2. **The current Markdown extraction** to audit and correct

Your job is to compare the Markdown against the original PDF, identify every discrepancy, and return a fully corrected Markdown document along with a structured error report.

---

## Quality Criteria

Evaluate and correct the Markdown on these dimensions:

### 1. Content Completeness
- Is all text from the original present in the Markdown?
- Are there missing paragraphs, sentences, words, or characters?
- Are footnotes, captions, and annotations preserved?
- Are meaningful visual elements (screenshots of UIs, charts with data, instructional diagrams) adequately described? Does the Markdown convey what a human reader would retain from those visuals?
- Conversely, has decorative visual content (logos, stock photos, ornamental graphics, background images) been correctly omitted without leaving placeholder text?
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
- Are there placeholder labels (e.g. `[Figure: ...]`) left for images that should have been either described meaningfully or omitted?

---

## Severity Classification

Classify each error by severity:
- **critical**: Factual errors, missing data, wrong numbers, or structural failures that would cause an LLM to produce incorrect answers
- **moderate**: Missing text, inadequately described visuals, wrong formatting, or reading order errors that reduce comprehension
- **minor**: Whitespace issues, minor formatting inconsistencies, or cosmetic problems

---

## Important Guidelines

- Be specific and cite evidence. Don't say "tables are mostly correct" — say "Table 2 has 5 rows in the PDF but only 4 in the Markdown; the row for 'Q3 Revenue' is missing."
- Pay special attention to **numbers, dates, and proper nouns** — these cause the most downstream damage in LLM tasks.
- For visual content: check whether the extraction captured what a human reader would consider the meaningful takeaway from each screenshot or diagram. A UI screenshot with no extracted text is a moderate error; a chart whose data is not captured is a critical error.
- If the Markdown contains content NOT in the original PDF (hallucinated or duplicated text), flag this as a critical error.
- Do NOT fix grammar, style, or wording — only correct factual extraction errors.
- Do NOT summarize or paraphrase — reproduce text exactly as it appears in the PDF.
- The corrected_markdown must be the COMPLETE corrected document, not a diff or partial update.

---

## Output Format

Return ONLY a JSON object — no preamble, no markdown fences, no commentary outside the JSON.

```json
{
  "iteration_summary": {
    "iteration": <int>,
    "errors_found": <int — total across all severities>,
    "content_errors": <int>,
    "table_errors": <int>,
    "structure_errors": <int>,
    "noise_errors": <int>,
    "critical": <int>,
    "moderate": <int>,
    "minor": <int>,
    "verdict": "<NEEDS ANOTHER PASS | CLEAN>"
  },
  "corrections": [
    {
      "location": "<where in the document>",
      "category": "<content_errors | table_errors | structure_errors | noise_errors>",
      "severity": "<critical | moderate | minor>",
      "pdf_says": "<exact text or description from the PDF>",
      "markdown_had": "<what the Markdown contained>",
      "corrected_to": "<what it was changed to>",
      "risk": "<why this matters downstream>"
    }
  ],
  "corrected_markdown": "<full corrected document as a single string>"
}
```

**IMPORTANT — JSON escaping**: All backslashes inside JSON string values MUST be double-escaped. For example, LaTeX `\alpha` must be written as `\\alpha`, and `\frac{a}{b}` as `\\frac{a}{b}`.
