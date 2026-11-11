# Tasks

- [x] Extend arc prefix rendering and verify bounded delivery/cursors.
- [x] Extend store, GitHub sync and shared closure reduction.
- [x] Expose typed fields through existing MCP and route validation.
- [x] Run focused tests, change-scoped gate and self-review.
- [x] Prepare the local commit and report delivery boundaries.
- [ ] Publish to the owner's public fork after target confirmation.

Focused backend: 158 passed. Each feature's regression fails on the original
fork main with the expected behavioral assertion. Pure rendering of all current
arc members at 1,333 characters retains verification and open-work summaries;
no live cursor or claim was changed.

The related backend gate found 28 baseline failures: two CLI expectations,
24 preview/project classification checks, one source-read budget and one shard
check missing pytest-split. All reproduce on unchanged fork main. No baseline
or detector was changed. The final maximum-entry fixture passes in the focused run.

Full-source mypy and flake8 pass. Changed files pass Black 26.10.1 and isort.
Comment history, brand spelling, line cap and 66 flake-ratchet tests pass.
The 471-file cross-surface frontend run exits zero; a focused 51-test frontend
run also passes. A seeded shuffled run passes all 95 changed-file backend tests.
Questions still route to the configured coordinator in compact briefs.

Public-fork publication needs explicit target confirmation. Deployment, restart,
and retiring the four live claims are not authorized by this implementation task.
