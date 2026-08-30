# Channel Collaboration and Checkpoint Integrity — Proposal

**Status:** implemented; pending code review

**Date:** 2026-08-30
**User story:** As an operator coordinating a persistent channel, I can rely on
Multiplex to distinguish meaningful work from transport activity, and every
channel participant can retain the collaboration rules through a fresh session
or compaction without relying on an earlier conversation turn.

## Decision

This delivery implements two connected changes:

1. A global `channel-collaboration` skill is packaged for every KiroCrew session.
   It teaches channel membership, peer-message semantics, capacity, and the
   distinction between a new channel agent and an attached dashboard session.
   A member that is not the channel coordinator may inspect and report, but it
   may not create, attach, or remove peers.
2. Deterministic channel delivery state is separated from authored Multiplex
   checkpoints. Peer receipts become inbox/attention state, while Updates keeps
   explicit milestones and lifecycle events. A deterministic freshness guard
   requests a checkpoint after meaningful state changes; it never manufactures
   a goal, decision, progress claim, or next action from a transcript.

These changes use the existing KiroCrew turn lifecycle. They do not depend on
an external Claude Code or Codex hook for dashboard sessions.

## Problem

Persistent channels currently have two gaps.

### Channel collaboration knowledge and authority

Peer etiquette exists in the Nexus harness skill
`kirocrew-orchestration`: `progress` is passive, `mention` requests attention,
`done` hands off work, and an interrupt is reserved for information that
invalidates an active premise. That skill is not a packaged KiroCrew built-in,
so a new dashboard session, attached session, or channel-owned worker is not
guaranteed to discover it.

The dashboard can create a channel-owned agent or attach an existing session,
but the current session-bound MCP surface exposes only channel status and peer
posting. An agent cannot safely manage membership itself. Teaching the REST
endpoint in a skill would not help: that endpoint is operator-authenticated and
must not be exposed as raw dashboard authority to every channel participant.

### Transport receipts are rendered as work updates

The gateway persists peer deliveries in each slot's bounded peer inbox. When it
drains that inbox into a turn, it appends a deterministic timeline entry such
as `Received 3 peer channel message(s).` The Multiplex Updates view renders that
entry alongside explicit authored milestones, for example a closed work item or
a dispatched wave. Receipt counts therefore look like progress even though they
only prove message delivery.

The same mixing makes a received peer message look like the session's next
work. A peer receipt is neither an authored `next_action` nor evidence that the
session adopted the peer's request.

## Goals

- Make collaboration rules discoverable to all KiroCrew sessions, including
  sessions that join a channel after they start.
- Give channel coordinators an authenticated, bounded management capability;
  keep ordinary members read/report-only.
- Preserve peer-delivery evidence without presenting receipt counts as work.
- Keep Multiplex's `next_action` authored and explicit.
- Surface an overdue checkpoint truthfully for dashboard-backed sessions when
  deterministic activity changed but the owner has not recorded its new work
  state.
- Preserve the same durable checkpoint contract across dashboard-backed
  KiroCrew, Claude Code, and Codex sessions while allowing KiroCrew to produce
  its own lifecycle facts in-process.

## Delivered behavior

### 1. Packaged channel-collaboration skill

One KiroCrew built-in skill named `channel-collaboration` is indexed for
every dashboard session and loaded when channel work is relevant or the skill
is named. It covers:

- whether a session is channel-owned, attached, or not yet a member;
- the channel-local member ID required to address each peer, alongside its role
  and current state;
- `progress`, `mention`, `done`, ordinary queued delivery, and interrupt
  etiquette;
- when to request a new worker versus attach an existing session;
- capacity awareness and terminal-member cleanup;
- the coordinator boundary: members request membership changes from the
  coordinator instead of attempting to create peers themselves;
- checkpoint ownership boundaries: attached dashboard sessions own durable
  Multiplex checkpoints, while channel-owned workers report through the channel.

The Nexus harness `kirocrew-orchestration` skill becomes a pointer to this
shared channel contract for its peer-channel section. The packaged skill stays
self-contained so KiroCrew sessions never require the Nexus repository merely
to learn basic channel behavior.

### 2. Deterministic channel-bound skill injection

Channel membership is durable runtime state, so it is a stronger trigger than
language-model skill selection. The gateway injects the packaged
`channel-collaboration` skill without waiting for a keyword in two paths:

- **Channel-owned worker:** `run_channel_agent` prefixes the skill body before
  the worker's first assigned turn. Its compact membership context is included
  on every later channel turn.
- **Attached dashboard session:** the normal context builder detects current
  channel attachment and injects the skill before the next turn. It repeats the
  compact membership context after a restart or compaction, so the rule does
  not depend on an old conversation window surviving.

The injection identifies the caller's channel-local member ID, role,
coordinator status, allowed operations, channel capacity, and a roster of each
peer's channel-local ID, role, and current state. The roster refreshes from the
durable channel membership whenever a member joins, leaves, or changes state.
It does not expose raw dashboard session keys, provider conversation IDs, or
arbitrary peer message bodies. Channel-local IDs are sufficient to address a
peer through the bounded channel protocol without granting access to that
peer's private dashboard session. An attached dashboard session receives the
checkpoint guidance and freshness reminder for its durable slot. A
channel-owned worker instead receives the explicit boundary that it reports
verified facts through channel `progress` or `done`; its coordinator or an
attached dashboard session owns the durable Multiplex checkpoint. A session
that is not attached may still load
the skill by name, but receives no authority or channel-specific state.

