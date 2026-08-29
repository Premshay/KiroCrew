# Knowledge Library Governance — Tasks

**Status:** implemented; review pending

**Date:** 2026-08-29

## Completed

- [x] Define the private-evaluation contract in the Knowledge module spec and
  operator documentation.
- [x] Add source-scoped graph-result filtering, including source-location
  membership after cross-source deduplication.
- [x] Add watcher-triggered reconciliation for stale ordinary source ingestion
  jobs while excluding embedding rebuild jobs.
- [x] Add a private golden-set loader, locator validation, aggregate metrics,
  private atomic report writer, and CLI entry point.
- [x] Reject an unavailable embedding backend so reports remain comparable to a
  hybrid baseline.
- [x] Cover source filtering, stale-job recovery, fixture validation, path
  containment, deduplicated locations, scope leakage, and abstention behavior.
- [x] Seed the operator-private fixture with Atlas frontend, backend,
  design-system, standards/crosswalk, GDPR, and security cases.

## Verification completed

- [x] Validate the 23-case private fixture and verify every excerpt anchor is
  present in its registered source.
- [x] Run `test/test_knowledge_evaluation.py` (5 passed).
- [x] Run the focused Knowledge Library suite (286 passed).
- [x] Run compilation, documentation lint, changed-file formatting, and diff
  whitespace checks.

## Follow-up work

- [ ] Establish the first hybrid baseline after the embedding backend is ready.
- [ ] Add independently verified time-bound, contradiction, retraction,
  reinforcement, and multi-hop cases before tuning retrieval.
- [ ] Design a controlled knowledge-on/knowledge-off task-lift experiment.
- [ ] Add truthful retrieval-adoption telemetry before claiming how often agents
  use the library in ordinary work.
