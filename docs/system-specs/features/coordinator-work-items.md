# Coordinator Work Items

Status: implemented

Coordinator work items are product-owned, bounded outcomes for one persistent
coordinator session. They replace Goal Conductor's former convention of placing
JSON strings under `item-N` keys in the per-session work ledger. The work ledger
continues to hold the coordinator's high-level goal, phase, and resumable next
step; it is not a work-item database.

## Authority and lifecycle

The coordinator is the authenticated calling session. Its key is derived from
the gateway-vetted `X-Session-Key` and normalized only by the lossless
dashboard-prefix fold used by `session_ledger`. A request never names a target
session, data-home path, assignee, worker, or provider identity.

Each coordinator has one store under the resolved KiroCrew data home:

```
<data_home>/work-items/<safe-key-plus-digest>/
    coordinator_key
    state.json
    archive/<cycle-id>.json
    .lock
```

`state.json` contains at most one active cycle and uses atomic whole-document
replacement under a bounded per-store lock. A malformed or oversized existing
record is a visible `work_item_store_corrupt` failure; unlike the small resume
ledger, it is never treated as an empty store. A missing record is the only
empty state.

Closing a dashboard tab preserves active cycles and archives. Permanent history
deletion cancels/destroys the session first, then purges matching work-item
stores by exact key and breadcrumb fold alongside the session ledger.

## Data and state machine

A cycle has an opaque `wc_…` ID, a goal, next action, timestamps, and at most
64 opaque `wi_…` items. An item has an immutable typed acceptance condition,
optional artifact reference/resources, a concrete next action, a bounded event
tail, bounded evaluator evidence, and timestamps.

The only states are `proposed`, `waiting`, `accepted`, `rejected`, and
`cancelled`. `accepted`, `rejected`, and `cancelled` are terminal. The
coordinator can transition an open item only to `proposed`, `waiting`,
`rejected`, or `cancelled`, with an event. It cannot write `accepted` directly:
only a recorded product evaluator `pass` makes that transition. A failing,
pending, refused, or unavailable evaluation remains non-terminal with its
evidence.

Acceptance is a closed product vocabulary:

- `pr_checks` — non-empty repository and positive PR number;
- `file` — path and expected existence;
- `human_approval` — explicitly remains pending in this slice.

There is no command, argv, shell, working-directory, or agent-supplied
executable field. `work_acceptance` builds any fixed `gh pr checks` invocation
itself.

## Product API

`mcp_tools/work_items.py` advertises and handles these stateless
`kirocrew-core` tools:

| Tool | Effect |
| --- | --- |
| `work_cycle_open` | Create the one active cycle. |
| `work_item_create` | Add a proposed item with immutable typed acceptance. |
| `work_item_list` / `work_item_read` | Read the active cycle/items and migration state. |
| `work_item_update` | Change only mutable reference/resources/next-action/event fields. |
| `work_item_transition` | Perform the restricted coordinator state transition. |
| `work_item_evaluate` | Record product-owned typed evaluator verdicts. |
| `work_cycle_close` | Archive an all-terminal cycle and clear active state. |
| `work_cycle_archive_list` / `work_cycle_archive_read` | Read immutable closed-cycle archives. |

Every MCP handler resolves strict session identity before forwarding the exact
verified key to the gateway's strict-internal `/api/work-items` routes. Those
routes reuse the ledger's recognized-session and restricted-mode gate, validate
the complete shape through central validation schemas, and run locked I/O/evaluation
off the event loop. Subagents have no inherited coordinator authority.

## Archive close protocol

An active cycle closes only when every item is terminal. The server first writes
an immutable archive containing the cycle digest, terminal outcomes, bounded
evidence, references, close time, and the coordinator's summary; it then
atomically clears `active_cycle` in `state.json`. On a crash between those
writes, a retry verifies the matching archive digest and completes the state
clear. Archives have count and byte ceilings; a full archive refuses closure
and retains the terminal active cycle rather than silently dropping history.

## Legacy Goal Conductor records

On the first work-item operation, the store performs a locked, idempotent
import from its own folded session ledger. It accepts only documented `item-N`
JSON string records with a valid current typed acceptance condition. Legacy
`running`/`waiting` become `waiting` with bounded migration provenance; `pass`
becomes `accepted`; `fail` becomes `rejected`. Malformed records become named
migration warnings and are never guessed into work. If every imported item is
terminal, the importer creates one immutable imported archive; otherwise it
creates one active migrated cycle. The retained `ledger_entry.py` script is
legacy compatibility only, not a new-cycle dependency.

## Explicit non-goals

This store is not a worker queue, task runner, worktree manager, lane/mailbox
system, dashboard board, liveness detector, or transcript archive. It records
coordinator-owned outcomes and acceptance evidence only. Worker assignment,
delivery receipts, cross-session reads, worker updates, and human-approval
controls require a later explicit capability boundary.