### 3. Coordinator-gated channel management

The delivery adds a dedicated session-bound channel-management MCP operation, rather than
reusing generic session-control tools. It validates the caller's current channel
membership and coordinator identity before it can:

- add one channel-owned agent from an allowed seat with a bounded role/task;
- remove a terminal or explicitly selected member; and
- read channel capacity and members.

Ordinary members may use the read/report portions of the collaboration surface,
but mutation attempts return an explicit coordinator-required response. The
operation respects the configured channel member cap and cannot target a channel
the caller is not attached to. Provider-owned channel workers retain their
existing containment against generic session-control tools.

An existing dashboard session is attached only by the dashboard user's explicit
channel-UI action. The agent-facing capability neither lists dashboard slots nor
attaches them by label: a channel coordinator must not turn untrusted peer text
into control of another private conversation. The UI attachment path remains
application-scoped and is an explicit operator choice; once attached, the
session receives the deterministic collaboration context described above.

### 4. Deterministic channel projection

Keep the peer inbox as the durable receipt record. Replace generic receipt rows
in the Multiplex Updates timeline with deterministic projections:

| Event | Multiplex projection | Does not change |
|---|---|---|
| passive `progress` or `done` delivery | unread peer-inbox count and latest-delivery metadata | authored update, `next_action` |
| `mention` delivery | peer-request attention with channel and sender | authored update, `next_action` |
| accepted interrupt delivery | urgent peer-request attention | authored update, `next_action` |
| member added, attached, dismissed, failed, or completed | channel roster/capacity event | authored goal or decision |

Existing persisted receipt rows use an exact, generated shape. The Multiplex
projection may classify that shape as transport history and hide it from Updates
without deleting the durable record. Arbitrary agent-authored timeline text must
never be pattern-matched or rewritten.

### 5. Checkpoint freshness guard

KiroCrew already applies an explicit `session_checkpoint` through a signed,
session-bound directive and persists it on an attached dashboard slot. The new guard adds
deterministic in-gateway lifecycle hooks and freshness state beside that record:

- a meaningful event advances a slot activity generation;
- a successful explicit checkpoint records the generation it covers;
- when activity is newer than the last checkpoint, the slot is marked
  `checkpoint_overdue`;
- before the next normal agent turn, KiroCrew injects a concise system reminder
  to publish a checkpoint before ending the turn; and
- Multiplex renders the stale condition rather than inventing a replacement
  summary.

For dashboard-backed sessions, meaningful events include task/goal start, accepted channel-management changes,
received peer mentions or interrupts, child-work completion, explicit decision
or blocker directives, and session finish. Passive peer receipts alone update
the inbox count but do not make a narrative checkpoint stale.

The initial policy is a soft guard: it requests a fresh checkpoint but never
blocks a task turn. The existing maintenance barrier remains stricter: a session
already must write a fresh checkpoint before it can acknowledge a coordinated
restart.

These are KiroCrew-native hooks at channel membership, peer-delivery, child
completion, and pre-turn boundaries. Claude Code and Codex retain their existing
external lifecycle bridges because KiroCrew does not own their client process.

## Success criteria

- A new, attached, or channel-owned KiroCrew session can discover the shared
  collaboration guidance after startup or compaction without relying on an LLM
  to select the skill.
- Every channel participant receives its own channel-local member ID and a
  current peer roster with channel-local IDs, roles, and states; roster refresh
  never exposes raw dashboard session keys.
- Only the active channel coordinator can add or remove a member through the
  session-bound management operation; attaching an existing session is an
  explicit dashboard-user action.
- Membership changes fail closed for an unaffiliated session, a non-coordinator,
  an unknown seat/slot, duplicate attachment, or a full channel.
- Peer receipt counts no longer appear in Multiplex Updates or overwrite an
  authored `next_action`.
- A mention or interrupt is visible as separate attention with its source,
  while passive delivery remains observable through the inbox count.
- A dashboard-backed activity generation newer than the checkpoint produces a
  durable overdue indication and one next-turn reminder; an explicit checkpoint
  clears it. Channel-owned workers retain progress in channel delivery until a
  future dedicated worker projection exists.
- Existing checks for signed checkpoint provenance, peer delivery persistence,
  and channel-worker session-control containment remain green.

## Delivery slices

1. Define the durable checkpoint-freshness and peer-attention data shape, then
   add gateway tests for transition, persistence, restart restoration, and
   non-coordinator rejection.
2. Add the session-bound coordinator operation and the packaged
   `channel-collaboration` skill; update the Nexus harness skill to point to it.
3. Change the Multiplex projection/UI to separate semantic Updates, peer inbox,
   peer-request attention, and roster capacity. Include compatibility handling
   for already-persisted generated receipt entries.
4. Verify one KiroCrew session and one attached channel session end-to-end,
   then verify the existing native Claude/Codex lifecycle bridge does not change
   authored checkpoint ownership.

## Out of scope

- Summarizing a transcript or a peer message with an extra model call.
- Treating peer content as operator authorization or automatically adopting it
  as a recipient's plan.
- Granting generic session creation, termination, transcript reading, or raw
  dashboard API credentials to channel workers.
- Changing the configured channel cap or automatically removing members solely
  because they are idle.
- A hard gate that prevents normal work until a checkpoint is written.
- Replacing the existing external Claude Code or Codex lifecycle hooks.
