# spikes/

Throwaway-by-design evaluation code. Scripts here exist to answer a question asked by a GitHub issue — "is X feasible / worth building?" — and to make that answer reproducible by whoever reads the resulting write-up. They are **not** part of the shipped pipeline.

Rules for anything under `spikes/`:

- Nothing in `src/`, `app/`, or `testing/` may import from `spikes/`. The dependency arrow only ever points inward.
- No new entries in `requirements.txt`. A spike uses what the project already installs — including transitive dependencies, which is a smell a spike may accept and a feature may not — or an explicitly throwaway virtualenv created outside the repo, documented in the spike's write-up.
- Every spike names the issue it answers and the `docs/` write-up that records the conclusion. When the write-up says "don't build this", the spike stays as the evidence; when it says "build it", the production version lands in `src/` and the spike is deleted.
- Outputs go to `tmp/` (git-ignored). Nothing a spike generates is committed.

| Spike | Issue | Write-up |
|---|---|---|
| `translation/` | [#65](https://github.com/ferraroroberto/pdf-to-markdown/issues/65) — layout-preserving PDF translation with a refinement loop | [`docs/translation-spike.md`](../docs/translation-spike.md) |
