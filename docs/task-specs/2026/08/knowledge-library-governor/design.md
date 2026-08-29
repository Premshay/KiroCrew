# Knowledge Library Governance — Design

**Status:** implemented; review pending

**Date:** 2026-08-29

## Decision

Keep the Knowledge Library local and pull-based, but give it three explicit
governance boundaries: returned source scope, bounded ingestion recovery, and
private reproducible measurement.

## Source scope

`HybridRetriever` continues to use keyword, graph, and vector retrieval, then
fuses their candidates with reciprocal-rank fusion. A requested `source_id`
now filters the candidate items from every leg. Graph traversal may traverse
entities connected through another source, but only an item held by the
requested source may enter the result set.

Source membership is deliberately the existing identity rule:

```
item.source_id == requested_source
OR source_locations contains (item, requested_source)
```

The second branch preserves a source's discoverability after cross-source
deduplication assigns physical ownership to another source.

## Ingestion recovery

`KnowledgeWatcher._scan()` delegates ordinary-job reconciliation to a worker
thread before discovery and source scans. The reaper updates a job only when:

- `source_id` is present;
- status is `processing`; and
- `updated_at` is at least two hours old.

It records `abandoned` plus an attributable error string. Corpus-wide embedding
rebuild jobs have no `source_id` and are excluded, preserving their existing
single-flight lease and cancellation semantics. The watcher logs a failed
reconciliation rather than failing the whole scan.

## Private golden evaluation

The evaluator is source code; fixtures and reports are private data:

```
$KIROCREW_HOME/workspace/knowledge/evals/
├── golden-v1.json
└── report-<timestamp>.json
```

Each versioned fixture case supplies a natural-language query, category, and
one or more expected locators. A locator names a registered source URI and at
least one stable file path, section title, or excerpt anchor. Item IDs are not
accepted because re-chunking changes them. An empty expected list is an
abstention assertion. A scoped case may additionally name its intended source.

`kirocrew knowledge evaluate` loads the fixture, verifies source registration,
requires the configured embedding backend, invokes production
`HybridRetriever`, and atomically writes a private report. The report records
identifiers, locations, scores, aggregate metrics, and the embedding signature;
it omits chunk bodies so reporting cannot create another corpus copy.

The evaluator treats a source-held deduplicated item as a valid expected hit
even if a different source owns the surviving physical item. This matches the
retrieval source-membership contract above.

## Rejected alternatives

- **Filter only keyword/vector seeds:** retains a source-scope leak through
  graph results.
- **Delete stale jobs:** loses the failure state an operator needs to diagnose;
  `abandoned` is truthful and recoverable.
- **Run evaluation with FTS fallback:** produces an incomparable baseline when
  embeddings are temporarily unavailable.
- **Commit the fixture:** exposes private source topology and document anchors.
- **Inject search results into every prompt:** spends context on irrelevant
  material and changes the pull-based Knowledge Library contract.

## Operational follow-up

The initial private fixture now includes Atlas frontend, backend, design-system,
standards/crosswalk, GDPR, and security contracts. It is a seed measurement,
not a release gate. Establish a baseline only when the shared embedding backend
is ready, then compare future reports using the same fixture, corpus, score
floor, and embedding signature.
