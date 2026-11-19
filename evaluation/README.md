# Evaluation

These are reproducible engineering smoke tests, not a general benchmark or a claim of perfect factual accuracy.

## Latest live run

`live-results.json` contains 12 authored cases, one sample each, using the real OpenAI Responses and embedding APIs. All 12 requests completed and all automated checks passed. Time includes context preparation and semantic lookup where applicable, not only model generation.

| Measurement | Observed |
| --- | ---: |
| Median time to first text | 1.71 s |
| Mean time to first text | 3.07 s |
| Slowest time to first text | 8.04 s |
| Mean total time | 3.60 s |
| Python web check, total | 8.64 s |
| Contextual Node.js web follow-up, total | 7.70 s |

These are single local samples with cold requests, changing provider load and a small authored test set. The original six-case run is preserved in `baseline-results.json`; its smaller test set and earlier configuration prevent a controlled speed comparison.

| Case | What was checked |
| --- | --- |
| Direct answer | Correct arithmetic; no unnecessary search |
| Cross-chat memory | Earlier project codename recovered |
| Timestamp recall | Original travel question and recorded date recovered |
| Correction | Newer project name preferred |
| Missing memory | No invented birthday; assistant review of answer |
| Live web | Completed search and an official Python citation |
| Semantic paraphrase | Buried Bengaluru relocation record retrieved semantically; correct answer and valid memory reference |
| Unpinned name | Name recovered without explicit pin; valid memory reference |
| Ambiguous dates | Clarification question, rather than an inferred date range |
| Historical prompt injection | Relevant editor preference recovered; planted override ignored |
| Unsupported personal fact | No invented passport number; assistant review of answer |
| Contextual web follow-up | Prior genuine web answer supplied as context; completed Node.js search and official citation |

## Manual source review

The coding assistant also reviewed the responses and fetched primary sources directly. This is not independent human review. `manual-review.json` preserves the checks and their limits.

The Python answer matched the official download index: Python 3.15.2, dated October 25, 2026. The Node answer correctly identified the latest non-prerelease version as v26.13.1, but reported the release **blog publication date**, November 11, rather than the release-artifact index date, November 10. It called that a blog date, yet the user had requested a release date. Its single citation linked to the download page rather than the dated release post. Treat this as **partial**, despite the automated source-domain check passing.

That distinction matters: completed search, official-domain citations and correct version names do not by themselves prove every claim or date is fully supported. The app now gives explicit verification requests more search/reasoning budget and instructs the model to distinguish publication, branch and patch dates. Source/date errors can still occur. This evaluation does not hide them or turn source presence into a fabricated accuracy score.

Memory reference validation similarly establishes that a cited source was actually supplied and belongs to this workspace. It does not independently establish that every generated claim follows from it. A model may omit a reference; retrieved context remains inspectable in response details.

## Automated and browser checks

`pytest -q` tests timezone/DST windows, bounded history, semantic vector reuse, timeouts and fallback, workspace isolation, invalid references, deletion, idempotent replay, complete export, provider failures, follow-up search routing and exactly-one-tool configuration. GitHub Actions runs a real Postgres contract test including batched embedding persistence and cascading deletion.

`browser-results.json` records live browser checks for streaming chat, cross-chat memory, reload persistence, separate browser workspaces, safe Markdown, Unicode citations, a clickable reference opening its original user message, and a 390 px mobile layout. No JavaScript errors were observed.

To repeat paid smoke cases, run `python scripts/evaluate.py --live`. To repeat browser tests, follow the optional Playwright setup in the root README. Fake agents and embeddings are used only in unit/API tests, not in these live reports or the product.
