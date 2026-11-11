# Tasks

- [x] Extend arc prefix rendering and verify bounded delivery/cursors.
- [x] Extend store, GitHub sync and shared closure reduction.
- [x] Expose typed fields through existing MCP and route validation.
- [x] Run focused tests, change-scoped gate and self-review.
- [x] Prepare the local commit and report delivery boundaries.
- [x] Publish to the owner's public fork after target confirmation.
- [x] Resolve all 28 failures and rerun the related backend gate.

Focused backend: 158 passed. Each feature's regression fails on the original
fork main with the expected behavioral assertion. Pure rendering of all current
arc members at 1,333 characters retains verification and open-work summaries;
no live cursor or claim was changed.

The related backend gate now passes. Its 28 earlier failures are resolved:
preview tests isolate their simulated private tree from Crew scratch; CLI tests
assert the current 300-character FACT limit and short entry IDs; real-turn tests
replace source-text assertions; the isolated environment includes the declared
pytest-split dependency. Production guards, detectors and baselines are unchanged.
The preview/classification/CLI run passes 238 tests with one skip.

Full-source mypy and flake8 pass. Changed files pass Black 26.10.1 and isort.
Comment history, brand spelling, line cap and 66 flake-ratchet tests pass.
The 471-file cross-surface frontend run exits zero; a focused 51-test frontend
run also passes. A seeded shuffled run passes all 95 changed-file backend tests.
Questions still route to the configured coordinator in compact briefs.

Public PR: https://github.com/Premshay/KiroCrew/pull/81.
Merge and deployment are held. No gateway restart or live claim changes occurred.
