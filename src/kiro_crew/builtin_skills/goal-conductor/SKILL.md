---
name: goal-conductor
description: Own a long-horizon goal end to end: decompose it into assertable work items, coordinate focused child sessions, verify typed acceptance, and decide each next round. Use when the user gives a goal that merits several bounded outcomes and active oversight.
---

# Goal Conductor

You own the goal. You do not do a work item's implementation yourself.

Your jobs are to decompose a goal, create durable outcome records, open focused
child sessions where useful, verify product-owned acceptance evidence, and make
the next-round decision. If a task needs a file written, a build run, or a fix,
it belongs to a work item or a child session, not to you.

Use the product work-item tools for every new item. They are scoped to this
coordinator session; never supply or infer another session's key. Load a
deferred tool with `tool_search` if it is not yet available.

## What qualifies as a work item

Create an item only when it is independent, long enough to warrant coordination,
and has a completion condition you can state before work begins:

- `pr_checks`: one repository and positive PR number;
- `file`: a named path that should exist or not exist;
- `human_approval`: a legitimate user decision that remains pending until a
  later product surface supports recording it.

There is no command, shell, argv, repository checkout, lane, mailbox, or agent
name in an acceptance condition. A condition that says “run this command” is
not accepted. Express CI-backed work as `pr_checks`, or stop for a human
decision if no typed condition fits.

Fewer than two qualifying candidates usually do not need a conductor. Say so
and handle the work in this session instead.

## Round 0 — agree the plan

Restate the goal, list the proposed outcomes and their typed acceptance
conditions, and state the intended small concurrency. Wait for the user's
approval before opening child sessions or creating the durable cycle.

After approval:

1. Call `work_cycle_open` with the goal and a concrete next action.
2. Call `work_item_create` for each approved outcome. Supply the immutable
   typed acceptance condition, a concrete next action, and only advisory
   resource paths that are already known.
3. Use `session_ledger_record` only for the conductor's own goal, phase, and
   resumable next step. Do not encode work items in its `artifacts` map.

`work_item_create` returns the opaque ID. Keep it in the immediate child prompt
when creating a child session, but do not present that as an assignment: Slice
2 deliberately has no durable worker binding, dispatch claim, delivery receipt,
or liveness promise.

## Coordinate a round

For a focused item, create a persistent child session with a title that names
the intended outcome, select the appropriate crew first, and send a complete
seed prompt. The seed names the outcome, the acceptance condition, relevant
resources, and where to report. Send it before claiming useful progress.

Do not write a child session key, message cursor, or “dispatched” state back
into a work item. Those are worker-assignment semantics and are intentionally
absent until a later slice can make them true atomically. A child transcript is
progress evidence, not completion evidence.

Use `work_item_update` only for a canonical artifact reference, advisory
resources, a concrete next action, or a bounded `progress` / `blocker` event.
It cannot change state or acceptance. A materially different acceptance
condition requires a new item; preserve the old item and its evidence.

Use `work_item_transition` only to move a non-terminal item to `proposed`,
`waiting`, `rejected`, or `cancelled`, always with a reason. You cannot set
`accepted`; only a recorded evaluator pass can do that.

## Patrol and acceptance

On every monitor wake, call `work_item_list`. It returns this coordinator's
bounded active cycle and migration status; it is the durable authority after
compaction, not the prior transcript.

Call `work_item_evaluate` with every eligible non-terminal item ID in one batch.
The product owns the evaluator and records its evidence atomically:

- `pass` moves that item to `accepted`.
- `fail`, `pending`, `refused`, and `error` leave it non-terminal. Inspect the
  recorded evidence and either repair, wait, re-express the typed condition, or
  ask the user.
- One evaluator error must not conceal sibling results.

Never declare an item complete just because a child session says it is done.
Never turn a failed check into a rejected item implicitly. Rejection and
cancellation are explicit coordinator decisions.

Remain quiet on a no-signal patrol. A real signal is a recorded acceptance
verdict, a blocker, a child question, or a decision the user must make.

## Close a cycle

When every item is terminal, call `work_cycle_close` with a concise outcome
summary. The product writes an immutable closed-cycle archive and then clears
only the active cycle. Later reads use `work_cycle_archive_list` and
`work_cycle_archive_read`; they never reopen a closed cycle.

If an item is no longer useful, explicitly reject or cancel it first. Closing a
cycle never silently changes state. Closing a dashboard tab also does not close
or delete a cycle; permanent history deletion removes both active data and its
archives.

## Legacy compatibility

Older installed copies of this skill stored `item-N` JSON strings in the
session ledger and used `scripts/ledger_entry.py` plus `scripts/accept_eval.py`.
Do not use either for new work. On the first product work-item read, KiroCrew
imports valid legacy records once: open records become `waiting`, terminal
records become an imported archive, and malformed values remain visible as
migration warnings rather than guessed work.

The old codec remains packaged for one compatibility release so already-copied
skills do not break. It is not part of this skill's new-item or patrol path.

## Stop conditions

Stop and report when all items are accepted, the user-set budget is spent, a
typed condition cannot settle the needed decision, or an item keeps failing and
the user must choose whether to retry, reject, or cancel it. Call
`autonudge_stop` when the coordinating loop ends.
