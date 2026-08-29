# Knowledge Library Governance — Plan

**Status:** implemented; review pending

**Date:** 2026-08-29
**User story:** As an operator, I can measure retrieval against my own cited
documents, scope project searches without cross-project leakage, and recover
from an interrupted ingestion without manual database repair.

## Problem

The Knowledge Library had three operational gaps:

1. A source-scoped search filtered keyword and vector seeds, but graph-derived
   results could still return an item owned by another source.
2. A source ingestion interrupted after recording `processing` could remain
   there indefinitely, preventing the normal source lifecycle from converging.
3. Retrieval changes had no stable operator-private intrinsic benchmark. Ad hoc
   searches cannot distinguish an improvement from a ranking regression.

## Goal

Deliver a small governance layer around the existing local retrieval system:

- scope every returned retrieval leg to the requested source while retaining
  cross-source graph traversal as ranking context;
- abandon only stale, source-owned ordinary ingestion jobs on watcher sweeps;
- evaluate production hybrid retrieval against private, source-anchored golden
  cases and write private reports.

## Constraints

- The golden set and reports identify private operator sources, so they stay
  under `$KIROCREW_HOME/workspace/knowledge/evals/`, never in this repository.
- Evaluation must use the same embedding-capable retrieval mode as a baseline;
  it must fail rather than silently compare FTS-only results.
- Reconciliation cannot interfere with corpus-wide embedding rebuilds, whose
  separate single-flight lease owns their lifecycle.
- This slice does not change live retrieval ranking, prompt injection policy,
  or the dashboard UI.

## Delivery plan

1. Correct source membership at the graph-result boundary and cover both
   ordinary ownership and deduplicated source locations.
2. Reconcile stale ordinary source jobs off the event loop before each watcher
   scan; exclude unowned rebuild jobs.
3. Add a versioned private-fixture evaluator and CLI command with stable
   document locators, scope-leak reporting, abstention measurement, and no
   chunk text in reports.
4. Document the public contract and operator procedure.
5. Seed the operator-private fixture separately with Nexus, harness, and Atlas
   cases across frontend, backend, design-system, standards, and security work.

## Success criteria

- A source-scoped graph query never returns an item outside the requested source.
- A stale source job becomes `abandoned`; fresh jobs and rebuild jobs remain
  `processing`.
- `kirocrew knowledge evaluate` rejects a missing source, malformed fixture,
  out-of-tree fixture/report path, and unavailable embedding backend.
- The evaluator reports hit@k, recall@k, MRR, nDCG, abstention accuracy,
  source-scope leaks, latency, corpus shape, and embedding signature without
  copying indexed chunk text into the report.

## Out of scope

- Automatic per-turn knowledge retrieval or prompt injection.
- A task-lift A/B harness, reranker, recency weighting, or content TTL policy.
- An automatic release gate for an operator-private corpus.
- Automatic recovery of jobs carrying live provider work within the two-hour
  ordinary-ingestion deadline.
