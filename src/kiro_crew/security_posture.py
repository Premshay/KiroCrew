"""Security-posture detail registry — the data behind Settings → Security.

Every count in this module is DERIVED from the live control it describes — never
a hardcoded number that can drift out of sync with what it covers — and each
control also resolves its concrete ``items`` so the dashboard can expand a row
into the real list instead of asking the reader to trust a pill.

Design contract:

- **Derived where a live list exists.** A count is always ``len(items)``, so the
  pill and the expanded list can never disagree. Seven controls resolve their
  items straight from the enforcing object (``sensitive_home_dirs()``,
  ``write_protected_home_paths()``, ``BUILTIN_DENIED_RULES``,
  ``SUSPICIOUS_BASH_PATTERNS``, the MCP dispatch registries, ``exfil_query_min_len()``,
  ``sel.audit_sources()``) — for those, drift is structurally impossible.
- **Three controls are curated, and are guarded by inverse tests.**
  ``_REDACTION_SINKS``, ``_CREDENTIAL_FAMILIES``, and ``_EXFIL_HEURISTICS`` have no
  single live list to enumerate (a redaction sink is a *call site*, a credential
  family is a *regex alternative*, a heuristic is a *branch*). Deriving a count
  from ``len()`` of a hand-written tuple would just relocate the original
  stale-number bug into this module, so each is paired with an
  **omission-detecting** test: the redaction registry is checked against every
  redactor call site in the package (with an explicit
  ``NON_EGRESS_REDACTION_MODULES`` allowlist), and the family/heuristic lists are
  checked against the live scanners. An omission — a curated list silently
  missing an entry — is the failure mode here, and only a test that
  detects an omission
  catches it, and a `len()` assertion never will.
- **Posture, not secrets.** Every item here is either a *public* control
  definition (a path pattern the agent is blocked from, a redaction sink's
  module, a credential FAMILY name) or a derived count. This endpoint never
  emits credential material, policy/profile rule CONTENTS (the governance
  ceiling the agent is fenced from — see the posture-only snapshot in
  ``handlers/security_governance_policy.py``), or user data. Sensitive-path entries are the *blocklist* itself,
  which is already documented in ``docs/architecture/security-deep-dive.md`` and is not a
  secret: knowing ``~/.aws`` is blocked does not help reach it.
- **One registry, two readers.** The dashboard renders it; ``test_security_posture``
  pins the derivation so a control that grows without its detail entry fails CI.

Redaction sinks are *named* in ``_REDACTION_SINKS`` rather than counted straight
from call sites, because a raw ``grep`` count would count comments, internal
non-egress uses, and every refactor. But the list is not trusted on its own: the
drift guard walks every redactor call site in the package and requires each
module to be either a registered sink or an explicitly-reasoned entry in
``NON_EGRESS_REDACTION_MODULES``. So the count means "egress paths covered" AND
cannot silently omit one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

import kiro_crew.validation as _validation
from kiro_crew import security
from kiro_crew import sel as _sel_mod
from kiro_crew.executors import governance_executor
from kiro_crew.security_posture_redaction import (  # noqa: F401 - public inventory export
    NON_EGRESS_REDACTION_MODULES as NON_EGRESS_REDACTION_MODULES,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PostureItem:
    """One concrete element behind a control's count.

    ``label`` is what the control covers (a path, a sink, a credential family);
    ``detail`` is the optional "how/where" shown as secondary text.
    """

    label: str
    detail: str = ""


@dataclass(frozen=True)
class PostureControl:
    """One expandable security control.

    ``items_fn`` is a callable (not a list) so the items are resolved at request
    time against the LIVE control — a governance profile reload or a user-added
    deny rule is reflected without a gateway restart.

    Deliberately has NO ``items`` field: an eagerly-captured list is the exact
    shape of the drift bug this module exists to prevent (a snapshot taken at
    import time and then quietly outliving the thing it described), and a mutable
    default would also make this "frozen" dataclass unhashable.
    """

    key: str
    label: str
    unit: str
    items_fn: Callable[[], list[PostureItem]]
    summary: str = ""
    source: str = ""


# ── Redaction egress paths ──
# The distinct boundaries where agent/tool output crosses to a human or an
# external service and therefore runs a redaction pass. Named here (rather than
# counted straight from grep) so the count means "egress paths covered" instead
# of "call sites that mention redact" — but see NON_EGRESS_REDACTION_MODULES
# below: a drift guard checks this list against EVERY redactor call site, so an
# egress path cannot be silently omitted. Each entry names the owning module.
# Where a sink runs only ONE of the two scanners, its detail text says so.
_REDACTION_SINKS: tuple[tuple[str, str, str], ...] = (
    (
        "Claude background turns",
        "dashboard/chat_turn/claude_background.py",
        "Between-turn tool output and autonomous assistant text are scrubbed before "
        "WebSocket delivery and durable transcript storage, independently of the "
        "ordinary foreground turn renderer.",
    ),
    (
        "Restart maintenance outcomes",
        "dashboard/handlers/sessions_parts/maintenance.py",
        "Channel-worker reset errors are scrubbed before their detail is returned "
        "in the maintenance HTTP response. The audit copy is scrubbed separately.",
    ),
    (
        "Agent-authored peer-channel messages",
        "dashboard/handlers/sessions_parts/channels.py",
        "Authenticated member posts are scrubbed before Channel.post persists, "
        "broadcasts and delivers them to other members. This endpoint is separate "
        "from the browser channel-message route.",
    ),
    (
        "Design critique review data and prompts",
        "apps/builtins/design_critique/backend/records.py",
        "Operator-supplied project context, review findings and generated design prompts "
        "are scrubbed before they are stored, returned to the dashboard or sent "
        "to a design agent. Redaction failure replaces the text with a refusal marker.",
    ),
    (
        "Peer-channel messages delivered to crew members",
        "dashboard/handlers_channel.py",
        "The message body is scrubbed before it enters a member's queue and can "
        "reach that member's agent or dashboard transcript.",
    ),
    (
        "Interrupted work in a mate reply",
        "dashboard/responsive_chat.py",
        "The bounded interrupted-turn reference passed to the next model prompt. "
        "Structural markers are neutralized, then exfiltration URLs and "
        "credentials are redacted before the reference enters model context.",
    ),
    (
        "The settings diagnosis",
        "dashboard/change_cards_settings.py",
        "Current and default values of every setting `diagnose_settings` reports, "
        "on their way to an agent's model context. A credential-like key is "
        "reduced to whether it is set, and every remaining string passes the "
        "credential redactor, because a setting value can be a token the operator "
        "pasted into a free-text field.",
    ),
    (
        "Change-card and guide rows in a conversation",
        "dashboard/chat_cards.py",
        "The title and result text of each change card and guide row written "
        "into a chat transcript and rendered in the browser. The text comes from "
        "an agent's proposal or a gateway outcome, so each string passes the "
        "exfiltration-URL then the credential redactor before it is stored.",
    ),
    (
        "A return-triggered warm task brief",
        "dashboard/warm_task_brief.py",
        "The original request and model-generated task facts written into a chat "
        "transcript and rendered in the browser. Each string passes the "
        "exfiltration-URL then the credential redactor before it is stored.",
    ),
    (
        "Change-card details shown before Apply",
        "change_card_catalog_plans.py",
        "Values a change card shows in full so the user sees what Apply runs: an "
        "MCP server's launch line, environment values and request headers. Each "
        "value passes the credential redactor before it reaches the card.",
    ),
    (
        "Mate goals",
        "member_goals.py",
        "The goals, key results and their readings a mate may write "
        "about itself, on their way to the Goals tab, the mate's tools and its "
        "saved goal reports. Each string passes the "
        "exfiltration-URL then the credential redactor before it leaves the record.",
    ),
    (
        "The symptom probes",
        "diagnose_probes.py",
        "Evidence strings the read-only `diagnose_settings` probes return to "
        "an agent's model context: log lines, job errors and file excerpts read "
        "from this host. Each string passes the credential redactor before it "
        "leaves the probe.",
    ),
    (
        "A remote crew's answer to one chat turn",
        "dashboard/handlers_crew_turn.py",
        "Everything a headless remote crew sends back for a turn, on its way to the "
        "caller and into a transcript: the streamed SSE events, a non-streamed JSON "
        "body, and the detail of a refusal the crew itself returned. The crew is a "
        "model with tools, holding its own Kiro identity and control secret in its "
        "environment, so a reply can quote a credential either because the model "
        "repeated its own environment or because content it fetched told it to -- the "
        "same reason the peer proxy redacts, and this route is a second way out of "
        "the same machine. The stream is reassembled into whole SSE EVENTS before "
        "each is scrubbed, because the socket splits wherever it likes and a "
        "credential straddling two chunks is invisible to a per-chunk pass. An event "
        "too large to scrub, or one the scrubber refuses, ends the stream instead of "
        "being forwarded; a non-streamed body that cannot be scrubbed is refused "
        "rather than returned.",
    ),
    (
        "Thread, GIL and loop-stall diagnostics",
        "diag/threads.py",
        "Python frames, folded stacks and loop-stall dump text, on their way to an "
        "operator through the debug read routes. Three things make this an egress "
        "path rather than an internal read. A frame label carries the absolute "
        "path of the file it came from, so it carries the operator's home "
        "directory and therefore their username; the loop-stall dumps are written "
        "by `faulthandler` from C, which cannot redact as it writes, so read time "
        "is the ONLY point at which they can be scrubbed at all; and a sampled "
        "frame can name a literal. Each string passes the shared path-shortening "
        "then the exfiltration-URL and credential chain, and the shortening runs "
        "FIRST because it is what removes the home prefix that the credential "
        "scanners do not look for. A scanner that raises drops the field rather "
        "than emitting it unredacted.",
    ),
    (
        "Fold values filled into a mate's dashboard page",
        "dashboard/handlers/member_dashboard.py",
        "Every string a template's fold paths resolve to, on its way into the page's "
        "own data island. A fold value is whatever a conductor wrote into the crew "
        "log, so a `session_ledger_record(goal=...)` carrying a pasted private key is "
        "rendered verbatim by any template that binds that fold -- and a mapping's "
        "KEYS are agent-authored on the same terms, an artifact name being a free "
        "string. This is the one step that hands fold values to a page's own script, "
        "so the pass sits here rather than in the feed, which the conductor also "
        "reads. Recursive over the resolved value rather than a named list of fields, "
        "because which fold reaches a page is a decision each TEMPLATE makes. A "
        "snapshot is taken from what the page holds, so it inherits this pass. The "
        "same traversal drops `worker_session_key` and empties a `bind` event's "
        "text, which covers the fields known to be secrets and says nothing about "
        "prose that happens to contain one.",
    ),
    (
        "Conductor work items shown on the Crew page",
        "dashboard/handlers/work_ledger_board.py",
        "Every string in one conductor's work-item board on its way to a browser: "
        "the item titles, each worker's own `summary`, the conductor's `decision`, "
        "and every artifact key and value. All are written by an AGENT and nothing "
        "between that write and this read inspects them, so a worker that pasted a "
        "token into its own status line would otherwise have it rendered verbatim "
        "on the page -- the masking that removes `worker_session_key` covers the one "
        "field known to be a secret and says nothing about prose that happens to "
        "contain one. The pass is recursive over the whole payload rather than a "
        "named list of prose fields, so a field added to `WorkItem` later is covered "
        "by default; it runs through `platform.context.redact_via_context`, so both "
        "scanners run in the shared order and a host with a loaded companion applies "
        "that companion's patterns too. Deliberately fail-closed: a composition "
        "error surfaces as a 500 rather than an un-redacted board.",
    ),
    (
        "Chat titles and kept models in the Settings follow-default confirm",
        "dashboard/chat_follow_default.py",
        "The titles of the first five chats the Settings follow-default count names, "
        "and the name of each model those chats keep, on their way to the confirm "
        "dialog in the browser. A title is LLM-written or the first words a person "
        "typed, and a closed chat's title and model are read straight from its "
        "transcript line, so nothing upstream of this answer inspects them. Each "
        "passes the dashboard's display redaction, `_redact_for_display`; a title "
        "does so BEFORE it is capped at 200 characters, because a cut taken first can "
        "split a credential into a stub the patterns no longer match. The raw model "
        "still drives the clear. Only chats the count already names contribute, and "
        "the route is owner-only.",
    ),
    (
        "Tool-call risk questions sent to the decision judge",
        "decisions/points/tool_risk.py",
        "The tool name, its arguments and the message excerpt that one `tool.risk` "
        "question carries to the third-party judge. Unlike its sibling "
        "`decisions/gate.py` -- which SCANS the same request and refuses the whole "
        "call on a hit, emitting nothing -- this module scrubs, and the scrubbed "
        "bytes are what leaves the machine: a credential in a tool argument is "
        "ordinary (an `aws` command, a curl header), so refusing on it would mean "
        "the seam never looks at the calls most worth looking at. Each field passes "
        "the shared exfiltration-URL then credential chain and is clipped only "
        "AFTER that, because clipping first can halve a secret into a fragment "
        "neither redactor matches; a scanner that raises drops the field instead of "
        "sending it. The gate then scans the placeholder, so a request it clears "
        "carries no unredacted byte.",
    ),
    (
        "Mid-turn handling decisions",
        "decisions/points/message_steer.py",
        "The running turn's own request and its newest assistant text and tool "
        "activity, sent to the third-party judge that decides whether a message "
        "typed into that turn interrupts it or waits for the next one. Unlike the "
        "gate above it, this module EMITS its redacted bytes: the cleaned excerpt "
        "is what leaves the machine, because refusing on it would turn 'the reply "
        "quoted an env file' into 'the seam stopped deciding'. Both scanners run, "
        "in the order credential-then-URL, over each half in FULL, and the clip to "
        "the consented budget is taken AFTER them -- a cut placed first can halve a "
        "secret, and a half is a fragment neither pattern matches, which is the "
        "same order `tool_risk.scrubbed` states; a scanner that fails yields the empty "
        "string rather than the input, and the gate's own scan still refuses the "
        "whole request for a spelling this pass missed. Nothing else about the "
        "turn is sent -- private reasoning is excluded at the read.",
    ),
    (
        "Compaction scoring sent to the decision judge",
        "decisions/points/compaction_keep.py",
        "A whole slot transcript sent to the third-party judge that scores, in the "
        "SHADOW, which of a session's tool calls an automatic compaction should have "
        "kept: the conversation text and every tool-call INPUT in it. Tool OUTPUT "
        "never leaves the machine at any fitting stage -- each result is replaced by "
        "its own character count (`result_placeholder`), which is the widest single "
        "omission in this table and is what keeps the largest half of a transcript "
        "local. Like the two points above it this module EMITS its redacted bytes "
        "rather than refusing on a hit: a credential in a tool argument is ordinary, "
        "and refusing would mean the measurement never runs on the transcripts most "
        "worth measuring. Both scanners run, credential-then-URL, over each field in "
        "FULL, and the stage's clip is taken AFTER them -- a cut placed first can "
        "halve a secret into a fragment neither pattern matches; a scanner that "
        "fails yields the empty string rather than the input, and the gate's own "
        "scan still refuses the whole request for a spelling this pass missed. "
        "Routed through `platform.context.redact_via_context` rather than the bare "
        "baseline pass, so a loaded companion's own credential and cookie spellings "
        "apply: this state is a whole transcript, and a baseline-only scrub would "
        "send a companion-defined secret verbatim to a third party. A composition "
        "failure re-raises out of the shim and DROPS the field here, which is the "
        "same direction every other arm of this module fails in. The shipped "
        "exfiltration-URL pass runs on top, because the shim covers credentials. "
        "Authorized by a THIRD keystone scope (`consent.STATE_KEY_COMPACTION`), "
        "because neither the message excerpt the main switch records nor the "
        "single-call `tool_args` scope describes it.",
    ),
    (
        "Recalled-memory questions sent to the decision judge",
        "decisions/points/memory_recall.py",
        "A snippet of each memory vector similarity recalled for this turn, sent to "
        "the third-party judge that decides which of them are worth their place in "
        "the prompt. Like the three points above it this module EMITS its redacted "
        "bytes rather than refusing: a recalled episode is text the agent wrote down "
        "turns or days ago, so refusing on a single episode that happens to quote an "
        "env file would stop the point firing on that store at all. Both scanners "
        "run over each snippet in FULL, credential then URL, and the clip to 200 "
        "characters is taken AFTER them -- a cut placed first can halve a secret into "
        "a fragment neither pattern matches, the same order `tool_risk.scrubbed` "
        "states; a scanner that fails yields the empty string rather than the input, "
        "and the gate's own scan still refuses the whole request for a spelling this "
        "pass missed.",
    ),
    (
        "Member capability editor responses",
        "agent_capabilities_redaction.py",
        "Owner-facing capability rows, Parent-change previews and impact summaries. "
        "safe_view applies redact_via_context before serialization and masks credential "
        "map values while preserving their structure. Retained secret values and "
        "source-content digests remain server-side.",
    ),
    (
        "Crew log entries",
        "crew_log/emit_text.py",
        "Message bodies written to the append-only per-session crew log under "
        "`<home>/crew-log/sessions/` -- what the user typed and what the model "
        "answered. This sink is a FILE rather than "
        "a response, so what it writes outlives the process and is read back "
        "later by folds and the session panel; that makes it an output boundary "
        "with a longer reach than an egress response, not a lesser one. Every "
        "body passes the shared exfiltration-URL then credential chain in the "
        "emitter rather than at its call sites, so a new call site cannot forget, "
        "and a redaction that fails writes the empty string instead of the input.",
    ),
    (
        "Subagent wait broadcasts",
        "subagent_manager/admission/waits.py",
        "The wait REASON on the `subagent_waiting` frame, which reaches every dashboard "
        "browser, the SSE relay and every app holding a `subagent:*` scope. It is the "
        "same provider- and store-authored prose the task routes scrub and the sibling "
        "`subagent_done` event redacts field by field, so it passes the shared "
        "exfiltration-URL + credential chain before it leaves the process.",
    ),
    (
        "Task queue panel responses",
        "dashboard/handlers/tasks.py",
        "Store-authored prose served by the Tasks & capacity routes: each task "
        "event's data payload and each row's wait reason, both of which carry a "
        "tool's or a provider's own error text verbatim. Every string, including "
        "the ones nested in dicts and lists, passes through the shared "
        "exfiltration-URL + credential chain before serialization; row "
        "identifiers are left intact because the panel keys its rows by them.",
    ),
    (
        "Memory recovery responses",
        "dashboard/handlers/memory_admin.py",
        "Retired episode text and supersession references, plus backup and restore "
        "failure details served to the memory recovery panel. These fields pass "
        "through the shared credential + exfiltration-URL chain before egress.",
    ),
    (
        "Memory record editor responses",
        "dashboard/handlers/memory_edit.py",
        "Record detail, correction previews, and bulk operation results served to "
        "the memory editor. Nested fields pass through the shared credential + "
        "exfiltration-URL chain before reaching the browser.",
    ),
    (
        "Member memory recall and copy responses",
        "dashboard/handlers/memory_member.py",
        "Selected facts, experiences, corrections, and owner-selected copy results "
        "returned to the dashboard or the memory_recall tool. Nested fields pass "
        "through the shared credential + exfiltration-URL chain before serialization.",
    ),
    (
        "Managed-venv update failures",
        "platform/wheel_apply.py",
        "The failure text a managed-venv shadow update shows an operator -- "
        "`kirocrew update`'s print, the dashboard's failed update step, the "
        "approve route's audit record -- plus the fallback installer one-liner "
        "printed next to it. The engine's error quotes the URL it tried and the "
        "fallback command embeds the artifact CDN base -- with a token-bearing "
        "KIROCREW_CDN_BASE either would land credentials in terminal scrollback, "
        "the dashboard or the audit chain, so both strings run the context "
        "redactor (redact_via_context: credentials and exfiltration URLs, plus a "
        "loaded companion's patterns) in full before any cap.",
    ),
    (
        "CLI update output",
        "cli_server_update.py",
        "What `kirocrew update` prints around a wheel update: the release-feed "
        "URL it is checking and the plain-pip in-place upgrade hint (its command "
        "or failure note). Both embed the CDN base -- with a userinfo-bearing "
        "KIROCREW_CDN_BASE either would land credentials in terminal scrollback, "
        "so each displayed string runs only the shared credential redactor "
        "(no exfiltration-URL pass: the value is the operator's own CDN base) "
        "immediately before print; the fetch still uses the real URL.",
    ),
    (
        "Central policy fetch failures",
        "platform/policy_distribution_boot.py",
        "The failure text of a central governance-policy fetch, which reaches TWO "
        "surfaces: `kirocrew policy fetch` on stdout and a `logger` line (durable, "
        "and served to the dashboard by `GET /api/logs`). The bytes are not ours -- a "
        "malformed document reaches the message through a parser error, and `json`'s "
        "errors quote the offending text, so an endpoint that echoes back the "
        "request's `Authorization` header (its own request reflected, a proxy error "
        "page) would carry that credential to both surfaces. Every detail goes "
        "through `_sanitize_detail`, which applies the shared credential + "
        "exfiltration-URL chain and additionally elides the operator-configured "
        "source URL and its bare hostname.",
    ),
    (
        "AutoNudge monitor responses",
        "dashboard/handlers/autonudge.py",
        "Structured monitor state served by the AutoNudge dashboard endpoints. "
        "Provider observations are arbitrary nested JSON and can quote credentials "
        "or exfiltration URLs, so every string key and value in the monitor mapping "
        "passes through the platform-aware redaction chain before the response leaves "
        "the backend.",
    ),
    (
        "Project-scan warning reasons",
        "project_scan.py",
        "The per-file warning strings the folder scanner returns "
        "(`CandidateTree.warnings`), which ride the scan/scaffold API responses "
        "into the dashboard's preview surface. A warning reason embeds the "
        "exception text of a failed parse, and a parser quotes the offending "
        "source line back -- tree content the user merely pointed at, so a "
        "malformed workspace declaration holding a credential would otherwise "
        "be echoed verbatim. Redacted at construction (`_warning_reason`), so "
        "no unredacted copy of the reason ever exists for a later consumer (a "
        "log line, a persisted report) to leak.",
    ),
    (
        "AWS identity-probe failures",
        "aws_consent.py",
        "The stderr of a failed `aws sts get-caller-identity`, run to show the "
        "operator which account a paid AWS service would bill before they confirm "
        "it. The text reaches the Settings > Voice consent card "
        "(`identityDetail` over `GET /api/aws/consent`), which is the only "
        "surface that carries it. The AWS CLI quotes back what it was resolving, "
        "so a failure "
        "can carry a `credential_process` command line, an SSO start URL, or a "
        "role ARN, and an endpoint override can carry an inline-credential URL -- "
        "so the first stderr line goes through the shared credential + "
        "exfiltration-URL chain at the source, before it is put on an `Identity`.",
    ),
    (
        "Browser CLI install failures",
        "browser_cli/install.py",
        "The stderr of a failed `npm install -g @playwright/cli` / browser download, "
        "which reaches TWO surfaces: a `logger.warning` line (durable, and pasted "
        "into bug reports via `kirocrew logs`) and the Settings > Browser error card. "
        "npm quotes the command's own environment back on failure, so the text can "
        "carry a registry `_authToken`, an inline-credential proxy URL, or a "
        "`*_TOKEN=` echo -- shapes the shared credential family does NOT match, so "
        "this sink adds its own npm patterns on top of the shared two-pass and "
        "redacts at the source rather than at either boundary.",
    ),
    (
        "Browser install job failure detail",
        "browser_cli/install_job.py",
        "The `error_detail` of the gateway's browser install job, returned by "
        "`GET /api/browser/install` and in every 409 `install_already_running` "
        "body. Failed-step output is redacted by `install._step` at the source; "
        "exception and error fallbacks are not. `bounded_detail` re-redacts every "
        "carrier on the FULL text before the 2000-character cut, using only "
        "`redact_install_output` (the shared two-pass plus npm patterns), so a "
        "pre-redaction cut cannot split a credential past its matching anchor.",
    ),
    (
        "Browser panel launch failures",
        "browser_cli/launcher.py",
        "The CLI's own words when the Browser panel's address bar could not open a "
        "page (`playwright-cli -s=panel-… open|goto` failing), shown VERBATIM in the "
        "panel's failure card; the gateway log gets only the session name and the "
        "exit code, never the text. The daemon's stack quotes its "
        "environment and the URL the human typed, so the text can carry a "
        "query-string credential or a `*_TOKEN=` echo; `_error_text` distills the "
        "message and runs the shared credential + exfiltration-URL chain on the "
        "kept lines BEFORE the cap, so a truncated head cannot leak what the tail "
        "would have matched.",
    ),
    (
        "Azure DevOps comment bodies",
        "apps/builtins/issue_radar/backend/azure_client_writes.py",
        "The text Issue Radar posts as a work-item or pull-request comment on Azure "
        "DevOps. A comment body is frequently model-authored -- a crew's reply, or an "
        "AI summary the user accepted -- and publishing it is irreversible: it lands "
        "somewhere public and permanent on the customer's own organization, so a "
        "credential or an exfiltration URL cannot be walked back. `_comment_text` "
        "therefore runs the two-pass chain at the client, immediately before the body "
        "reaches `az devops invoke`, rather than trusting each caller to have "
        "redacted; the crew path already redacts and loses nothing, because both "
        "passes are idempotent.",
    ),
    (
        "Off-host backup uploads (opt-in)",
        "snapshot_redact.py",
        "The copy of a backup bundle that leaves the machine for object storage. The "
        "destination is not this module's business: the AWS Control app owns the bucket, "
        "its hardening, the consent grant and the transport, and refuses to send at all "
        "if that posture is missing or unreadable. What lives here is the one thing a "
        "hardened destination does not do -- rewriting the bytes that leave -- because a "
        "private bucket still holds whatever was put in it, and the operator's own "
        "account is not the only place a restored bundle can end up. An operator opts IN "
        "to rewriting the outbound copy through the credential + exfiltration-URL chain; "
        "it is OFF by default because substituting a tag changes length, which "
        "invalidates any format that depends on byte offsets. When it is on, databases "
        "are rewritten value-by-value through SQL rather than over their bytes, and every "
        "outbound database is rebuilt so no old value survives in page slack. A pass that "
        "cannot complete refuses the send rather than falling through to an unredacted "
        "one. The LOCAL archive is never redacted either way: it does not cross the "
        "boundary, and rewriting it would damage the only copy that restores complete.",
    ),
    (
        "Off-host backup pushes (the boundary itself)",
        "apps/builtins/aws_control/backend/backup.py",
        "Where a bundle actually leaves the machine. This module runs no scanner itself: "
        "the only redaction it performs is the call into `snapshot.prepare_redacted_copy`, "
        "which runs the full credential + exfiltration-URL chain in `snapshot_redact.py` "
        "(the row above). It is named here anyway because the call ORDER is the control -- "
        "redaction runs before the upload is authorized and before any bytes reach the "
        "transport, so a redaction that raises stops the push instead of letting it "
        "proceed unredacted, and this module pushes whatever the seam returns: the "
        "redacted copy when the operator opted in, the original when they did not. "
        "Reversing those two steps would leave the guarantee intact on paper while "
        "sending the secrets anyway, which is why the boundary gets its own row rather "
        "than being left implicit in the module that computes the copy.",
    ),
    (
        "Session intent summaries",
        "session_summary.py",
        "Intent-summary payloads persisted to the `.intents` sidecar and served by "
        "`GET /api/chat/slots/{slot}/summary`. The payload is model output derived "
        "from transcript text, so a secret or beacon URL pasted into the chat can be "
        "reproduced inside it; `normalize_payload` runs the whole nested payload "
        "through the credential + exfiltration-URL chain before the write, because "
        "the sidecar is durable and read straight back to the panel.",
    ),
    (
        "Automatic session status cards",
        "dashboard/card_lifecycle.py",
        "Bounded recent transcript messages sent to the background model and its "
        "HTML/text response served by GET /api/chat/slots/{slot}/dashboard-card. "
        "Both boundaries run credential and exfiltration-URL redaction before "
        "model input or cached publication; the owner-only GET never generates content.",
    ),
    (
        "Cross-session turn delivery",
        "dashboard/chat_delivery.py",
        "The text a steer or a queued message carries into a turn, on the path "
        "`POST /api/chat` uses. Two boundaries in one call: the text is persisted "
        "into the slot's transcript and broadcast to every connected browser as a "
        "`steer_push` / `queue_push` card, so the scan happens here, before either "
        "boundary, for every origin except the session's own human, whose text is "
        "shown as typed like an ordinary send's row.",
    ),
    (
        "Queued-message drop notice",
        "dashboard/session_control_parts/delivery_containment.py",
        "The excerpt of a queued message a drain dropped, posted back to the channel "
        "it was addressed from (`notify_channel_recipient_dropped`): conversation "
        "text leaving for an external channel, scanned for credentials and "
        "exfiltration URLs on the way out.",
    ),
    (
        "Session control read",
        "dashboard/session_control_parts/session_status.py",
        "Another session's transcript tail and its cached intent summary, served by "
        "`GET /api/session-control/read` and `GET /api/session-control/summary` to "
        "the calling agent. Conversation "
        "content read off a live slot, so it can carry a credential a tool "
        "printed — the same output-boundary reason as the session-storage inventory "
        "below, with the reader being an LLM rather than the browser.",
    ),
    (
        "Session storage inventory",
        "dashboard/handlers/session_storage.py",
        "A session's title and its first message, served by "
        "`GET /api/system/session-storage/sessions` and its per-row detail. Both are "
        "conversation content read straight off a transcript, so either can carry a "
        "key someone pasted into a chat — the same output-boundary reason as the "
        "session-memory titles below.",
    ),
    (
        "Workflow management responses",
        "dashboard/handlers/workflows.py",
        "Authored workflow source, reusable definitions, revision lineage, and run output "
        "served to the Agent Capabilities panel or returned through the workflow MCP tools. "
        "These values can contain model-authored text or tool results, so every nested key and "
        "value passes through the credential + exfiltration-URL chain before egress.",
    ),
    (
        "Chat pin previews",
        "dashboard/chat_pins.py",
        "Message previews submitted to POST /api/chat/pins are persisted to "
        "chat_pins.json and re-rendered by the pinned-messages panel, so the "
        "preview is an output boundary; credentials and exfiltration URLs are "
        "redacted before storage and on every response path (list and "
        "idempotent duplicate-create) via _redacted_pin.",
    ),
    (
        "Skill context budget",
        "dashboard/handlers/skill_budget.py",
        "Skill display names served by `GET /api/skills/-/budget`. An auto-skill's "
        "frontmatter `name` is written by the agent, so it is LLM-authored text that "
        "reaches the dashboard verbatim — the same output-boundary reason as the "
        "session-memory titles below.",
    ),
    (
        "Session & task memory panel",
        "dashboard/session_memory.py",
        "Chat titles served by `GET /api/sessions/memory`. Titles are generated from "
        "user content, and the resume path in `chat_handlers` assigns a "
        'client-supplied `body["title"]` to the slot with no scan of its own, so this '
        "serializer is the boundary that guarantees the scan — the same "
        "output-boundary reason as the sibling subagent-task text.",
    ),
    (
        "Mochi notify + pin egress",
        "apps/builtins/mochi/hooks.py",
        "Agent-authored notify text (perform_pet_action summary/chatMessage) crosses to "
        "the browser via the `mochi:notify` broadcast and the chat push; `redact_tree` "
        "scrubs credentials and exfiltration URLs before publish, the same "
        "output-boundary reason as the app plan/activity-log sinks.",
    ),
    (
        "Session transfer bundle",
        "dashboard/session_transfer.py",
        "Transcript content copied to another Kiro Crew instance over an Instances "
        "tunnel. The bundle LEAVES this host, so it is an output boundary: a "
        "transcript written before the redactors existed (or carried in from a "
        "channel) can still hold a raw credential on disk, and relying on the "
        "receiving instance to scrub it would send the secret across the boundary "
        "first. This covers **Layer A only** — the display transcript, plus the "
        "title and origin label. The Layer B kiro-cli context is deliberately "
        "forwarded BYTE-EXACT and is NOT scrubbed: its thinking blocks carry a "
        "provider signature over their own content, so any rewrite invalidates the "
        "conversation and the peer's next turn is rejected (measured: a leaf-string "
        "pass altered a signature in 41% of one developer machine's 704 sessions). "
        "Redacting that artifact and transplanting it are mutually exclusive. What "
        "bounds the exposure is the destination rather than the payload — a send "
        "goes to the OPERATOR'S OWN peer instance over a tunnel they authenticated, "
        "and the peer stores it 0600 — so Layer B never leaves the operator's own "
        "trust boundary. Inbound Layer B is validated structurally (parse-only, "
        "never rewritten) and refused whole if any record does not parse.",
    ),
    (
        "Federated session search",
        "dashboard/handlers_instances.py",
        "Rows returned by GET /api/instances/search-sessions, straight to the "
        "browser. Two distinct inputs make it an output boundary of its own: "
        "PEER rows are untrusted remote JSON (allowlist-reshaped, then title/"
        "snippet re-redacted locally — the peer claims to have scrubbed, this "
        "hub does not take its word for it), and LOCAL rows come from "
        "conversation_log.search_sessions directly, bypassing the "
        "/api/sessions/search handler where the local redaction normally runs, "
        "so the same title/snippet scrub is applied here.",
    ),
    (
        "Peer live-session list",
        "dashboard/handlers_instances_peer.py",
        "Rows returned by GET /api/instances/{id}/chat-slots, straight to the "
        "browser's Sessions list. A second, distinct Instances boundary from "
        "the federated search above: these are a connected peer's OPEN session "
        "titles, and a title is MODEL-AUTHORED text produced on the other machine. "
        "The local half of that same list has its title scrubbed by "
        "dashboard/slot_projection.py before it renders, so a peer row forwarded "
        "as-sent would be the one row in a merged list whose text never met a "
        "redactor. Allowlist-reshaped to the fields the sidebar reads, then every "
        "string run through the peer-text sink (scrub before clamp, so a "
        "credential cannot survive by sitting past the length limit).",
    ),
    (
        "Profile artifact",
        "perf_sampler.py",
        "Folded-stack profiles written by `kirocrew perf sample`. Frame labels are "
        "code identifiers and shortened paths, but the artifact exists to be sent "
        "to a maintainer, and py-spy's raw output embeds absolute paths from the "
        "target process — so it is an egress boundary, redacted on the way out.",
    ),
    (
        "md-notebook error middleware",
        "apps/builtins/md_notebook/server_http.py",
        "Every md-notebook API error body. Handlers drive git and filesystem work "
        "over caller-supplied vault URLs, ids and note paths, so an unmodeled "
        "exception carries absolute paths and git stderr; `_safe_error` scrubs "
        "credentials, exfiltration URLs and host paths before the JSON reaches the "
        "browser — the same output-boundary reason as the dashboard error sinks.",
    ),
    (
        "Issue Radar provider CLI stderr",
        "apps/builtins/issue_radar/backend/errors.py",
        "`gh`/`glab` stderr quoted into provider exception messages, which the "
        "issue-radar routes return in their error bodies and the frontend renders "
        "verbatim. `sanitize_cli_stderr` runs only the host-path pass plus a "
        "private-host filter, not the credential scanner: both CLIs take their "
        "token from the environment rather than argv and neither echoes it, so host "
        "topology is the disclosure risk here. The actionable phrasing (auth, "
        "not-found, 403, timeout) is deliberately preserved so the user can "
        "self-diagnose.",
    ),
    (
        "Auto-Improvement fallback tool audit",
        "apps/builtins/auto_improvement/spine/agent_runner.py",
        "Per-tool SEL events for the unattended subprocess agent carry the tool's TARGET "
        "HINT — a path, glob or shell command the model chose. On-disk SEL records are not "
        "redacted by the writer and the persisted HMAC chain signs the bytes as-written, so "
        "the hint is redacted here, before `log_tool_invocation`. Fail-closed: a redactor "
        "that cannot run emits a fixed placeholder rather than raw agent text.",
    ),
    (
        "Auto-Improvement commit messages",
        "apps/builtins/auto_improvement/spine/driver_commit.py",
        "Agent-authored commit subjects/bodies redacted before they become PERMANENT git "
        "history — both the local keep commit and the direct-push commit. A pushed commit "
        "message cannot be edited without rewriting history, so this is a one-way egress "
        "boundary.",
    ),
    (
        "Auto-Improvement one-click commit",
        "apps/builtins/auto_improvement/backend/commit.py",
        "The operator-triggered commit path builds its message from the queued PR body "
        "(agent-authored prose) and redacts before committing — same "
        "cannot-be-unpublished reason as the driver's path.",
    ),
    (
        "Auto-Improvement MCP tool results",
        "apps/builtins/auto_improvement/backend/mcp_server.py",
        "Every `tools/call` result is serialized run evidence handed to an LLM — a finding's "
        "note/signature/hypothesis are the model's own prose. Redacted BEFORE truncation so "
        "the cut cannot split a credential into a fragment the scanner misses. FAIL-CLOSED: "
        "these six tools are conveniences, so withholding a result beats leaking. The ERROR "
        "paths are scanned too (`_redact_error`): tool ARGUMENTS reach exception text by "
        'design — `get_finding` raises "no finding with fingerprint <fp>" with the caller\'s '
        "raw value — so a credential-shaped argument would be echoed straight back to the "
        "model and into the SEL record unless scrubbed. The exception type and JSON-RPC "
        "error code are composed in after scrubbing, so the message stays actionable.",
    ),
    (
        # The watcher's residual risk, disclosed because accepting it is the OPERATOR's
        # decision and a silent limitation is the real defect. Measured: a nested process under
        # `mode="strict"` sees ~/.aws, ~/.config/gh and ~/.docker EMPTY on a populated host,
        # and ~/.ssh exposes only `known_hosts` (host-key verification needs it) while
        # id_rsa/*.key stay hidden — so CREDENTIALS are confined. NETWORK EGRESS is NOT: the
        # sandbox never enters a network namespace (no CLONE_NEWNET; its docstring explains
        # agentic commands need reachable networking), and while curl/wget/nc are denied,
        # `python helper.py` is allowed and can open a socket. The shell denylist cannot close
        # that — it gates the requested command, not what the command then does. Consequence:
        # point the PR watcher only at repositories whose PR comments you would be willing to
        # execute; the row detail below states that consequence to the operator.
        "Auto-Improvement PR-watcher egress boundary",
        "apps/builtins/auto_improvement/backend/pr_watchers.py",
        "The watcher reads UNTRUSTED text (pull-request comments, check logs) and runs with an "
        "auto-approved shell, because its job is to run the repository's own build/test/lint, "
        "rebase, and commit. Credential stores are hidden by the strict sandbox (verified), but "
        "network EGRESS is deliberately reachable and a nested interpreter can open a socket "
        "even though curl/wget/nc are denied. Two OPT-IN gates, both default OFF, both a "
        "one-time consent: `watcherAcceptEgressRisk` is a HARD precondition — `_make_runner` "
        "refuses to build any watcher runner without it, so a watcher cannot run at all until "
        "the operator acknowledges this egress boundary — and `watcherAutoStart` separately "
        "gates whether a polled GET may PROMOTE watchers (without it a promote would need no "
        "operator action, leaving no consent moment). Treat setting either flag as agreeing to "
        "execute the pull request's comments.",
    ),
    (
        "Auto-Improvement run activity feed",
        "apps/builtins/auto_improvement/backend/runner.py",
        "The live activity ring buffer carries RAW model output — assistant text and the "
        "`command` of a bash tool call — and is served verbatim by GET /run into the "
        "dashboard — so this is an egress boundary, not an internal log. FAIL-CLOSED: an "
        "unscannable string becomes a fixed placeholder while the event structure, "
        "timestamps and other fields survive, so the operator still sees the run "
        "progressing without unscanned text reaching the browser. Recursive, because the "
        "agent event is nested. The WATCHER snapshot and chat-session records are scanned on "
        "the same grounds (`routes._redact_tree`): the watcher log ring is redacted on WRITE, "
        "and `WatcherState.as_dict` beside it serves `target`/`title`/`lastNote`/"
        "`verdictReason`/`fixing` — all model- or pull-request-derived, and the watcher "
        "ingests PR text as untrusted input by design, so unscanned a credential-shaped "
        "`target` would reach the browser verbatim. The session "
        "records go through it too, because `save_session` merges the caller's patch and the "
        "stored `title` is built from a finding's target. So do the route ERROR bodies: "
        "`commit.py` builds its `error` from git stderr — which quotes refs, paths and "
        "whatever a repository's own hooks printed — scrubbed at that source "
        "(redact-then-bound) so its character bound can never cut a credential mid-match. "
        "A refused commit is surfaced at the finding row, which makes that error a live path "
        'to the browser, so all five `result.get("error")` responses plus the '
        "PR-status and draft bodies are scanned. "
        "Covers the TERMINAL ERROR field on the same response too "
        '(`_fail`): `f"{type(exc).__name__}: {exc}"` is scanned like the feed beside it, '
        "because an exception message routinely quotes what failed — a git url, a "
        "subprocess argv, a path — so a run dying on an agent-influenced value would carry "
        "it to the browser. The exception TYPE is composed in after redaction so "
        "the message stays actionable.",
    ),
    (
        "Auto-Improvement PR prose",
        "apps/builtins/auto_improvement/profiles/github_repo/pr_recipe.py",
        "The pull-request TITLE and BODY are agent-authored prose published by `gh pr "
        "create`, and a PR description cannot be un-published. Redacted in place because "
        "prose survives rewriting; the DIFF beside it is instead detected-and-refused by "
        "`spine/push_policy.py:scan_content_for_secrets`, since rewriting a patch would "
        "corrupt the fix the gate proved.",
    ),
    (
        "Auto-Improvement finding evidence",
        "apps/builtins/auto_improvement/backend/routes.py",
        "The candidate diff and drafted PR body are agent-authored text read back off "
        "disk and rendered in the operator's browser by GET /findings/{fp}. FAIL-CLOSED, "
        "unlike the watcher log below: the text stays on disk and re-readable, so "
        "withholding it beats serving it unscanned.",
    ),
    (
        "Auto-Improvement watcher log",
        "apps/builtins/auto_improvement/backend/pr_watchers.py",
        "Per-PR watcher log lines carry agent output and third-party CI text and are "
        "served to the dashboard via GET /watchers/{fp}/log with NO second scan, so this is "
        "the only pass between that text and the browser. FAIL-CLOSED: an unscannable "
        "line becomes a fixed placeholder, so the log keeps advancing without serving "
        "unscanned text.",
    ),
    (
        "Side-chat parent snapshot",
        "dashboard/side_context.py",
        "Parent user/assistant turns embedded in the side-chat prompt. The prompt "
        "leaves the dashboard's own storage and is persisted by kiro-cli into its "
        "session file, so this is an egress boundary rather than an internal read.",
    ),
    (
        "Dashboard live stream",
        "dashboard/chat_runner.py",
        "Per-chunk StreamRedactor on the chat_chunk WebSocket stream — withholds a "
        "trailing credential-class run so a secret split across chunks cannot cross "
        "the wire.",
    ),
    (
        "Dashboard thinking stream",
        "dashboard/chat_runner.py",
        "Separate StreamRedactor for the ephemeral chat_thinking stream.",
    ),
    (
        "Dashboard final message",
        "dashboard/chat_runner.py",
        "Full-text redaction pass over the assembled assistant turn before it is " "finalized.",
    ),
    (
        "Dashboard slot snapshot",
        "dashboard/state_parts/slot_transcript.py",
        "Every message, tool input, tool output, and title in the slot payload sent "
        "to the browser.",
    ),
    (
        "Session detach notice",
        "dashboard/state_parts/session_events.py",
        "The notice sent to a channel whose session-resume binding was cleared — a "
        "separate egress boundary from the browser payload, and its session title is "
        "user-controlled, so the rendered notice is re-scanned through the shared "
        "display_safe sink before it reaches the transport.",
    ),
    (
        "Channel session surfacing",
        "dashboard/channel_slots.py",
        "Titles and hydrated transcript of a Slack/Discord/Teams conversation as it is "
        "surfaced into a dashboard chat slot — the channel transport is a separate "
        "egress boundary, so its content is re-scanned before reaching the browser.",
    ),
    (
        "Session history (JSONL)",
        "history_append.py",
        "Redacted before persistence, so a leaked credential is not written to disk "
        "and replayed into later context.",
    ),
    (
        "Auto-skill update merge prompt",
        "history_consolidation_skills.py",
        "The existing live skill body is redacted before it enters the model prompt "
        "that merges a proposed update, and the model's merged output is scanned again.",
    ),
    (
        "Auto-skill pending candidate staging",
        "history_consolidation_skills.py",
        "LLM-authored descriptions, triggers, procedures, and generated scripts are "
        "redacted before SkillsLoader writes a pending create or update candidate.",
    ),
    (
        "Auto-skill direct publication",
        "history_consolidation_skills.py",
        "LLM-authored descriptions, triggers, and procedures are redacted before "
        "immediate live skill creation or direct refinement through SkillsLoader.",
    ),
    (
        "Side-panel stream",
        "dashboard/handlers/side.py",
        "StreamRedactor mirroring the main chat for the side-question stream.",
    ),
    (
        "Reply-thread stream and store",
        "dashboard/chat_threads.py",
        "Three boundaries of a reply thread on a mate chat message: the "
        "StreamRedactor on the mate's live reply (as the side panel's), the "
        "thread envelope (parent, surrounding chat and prior replies, which "
        "kiro-cli persists into its own session file), and every stored reply "
        "and quoted parent on its way out of the sidecar to the browser.",
    ),
    (
        "Steering file metadata",
        "dashboard/handlers/steering.py",
        "First-heading descriptions and display paths in the /api/steering listing "
        "and detail responses. Document CONTENT is deliberately NOT redacted: it "
        "populates the tab's editor and is written straight back on save, so "
        "redacting it would overwrite the user's own file with markers.",
    ),
    (
        "Telemetry spend ranking",
        "dashboard/handlers/telemetry.py",
        "Session titles attached to the conversations-by-spend rows of "
        "/api/telemetry/startup. `display_title` is model-authored, and this is a "
        "SECOND egress path for it alongside the slot snapshot — a title set "
        "through `api_chat_slot_resume` reaches the slot unredacted, so nothing "
        "upstream of this handler has scanned it.",
    ),
    (
        "OpenAI-compatible API",
        "dashboard/openai_compat.py",
        "Streamed and non-streamed completions served to third-party clients.",
    ),
    (
        "Slack messages",
        "slack/handler_runtime/delivery.py",
        "StreamRedactor on the live edit stream plus a full pass on the final posted " "message.",
    ),
    (
        "Ops Mission Control Slack board",
        "apps/builtins/ops_mission_control/backend/slack_out.py",
        "Incident titles, resources, and diagnoses mirrored to an ops channel. A "
        "separate egress boundary from the Slack messages sink: this text originates in a "
        "third-party provider's alarm payload (not a model turn) and lands in a "
        "channel whose audience is usually wider than the dashboard's.",
    ),
    (
        "Slack cron / notification posts",
        "slack/gateway_runtime/subagent_injection.py",
        "Job names, cron results, and error strings before they reach a channel.",
    ),
    (
        "Subagent results",
        "subagent_parts/run_output.py",
        "Task text and returned results before injection into the parent context or "
        "the dashboard.",
    ),
    (
        "Voice reply (TTS)",
        "voice_reply.py",
        "Spoken text is redacted before synthesis so a credential is never read " "aloud.",
    ),
    (
        "SEL audit log",
        "sel.py",
        "String fields are redacted before an event is forwarded to a log "
        "integration; on-disk records are redacted by each caller before `log` "
        "(the writer signs bytes as-written, so it cannot redact them itself).",
    ),
    (
        "Task reports",
        "task_reporter.py",
        "Run names, titles, and bodies in generated task reports — "
        "exfiltration-URL scanning only, not the credential scanner.",
    ),
    (
        "Vector memory snippets",
        "vector_memory.py",
        "Search-result snippets before they are surfaced or re-embedded.",
    ),
    (
        "Workflow injections",
        "dashboard/workflow_inject.py",
        "Workflow progress summaries injected back into a session.",
    ),
    (
        "Structured monitor wakes",
        "monitoring/controller.py",
        "Provider-controlled canonical facts and operator-authored wake instructions "
        "before they are injected into an agent session. The complete bounded envelope "
        "passes the exfiltration-URL and credential scanners before dispatch.",
    ),
    (
        "Onboarding import",
        "onboarding_scan.py",
        "Text imported from a foreign agent's config (instructions, memories, skills, "
        "settings and the other import categories) before it enters Kiro Crew.",
    ),
    (
        "Discord / Telegram / WeCom / Webex",
        "messaging/driver.py",
        "The channel-neutral TurnDriver's StreamRedactor — every non-Slack chat "
        "channel inherits redaction from this one egress.",
    ),
    (
        "Hook replies and memory refusals (shared channel pipeline)",
        "messaging/dispatch.py",
        "A user-defined `on_message` hook can answer a turn instead of the model, "
        "which SHORT-CIRCUITS the turn and so never reaches the redactor in "
        "messaging/driver.py that every other reply on this pipeline inherits. "
        "The hook's text is arbitrary (it is user code, and it may quote the "
        "inbound message or a command's output back), so it goes through the "
        "shared credential + exfiltration-URL chain here, at the one point the "
        "reply leaves for a channel. This module also carries a NON-egress site, "
        "the session-directive consumer's confirmation log line, which scrubs the "
        "same LLM-derived text before it reaches the gateway log; it is named here "
        "rather than allowlisted separately because a module gets one "
        "classification and the egress one is the load-bearing half. Member-memory "
        "refusals also remove local paths before truncation and channel delivery.",
    ),
    (
        "Outbound raster payloads",
        "messaging/outbound_files.py",
        "Exact raster bytes go through platform.binary_content_is_flagged -- the "
        "one binary-content scan every file-delivery gate shares -- before upload, "
        "so this leg and those gates give one answer to one question.",
    ),
    (
        "Slack member-memory refusal",
        "slack/transport_dispatch.py",
        "Member-memory errors bypass the streamed response. Credentials, "
        "exfiltration URLs and local paths are removed before the bounded "
        "refusal is sent to the channel.",
    ),
    (
        "Discord direct send",
        "discord/transport_dispatch_commands.py",
        "The direct-send path that bypasses TurnDriver, redacted independently.",
    ),
    (
        "Discord transformed image text",
        "discord/renderer.py",
        "Image-markup removal can join previously separated text into a credential; "
        "the transformed body is re-scanned before Discord receives it.",
    ),
    (
        "Teams rendered message text",
        "teams/renderer.py",
        "Every LLM-authored string this channel sends, re-scanned in the form Teams "
        "will RENDER rather than the bytes handed over: the answer body, an "
        "`[OPTIONS:]` chip label, the tool-progress bubble, and an approval card's "
        "tool title and purpose. Teams markdown-renders all of them, so markup the "
        "platform removes can rejoin a credential the TurnDriver's byte-level stream "
        "scan saw as broken -- `AKIA**...**` is two fragments to the scanner and one "
        "key on screen. Card text matters as much as message text here, because a "
        "card's TextBlock is rendered the same way.",
    ),
    (
        "Teams attachment name",
        "teams/attachments.py",
        "The display name on an inline image, whose two sources are BOTH "
        "LLM-authored: the markdown alt text and, when that is empty, the "
        "basename of the model-written path. Extraction has already cut the path "
        "out of the answer body, so for an empty caption this name is the only "
        "surviving sink -- and the sanitizer preserves `[A-Za-z0-9._-]`, every "
        "character an `AKIA...` key id or a `ghp_...` token needs. Both the source "
        "and the finished name are scanned, because the length cap can slice a "
        "token down to a prefix the scanner no longer matches.",
    ),
    (
        "Telegram rendered widget text",
        "telegram/renderer.py",
        "The two LLM-authored strings this channel renders outside the answer "
        "body: an approval prompt's tool title (sent as markdown) and every "
        "`[OPTIONS:]` inline-keyboard label. Both are rendered, so markup the "
        "platform removes can rejoin a credential the TurnDriver's byte-level "
        "stream scan saw as broken. Scanned at the one keyboard builder both "
        "callers pass through.",
    ),
    (
        "Discord attachment description",
        "discord/client.py",
        "The accessible description on an uploaded image, from its markdown alt "
        "text. Extraction unescapes that text before the wire, reassembling a "
        "backslash-escaped credential the TurnDriver's stream scan never saw "
        "contiguously -- it is re-scanned here, on the form that actually leaves.",
    ),
    (
        "Telegram failure reason",
        "telegram/transport_dispatch.py",
        "The bounded failure reason a permanent AcpError surfaces in the chat "
        "reply instead of the generic retry text. The message is backend error "
        "text rather than stream output, so it bypasses the shared TurnDriver "
        "redaction and is scanned (credentials, exfiltration URLs, local "
        "paths) at this egress before the renderer posts it.",
    ),
    (
        "Telegram rendered-form seal",
        "telegram/renderer_display.py",
        "Every text this channel posts, scanned against the form Telegram RENDERS "
        "rather than the bytes sent. The TurnDriver's stream scan runs before this "
        "renderer introduces any markup, so it cannot see a credential the markup "
        "then reassembles: `AKIA**...**` matches nothing at the byte level, and "
        "`_md_to_telegram_html` turns it into `AKIA<b>...</b>`, which the client "
        "displays as an intact key. A link and a zero-width character between the "
        "halves do the same. Both live-frame and seal run "
        "`display_safety.redact_for_display` ahead of any tag being introduced, "
        "covering the HTML, Rich-Message and plaintext branches at once; the "
        "reasoning blockquote and the restored upload markup run it too.",
    ),
    (
        "Recent-sessions read audit",
        "messaging/sessions_view.py",
        "The exception text recorded when the collector fails to read the sessions "
        "directory, which goes into a SEL audit record every surface's session list "
        "shares. The message is filesystem error text and can quote a path or an "
        "environment value, so it is redacted before truncation — truncating first "
        "would let a cut split a credential pattern out of the matcher's reach.",
    ),
    (
        "LLM-generated session title",
        "messaging/auto_title.py",
        "The 3-6 word title a background turn proposes for a session. The model "
        "writes it FROM the conversation, so it can quote a credential or a beacon "
        "URL the user pasted, and the title then lands in three places at once — "
        "the conversation log, the channel's own thread/chat title, and the "
        "dashboard sidebar. Redacted here, before the cap, because the cap is what "
        "would otherwise split a credential pattern out of the matcher's reach; and "
        "here rather than at each caller, because a title reaching one surface "
        "unredacted is the whole failure.",
    ),
    (
        "Channel keyword-command replies",
        "messaging/commands.py",
        "The reply text of the path-independent chat commands — a cron job's name, "
        "schedule and message, a subagent's task, a task-runner spec path and a "
        "start failure. Each is free-form text a user typed or the agent proposed, "
        "and every reply is posted to a channel AND persisted to the conversation "
        "log, so the credential + exfiltration-URL pair runs before the string "
        "leaves the module rather than at each channel's own boundary.",
    ),
    (
        "Discord session-resume replay",
        "discord/session_resume.py",
        "Session titles in the `!sessions` picker and the transcript replayed when a "
        "dashboard session is resumed — content authored in one surface is re-scanned "
        "before it crosses into Discord, which is a separate egress boundary. Also "
        "neutralizes `@` mentions so replayed text cannot ping a server.",
    ),
    (
        "Outbound channel messages",
        "dashboard/handlers/messaging.py",
        "Recursively redacts outbound message payloads before they leave the gateway.",
    ),
    (
        "Outbound channel messages — send_message post leg",
        "dashboard/messaging_api/proactive_send.py",
        "The Slack post of a proactive send and the in-place edit: the text and Block Kit "
        "blocks are display-redacted before they are posted to Slack.",
    ),
    (
        "Streaming speech-to-text",
        "dashboard/stt_stream.py",
        "Transcription partials and finals are redacted before they leave the process.",
    ),
    (
        "Agent channels",
        "channel.py",
        "Tool names and tool inputs shared between agents on a persistent channel.",
    ),
    (
        "App lifecycle output",
        "apps/routes.py",
        "Install/start/stop script output and warnings surfaced from an app.",
    ),
    (
        "App teardown output",
        "apps/teardown.py",
        "Output from an app's own onDisable script, scrubbed by the dual-pass "
        "redact() helper before it becomes a warning on the disable and "
        "trust-revocation responses.",
    ),
    (
        "App activity log",
        "apps/builtins/mochi/activity_log.py",
        "Agent-authored activity entries are redacted before persistence, for the "
        "same reason as the session JSONL: the file is served back over the app's "
        "activity route AND read into a later prompt, so an unredacted credential "
        "would be written to disk and then replayed. Redaction sits at the single "
        "write point rather than at each caller.",
    ),
    (
        "App plan endpoint",
        "apps/builtins/mochi/backend/routes.py",
        "The agent-authored plan queue (update_plan) is served to the dashboard "
        "over the app's /plan route; a credential or webhook URL an LLM wrote into "
        "a narrative/task field is recursively redacted here before json_response, "
        "the same output-boundary reason as the app activity log.",
    ),
    (
        "Shared options-overflow sink",
        "messaging/renderer.py",
        "The one place LLM-authored [OPTIONS:] choices that do not fit a channel's "
        "interactive widget are written into the message BODY (format_overflow, "
        "reached from apply_options_cap on slack/telegram/discord). That crosses a "
        "boundary the widget path does not: a widget label is plain text, while the "
        "body is markdown-parsed, so a key split by a code span, emphasis or a "
        "Discord spoiler is broken to every byte-level scan -- including the "
        "TurnDriver's streaming redactor -- and whole on screen once the platform "
        "drops the delimiters. Choices are therefore redacted here in DISPLAY form "
        "(messaging/display_safety.py) BEFORE mention syntax is defanged, since the "
        "ZWSP insertion is itself a post-scan transformation. Enforced at this sink "
        "rather than per renderer for the same reason the max_buttons cap lives in "
        "shared code: a channel cannot forget what it does not call.",
    ),
    (
        "Webex delivery",
        "webex/renderer.py",
        "The Webex DELIVERY boundary. Webex renders markdown, so it reassembles a "
        "credential the driver's literal-byte scan of the provider stream could "
        "not match contiguously (`AKIA**IOSF**ODNN7EXAMPLE`, a link target broken "
        "by emphasis) -- and this channel adds two egress paths of its own that "
        "carry the same text: the numbered `[OPTIONS:]` fallback posted when an "
        "Adaptive Card send fails, and the local-path references restored when an "
        "upload fails. The final answer is therefore re-scanned through "
        "redact_for_display (messaging/display_safety.py) on the DISPLAYED form, "
        "with the same redactor pair TurnDriver streams through. Deliberately "
        "without the mention defang the shared `display_safe` adds: that "
        "neutralizes `@everyone`-style broadcast grammars, Webex has none, and "
        "this channel's allow-list is email addresses -- so defanging would mangle "
        "every address the agent legitimately prints.",
    ),
    (
        "iMessage delivery",
        "imessage/renderer.py",
        "The iMessage DELIVERY boundary. This channel is the one that collapses "
        "markdown itself -- `to_plaintext` in `delivery_text` -- because the "
        "surface renders no markup, so the driver's literal-byte scan of the "
        "provider stream runs BEFORE the transformation that can reassemble a "
        "credential it could not match (`**AKIA**IOSFODNN7EXAMPLE`, a link "
        "target broken by emphasis). `delivery_text` therefore re-scans the "
        "flattened form through redact_for_display (messaging/display_safety.py) "
        "with the same redactor pair TurnDriver streams through, which is the "
        "only form that actually ships. Elsewhere in this package "
        "`redact_handle` appears solely in log lines, which is why the sibling "
        "modules are listed as non-egress and this one is not.",
    ),
    (
        "WeCom reasoning blocks",
        "wecom/renderer.py",
        "The reasoning WeCom renders inside its native `<think></think>` block. "
        "`TurnDriver` redacts each thinking chunk, but with a plain per-chunk pass "
        "rather than the rolling `StreamRedactor` it uses for the answer -- so a "
        "credential split across two chunks passes both halves and is reconstituted "
        "by the renderer when it joins them for the frame. The assembled string "
        "therefore goes through the shared credential + exfiltration-URL chain at "
        "the send boundary, which is the only form that ships and also covers a "
        "credential that was never split. The ANSWER text needs no pass here: it "
        "reaches this renderer already through the driver's rolling redactor.",
    ),
    (
        "Feishu answer body",
        "feishu/renderer.py",
        "The answer body Feishu renders as markdown. `text()` is what `on_done` "
        "sends, and Feishu is a buffered zero-widget channel: its body reaches "
        "`render_options_as_text` -> `apply_options_cap`, which display-redacts the "
        "numbered OPTIONS choices but returns the body itself unscrubbed, so the "
        "only pass the body had was the driver's channel-neutral LITERAL stream "
        "scan. A credential split by emphasis (`AKIA**REST`) or a link therefore "
        "survives that scan and is reassembled when Feishu renders the markdown. "
        "The body is screened at the send boundary through `Renderer.redact_for_"
        "target` -- the `redact_for_display` pass over the credential + "
        "exfiltration-URL chain, on the display form that actually ships. `text()` "
        "is where the scrub lands because `text()` is what `on_done` sends; history "
        "is persisted separately by `messaging.dispatch` from the driver's own "
        "accumulated text, so this pass covers the shipped bytes without touching "
        "the transcript.",
    ),
    (
        "Slack attachment titles and filenames",
        "slack/files.py",
        "The two upload sinks that are NOT the message body: an attachment's "
        "title comes from LLM-authored alt text, and its filename from a local "
        "path the model chose. Both reach Slack as their own fields, so neither "
        "is covered by the body's render pipeline, and a filename is re-scanned "
        "AFTER sanitizing because collapsing the unsafe characters can rejoin a "
        "credential the original had broken up.",
    ),
    (
        "Typed-reply approval prompt",
        "messaging/approval.py",
        "The numbered tool-approval prompt for a channel with no interactive "
        "widget (max_buttons=0). Both fields it interpolates are AGENT-AUTHORED: "
        "the model chooses the tool name and writes the purpose, so either can "
        "carry a credential or an exfiltration URL, and the prompt puts them "
        "straight into a chat message. The screen runs in build_approval_prompt "
        "rather than at each channel's sink because that is why the helper is "
        "shared: a channel adopting the ladder inherits the guarantee instead of "
        "re-deriving it, and a caller that forgets it leaks on a security prompt. "
        "A channel that screens again at its own sink is merely redundant.",
    ),
    (
        "WhatsApp render pipeline",
        "whatsapp/renderer.py",
        "The WhatsApp RENDERING boundary. This is the only markup-CONSUMING "
        "channel whose own converter rewrites the delimiters: `to_whatsapp_text` "
        "turns `AKIA**I**OSFODNN7EXAMPLE` into `AKIA*I*OSFODNN7EXAMPLE`, which "
        "matches no credential pattern as written while the reader's client "
        "strips the markers and shows an intact key. It strips ANSI and then runs "
        "redact_for_display (messaging/display_safety.py) over the "
        "redact_exfiltration_urls + redact_credentials pair, and the pass that "
        "carries the guarantee is the one AFTER the conversion: the module also "
        "reduces `<thinking>` blocks, pipe tables and mermaid fences, each of "
        "which deletes a span and so joins whatever sat on either side of it, "
        "which a scan of the authored form cannot see. A second pass runs before "
        "the conversion as a belt. `render_chunks` runs the whole pipeline before "
        "the splitter, so a credential cannot be cut into an unmatchable prefix. "
        "`display_safe_text` is the same screen without the conversion, for the "
        "already-dialect sinks (a file-rejection note, an image caption, the "
        "approval prompt) that `whatsapp/turn_renderer.py` puts on the wire.",
    ),
    (
        "Weixin steer receipts",
        "weixin/turn_renderer.py",
        "The in-answer steer chip (`> \u21aa\ufe0f {summary}`). A steer arrives "
        "separately from the provider stream, so it never passes `TurnDriver`'s "
        "rolling redactor, and it is interpolated into the message BODY -- which "
        "the platform markdown-parses, so a credential split by a code span or "
        "emphasis is whole on screen while a byte-level scan saw it broken. The "
        "summary therefore goes through the shared DISPLAY-form redactor at the "
        "point of interpolation: `Renderer.redact_for_target`, which is only the "
        "`redact_for_display` pass -- it re-scans the markdown-rendered form with "
        "the credential + exfiltration-URL chain, and deliberately does not repeat "
        "the driver's rolling stream scan, which a one-shot summary has no split "
        "chunks to need. The ANSWER text needs no pass here at all: it reaches this "
        "renderer already redacted by the driver.",
    ),
    (
        "Slack render pipeline",
        "slack/format.py",
        "The Slack RENDERING boundary: text that is converted to mrkdwn goes "
        "through render_for_slack / render_one_for_slack, and a build gate "
        "(test_slack_render_pipeline.py) fails if any module calls "
        "to_slack_mrkdwn itself. Both helpers strip ANSI and run "
        "redact_via_context BEFORE conversion and again after, because neither "
        "ordering is safe alone -- the ANSI strip inside to_slack_mrkdwn "
        "reassembles a credential the escapes had broken up, while its 39k "
        "self-truncation cuts one into an unmatchable prefix. Text is pre-split "
        "below that ceiling so conversion never truncates, and OPTIONS choice "
        "labels are redacted before the Block Kit slices. SCOPE, stated exactly: "
        "the gate polices the CONVERSION primitive, not the Slack API calls -- a "
        "path that posts raw text without ever converting it is not covered here "
        "and relies on its own redaction.",
    ),
    (
        "Slack session mirror",
        "dashboard/chat_slack.py",
        "Thread titles and the conversation history seeded into a newly linked "
        "thread. Titles go through redact_and_truncate (redaction BEFORE "
        "truncation, so a truncation boundary cannot split and hide a "
        "credential); history is delegated to the shared Slack render pipeline "
        "above with redact_via_context injected as its redactor.",
    ),
    (
        "Configured-channel session mirror",
        "dashboard/chat_mirror.py",
        "Recent dashboard context posted while linking a configured non-Slack "
        "destination, via redact_via_context before transport dispatch, then "
        "chunked to the channel's own message limit rather than truncated.",
    ),
    (
        "Unattended channel egress",
        "messaging/egress.py",
        "Text pushed to a non-Slack channel without a renderer: the dashboard reply "
        "mirror, subagent and cron results, and the compaction notice. Control tags "
        "are stripped, the text is redacted in display form through "
        "redact_via_context, and each transport chunk is seam-checked with the same "
        "redactor before send.",
    ),
    (
        "Slack Block Kit views",
        "slack/blocks.py",
        "Message text rendered into Block Kit payloads (Home Tab, session views).",
    ),
    (
        "File cards broadcast to the browser",
        "dashboard/handlers/files.py",
        "The file-card JSON pushed over the chat WebSocket is redacted before "
        "broadcast. (Apart from the owner's file read and diff on the next row, "
        "this module's other redact() calls are upload GATES — they abort a send "
        "when redaction would alter the content — not egress.)",
    ),
    (
        "File viewer read and diff for the owner",
        "dashboard/handlers/files.py",
        "The file body returned by the dashboard's file read (api_file_read) — "
        "the chat side panel's Files tab, and the Library and Artifacts session-"
        "document previews and re-reads that use the same route — and the "
        "original and diff bodies returned by the matching file diff "
        "(api_file_diff), which the panel compares that buffer against. Both "
        "scanners run for every caller EXCEPT the dashboard owner with the "
        "credential-redaction switch turned off (Settings → Security → "
        "Credential redaction in file views): then these two responses run the exfil-URL "
        "pass alone, in the owner's own dashboard only, with one verdict per "
        "request so neither side is raw while the other is masked. The switch "
        "defaults to on, "
        "is stored on the keystone floor the agent cannot write, and every flip "
        "is audited.",
    ),
    (
        "File watch frames",
        "dashboard/file_api/transfer.py",
        "The file-watch SSE frames (api_file_watch) are redacted before they are "
        "pushed. The file download and the Range stream serve the file's own "
        "bytes and use redact() as a GATE: a file whose text the redactor would "
        "change is refused, not served masked.",
    ),
    (
        "Office document preview",
        "dashboard/file_api/office_preview.py",
        "The text, blocks and slides the Office preview (api_file_office_preview) "
        "returns are redacted before they are capped.",
    ),
    (
        "Spreadsheet preview",
        "dashboard/file_api/sheet.py",
        "The cell values and formula text the spreadsheet preview "
        "(api_file_sheet) returns are redacted.",
    ),
    (
        "Project branch label",
        "dashboard/file_api/project_dirs.py",
        "The branch name, short HEAD and project path the branch label "
        "(api_project_git) returns are redacted; the path goes through "
        "_redact_project_path, which the Git panel and the file tree share.",
    ),
    (
        "Git panel status and log",
        "dashboard/file_api/git_panel.py",
        "The branch, file paths, commit messages and authors the Git panel's "
        "status and log (api_project_git_status, api_project_git_log) return are "
        "redacted; file paths go through redact_path_segments.",
    ),
    (
        "Project file tree",
        "dashboard/file_api/project_tree.py",
        "The root the Files tab's tree listing (api_project_tree) returns goes "
        "through _redact_project_path, and every listed path is redacted segment "
        "by segment through redact_path_segments.",
    ),
    (
        "Slide-deck render screen and conversion diagnostic",
        "dashboard/handlers/office_slides.py",
        "The .pptx/.ppt slide renderer screens the deck's rendered text through "
        "the credential redactor then the exfiltration-URL redactor BEFORE it "
        "rasterizes any page (`_screen_rendered_text`): a slide PNG is a picture "
        "of that text and cannot be redacted after the fact, so a deck whose "
        "visible text either scanner would change is not rendered at all -- the "
        "manifest answers `content_redacted` and the panel falls back to the "
        "text outline, which the file-preview route redacts per slide. The same "
        "credential-then-URL chain also scrubs the conversion child's stderr "
        "tail before it is logged or returned as `detail`. Both scanners run "
        "over the full text; a scanner that raises drops the field rather than "
        "emitting it, and the render is refused rather than served unredacted.",
    ),
    (
        "MCP custom server specs",
        "dashboard/handlers/mcp_custom.py",
        "Editable MCP server specs returned by the dashboard HTTP API to the browser. "
        "Configured header values receive credential redaction only before they cross "
        "that boundary.",
    ),
    (
        "MCP probe results",
        "dashboard/handlers/mcp_probe.py",
        "Cached MCP probe results returned by the dashboard HTTP API to the browser. "
        "Configured header values and reflected credentials in probe errors receive "
        "redaction before they cross that boundary.",
    ),
    (
        "MCP apply errors",
        "dashboard/handlers/mcp.py",
        "Uninstall and agent-rebuild errors the apply route returns: URL, then credential.",
    ),
    (
        "MCP server metadata",
        "mcp_discovery_server_info.py",
        "McpServerInfo.to_dict() is the serialization boundary for every dashboard "
        "MCP listing; header values and reflected credentials in probe errors are "
        "redacted there before the payload leaves the backend.",
    ),
    (
        "MCP app tool results",
        "dashboard/handlers/mcp_apps.py",
        "Recursively redacts every string leaf of an MCP app's tool result before "
        "it reaches the browser.",
    ),
    (
        "MCP app render payloads",
        "mcp_apps_render.py",
        "Recursively redacts string leaves of a rendered MCP app payload.",
    ),
    (
        "Auto-skill pending detail / promotion",
        "skills_review.py",
        "LLM-authored auto-skill candidate content (SKILL.md, scripts, and nested "
        ".meta.json values/keys) is redacted at the pending detail-read choke "
        "before it is returned by the dashboard skills API, and again in-place "
        "before an approved candidate is promoted to the live skills dir.",
    ),
    (
        "Ops provider evidence",
        "apps/builtins/ops_mission_control/backend/registry.py",
        "Third-party ops payloads (CloudWatch log lines, Datadog monitor context, "
        "and any companion-contributed EvidenceSource) are redacted at the single "
        "gather_evidence choke before they reach a model prompt, a transcript, or "
        "Slack — centrally, so an adapter author cannot leak a credential by "
        "forgetting to redact. Routed through redact_via_context, so a loaded "
        "companion's own credential patterns apply and a host that fails to compose "
        "one fails closed rather than silently falling back to public patterns.",
    ),
    (
        "Ops shared-ledger push",
        "apps/builtins/ops_mission_control/backend/ledger_sync.py",
        "The LAST gate before this app's one published artifact leaves the machine. "
        "`ledger.jsonl` is committed and pushed to the team's git remote, so a "
        "credential sitting in a legacy row — written by an older build, or by any "
        "path other than the redacting POST /ledger route — would be fetched by every "
        "teammate and require a history rewrite to recall. The pre-push scan takes the "
        "UNION of `security.get_credential_patterns()` and this app's own "
        "`secrets.redact_tokens`, because neither is a superset: the core patterns "
        "carry AKIA/ASIA and miss a prefixed Datadog application key, while "
        "redact_tokens knows the provider shapes and misses an AWS access key id. A "
        "flagged line REFUSES the push rather than redacting it in place — the ledger "
        "is the operator's own knowledge and silently rewriting it would destroy the "
        "lesson — and the refusal reports line NUMBERS only, never the matched text, "
        "since it is logged to SEL and the console.",
    ),
    (
        "Ops investigation brief",
        "apps/builtins/ops_mission_control/backend/dispatch.py",
        "The signal's own provider-controlled metadata — title, resource and provider "
        "URL — is redacted before it is rendered into the investigation brief, which "
        "goes into the agent's context and from there into the transcript and any "
        "session artifact. A signed webhook is accepted from anything able to POST "
        "JSON and a console link can carry a token in its query string, so this "
        "metadata is exactly as untrusted as the evidence bodies gather_evidence "
        "already covers, so it is redacted the same way. Routed through redact_via_context for the same companion-seam "
        "reason. Fields this app assigns (source, severity, fired_at, fingerprint, "
        "operating_mode) are deliberately not redacted: masking one could only "
        "corrupt a value the agent needs to reason about.",
    ),
    (
        "Ops knowledge ledger",
        "apps/builtins/ops_mission_control/backend/routes.py",
        "The learned pattern/fix pair is redacted on the WRITE path (POST /ledger), "
        "before the content-addressed id is computed. This is the app's only artifact "
        "that leaves the machine: ledger_sync commits ledger.jsonl to a shared git "
        "remote, and a 'fix' field is the likeliest place for a pasted credential "
        "because a command line is what a fix looks like. Write-path rather than "
        "sync-path because the entry is on local disk and in the vector index long "
        "before any sync runs, and an operator who enables sync later would otherwise "
        "retroactively publish everything written before it. ledger_sync.push() adds a "
        "second, independent refusal for entries that predate this redactor.",
    ),
    (
        "Ops Mission Control desktop notifications",
        "apps/builtins/ops_mission_control/backend/notify_out.py",
        "Incident titles and provider failure reasons pushed onto the local "
        "notification bus. A separate egress from the Slack board even though the "
        "source text is the same third-party provider payload: this one lands in the "
        "OS notification centre and in the persisted notification JSONL. Runs the "
        "credential and exfiltration-URL scanners plus the app's own provider-token "
        "pass, matching the postmortem writer — core redaction alone leaves a "
        "provider api_key inside a URL intact.",
    ),
    (
        "Ops Mission Control incident postmortem",
        "apps/builtins/ops_mission_control/backend/store.py",
        "The per-incident Markdown artifact written when an incident closes "
        "(incidents/<id>.md). A distinct boundary from the two above because the "
        "file is a SHAREABLE local artifact — it exists so an operator can hand a "
        "colleague, or a ticket, the investigation record — so its provider titles "
        "and model-authored diagnosis are redacted at the write, not at a read.",
    ),
    (
        "Diagnostics support bundle",
        "diagnostics.py",
        "The redacted zip built by `kirocrew doctor --bundle` and Settings › About › "
        "Report a Problem, plus the pre-filled GitHub issue URL it returns. The most "
        "external boundary in this list: the artifact exists to be attached to a "
        "PUBLIC issue, and its members are raw gateway/kiro-cli logs and crash "
        "reports. Every text member and the user-typed note run the credential and "
        "exfiltration-URL scanners plus a sensitive-header pass before anything is "
        "written into the archive.",
    ),
    (
        "Connections L1 smoke report",
        "connections/l1_smoke.py",
        "The authorized-grant sweep's JSON verdict report: written to disk by "
        "`_persist_report`, echoed to stdout by `_echo`, and uploaded by the "
        "nightly lane as a build artifact — so it reaches CI logs and every "
        "reader of the repository, not just the operator who ran it. The "
        "credential-bearing part is the provider's own error text, scrubbed by "
        "`redact_mcp_error` as it enters a verdict row (both site-wide scanners "
        "plus the configured header-value pass), at the write rather than at a "
        "read, because the report outlives the process that made it.",
    ),
    (
        "Tag definitions (HTTP + auto-tag)",
        "dashboard/chat_tags_vocabulary.py",
        "Tag names supplied by both the POST /api/chat/tags HTTP handler and the "
        "background auto-tag task are LLM-authored or project-derived and persist "
        "to tags.json, the dashboard sidebar, and Slack notifications. Both paths "
        "redact credentials and exfiltration URLs before creation/persistence.",
    ),
    (
        "Background auto-tag (project-derived names)",
        "dashboard/chat_auto_tag.py",
        "The background auto-tag task derives tag names from the slot's project "
        "path and persists them to tags.json and the dashboard sidebar via the "
        "shared tag-creation path. Names are passed through redact_credentials "
        "and redact_exfiltration_urls before resolution or persistence.",
    ),
    (
        "Session-pulse survey feedback (Aperture egress)",
        "dashboard/handlers/feedback.py",
        "The free-text `feedback` field submitted via POST /api/feedback/submit is "
        "forwarded to Aperture, a third-party AWS service, so it is a genuine "
        "external egress boundary — a user typing a credential or exfiltration URL "
        "while describing their experience would otherwise leave the host "
        "unredacted. `_customer_responses` runs it through redact_exfiltration_urls "
        "then redact_credentials before it is included in the outbound payload. "
        "`email` is run through that SAME pass (redact_exfiltration_urls then "
        "redact_credentials) because a user could paste a credential into it; its "
        "`pii: True` marker is a separate Aperture disclosure flag, not a "
        "substitute for redaction. `rating` (a fixed frontend enum) is not run "
        "through this pass.",
    ),
    (
        "Auto Triage Pipeline dashboard strings",
        "apps/builtins/issue_radar/backend/pipeline_fold.py",
        "Every string this read-only fold hands to its routes -- issue titles, "
        "assignee and author logins, labels, event names, slot keys -- funnels "
        "through one `_printable` helper before serialization, and the routes render "
        "it in the dashboard. The titles and labels are NOT our text: they come from "
        "the forge, where any user can open an issue and write anything in the "
        "title, so this is a dashboard-bound sink for attacker-controlled text. "
        "`_printable` runs the shared credential + exfiltration-URL chain FIRST, "
        "then neutralizes control and bidirectional-override characters, and "
        "truncates LAST -- redaction has to precede truncation, because cutting "
        "first can split a credential so only its tail is left to match and the head "
        "survives into the output.",
    ),
    (
        "AWS Control error responses",
        "apps/builtins/aws_control/backend/routes.py",
        "Error text returned by the aws-control builtin's HTTP surface. The app "
        "shells out to the AWS CLI (via the deploy engine chokepoint), and a "
        "failed call's stderr can quote back a `credential_process` command "
        "line, an SSO URL, a role ARN, or an endpoint override carrying inline "
        "credentials. Every outbound error string (the `aws_call_failed` "
        "bodies, the bill card's `fetchError`, backup's `remoteError`, and the "
        "library `not_pushable` message) funnels through one `_safe_error` "
        "chokepoint that applies the deploy handlers' credential + "
        "exfiltration-URL chain before the text reaches the dashboard.",
    ),
    (
        "Job runtime error responses",
        "apps/job_routes.py",
        "Error text returned by the shared `_jobs/*` HTTP surface that every app's "
        "job runtime is served through. The SDK scrubs what a RUNNER produces, but "
        "the refusal this surface raises itself quotes caller-supplied material -- "
        "a job kind name, a dedupe key, or a path carried out of the SDK's own "
        "exception -- so the `job_start_failed` body funnels through one `_safe` "
        "chokepoint that applies the credential + exfiltration-URL chain before the "
        "text reaches the dashboard.",
    ),
    (
        "Peer crew text",
        "dashboard/peer_redaction.py",
        "Every string a connected peer crew sends that this dashboard renders, "
        "broadcasts or persists: proxied replies (JSON documents and SSE events), "
        "capability labels and session-list fields. The peer is a SEPARATE "
        "machine with its own agents, environment and secrets, so its text can "
        "quote a credential or an exfiltration URL that no local redaction pass "
        "has ever seen. The peer redacts its own copy with this same chain, but a "
        "hub that trusted that would inherit whatever an older or misconfigured "
        "peer failed to scrub -- so every such string runs the credential + "
        "exfiltration-URL chain here, through the one sink.",
    ),
    (
        "Host-side UI preference backup",
        "ui_prefs.py",
        "Every value read back from `~/.kiro/crew/ui-prefs.json` before it is "
        "served by `GET /api/ui-prefs` and written into the renderer's "
        "localStorage. The values are opaque strings the server never parses, "
        "and the file sits in the agent-writable data home, so a credential or "
        "exfiltration URL smuggled inside an innocent key would otherwise reach "
        "the dashboard verbatim. A value the credential + exfiltration-URL chain "
        "would alter is DROPPED rather than served redacted: a redacted "
        "preference is not one the client can use, and the sync loop would "
        "write the sentinel back over the file.",
    ),
    (
        "WakaTime coding-activity heartbeats",
        "wakatime/heartbeats.py",
        "The project label sent as the heartbeat entity and project fields when "
        "WakaTime send-heartbeats is enabled. The label is the agent/user-selected "
        "project directory basename, so a directory whose name is itself "
        "credential- or URL-shaped would otherwise be POSTed verbatim to WakaTime. "
        "The basename passes through the shared credential + exfiltration-URL chain "
        "in `_entity_for_project` before it can leave for the external API.",
    ),
    (
        "Crew webview panels",
        "agent_panel.py",
        "Everything a crew publishes into its drawer webview: every string in the "
        "`data` object -- KEYS as well as values, because the template renders a "
        "key as a heading -- plus the panel title. A panel is assembled unattended "
        "from issue bodies, review comments and command output, so it is untrusted "
        "text on a path that ends at the operator's dashboard, both in the native "
        "docked summary and inside the composed sandbox document. Each string is "
        "Unicode-sanitized BEFORE the chain, because a redactor matches the "
        "characters it is handed: an invisible character inside a token hides it "
        "from both patterns and is then dropped downstream, which would store the "
        "joined-up secret with redaction skipped. The chain runs at "
        "PUBLISH rather than at render, so a credential never enters `panel.json` at "
        "all: it cannot be read back by a later reader, cannot survive in halves "
        "across the record's byte ceiling, and is not left on disk for the next "
        "reader that forgets to scrub.",
    ),
    (
        "Member event-log projection broadcast",
        "eventlog/service.py",
        "Folded member projection views pushed to the browser over the "
        "member-projection WS on every change. A projection carries "
        "agent-authored free-text (an activity record's `project`, message "
        "previews) that would otherwise cross to the dashboard verbatim, the "
        "same class the sibling `/activity` HTTP read redacts. "
        "`_redact_projection_value` runs the shared exfiltration-URL then "
        "credential chain over the view before broadcast, plus the "
        "URL-secret-parameter layer the sibling roster egress runs, so a short "
        "credential carried as a URL query parameter cannot slip the two "
        "heuristics; it is applied at the "
        "network boundary rather than the fold so the stored projection keeps "
        "its raw value for server-side folds while nothing leaves unredacted.",
    ),
    (
        "Process-tree diagnostics",
        "diag/procs.py",
        "The command line, working directory and four environment values of every "
        "process in the Kiro Crew family, rendered into the read-only process view "
        "that answers the dashboard and the debug MCP tool. Every one of those "
        "fields belongs to the process being inspected rather than to this code, so "
        "each passes the shared credential redactor BEFORE it is clipped to its "
        "retention cap -- the same order the sinks above state, and for the same "
        "reason: a cut taken first can halve a secret into a fragment neither "
        "pattern matches, which ships the surviving prefix in clear. The "
        "environment map is those four named keys and no others, and is omitted "
        "entirely unless a caller asks for it.",
    ),
    (
        "MCP launch approval display records",
        "mcp_gateway/launch_approval.py",
        "Command and argument display values persisted in the sealed approval store "
        "and returned by dashboard status. Both shared redactors run before storage, "
        "so raw launch values do not enter durable operator-facing state.",
    ),
    (
        "Mate-written dashboard field values",
        "dashboard_agentic.py",
        "Every string a mate writes into its own dashboard's agentic fields, on "
        "its way to the rendered page. The scrub runs on the way IN, so what the "
        "registry stores is already redacted and no later reader has to remember to "
        "scrub it. Both shared redactors run, exfiltration URLs first, and the walk "
        "is recursive over arrays and objects rather than a named list of fields, "
        "because a template declares its own agentic fields and a value's strings sit "
        "inside containers as often as they stand alone. An object's KEY is scrubbed "
        "beside its value, since a page draws a key as a label in the same characters "
        "it draws the value. The type and size questions are asked again about the "
        "scrubbed result, because a value that fit before the scrub can be over the "
        "cap after it.",
    ),
    (
        "Field values pushed to a mate's dashboard page",
        "dashboard/handlers/member_dashboard_fields.py",
        "Every subscribed field's value on its way to the owner's socket and into the "
        "page's data island. This is the ONE place such a value is produced -- the "
        "initial read and every later patch both come out of `_values` -- which is what "
        "makes one scrub here cover both rather than each sender remembering. The "
        "redactor is handed in by the route rather than chosen here, so the module "
        "applies the gateway's own redactor and cannot substitute a weaker one.",
    ),
)


# ── Credential families ──
# The credential CLASSES the redaction regex matches. Family names only — never a
# pattern that could be inverted into a generator, and never live secrets.
_CREDENTIAL_FAMILIES: tuple[tuple[str, str], ...] = (
    (
        "AWS access keys",
        "AKIA / ASIA key IDs, plus labelled secret-access-key and session-token forms",
    ),
    (
        "Private keys",
        "PEM blocks (RSA / DSA / EC / OPENSSH), including encrypted and truncated bodies",
    ),
    ("Slack tokens", "xoxb / xoxp / xoxa / xoxs bot, user, and app tokens"),
    (
        "GitHub tokens",
        "ghp / gho / ghu / ghs / ghr classic tokens and github_pat fine-grained tokens",
    ),
    ("GitLab tokens", "glpat personal access tokens"),
    ("Stripe keys", "sk_live / rk_live / sk_test / rk_test secret and restricted keys"),
    ("SendGrid keys", "SG. prefixed API keys"),
    ("OpenAI keys", "sk-proj project keys"),
    ("Anthropic keys", "sk-ant API keys"),
    ("npm tokens", "npm_ access tokens"),
    ("PyPI tokens", "pypi- API tokens"),
    ("DigitalOcean tokens", "dop / doo / dor v1 personal, OAuth, and refresh tokens"),
    ("Google OAuth secrets", "GOCSPX- client secrets"),
    ("Telegram bot tokens", "numeric bot id joined to a URL-safe secret"),
    ("JWT / JWE tokens", "3-segment signed and 5-segment encrypted compact tokens"),
    # Phrased WITHOUT a literal "Authorization: Bearer <token>" sequence: that
    # exact shape is what the scanner matches, so spelling it out here would make
    # this very description self-redacting wherever the payload is itself scanned
    # (the SEL audit log, a Slack-relayed posture summary) — the row would render
    # as "[REDACTED: credential]". A pinned test asserts the whole payload survives
    # redact_credentials() unchanged; keep new descriptions clear of live shapes.
    ("HTTP bearer tokens", "Bearer-scheme HTTP auth headers, including JSON-serialized log dumps"),
    (
        "Database URIs",
        "postgres / mysql / mongodb / redis / amqp connection strings with an embedded password",
    ),
    (
        "Base64-encoded variants",
        "40+ char base64 chunks are decoded and re-scanned against every family above",
    ),
)


# ── URL exfiltration heuristics ──
_EXFIL_HEURISTICS: tuple[tuple[str, str], ...] = (
    (
        "Credential in path or query",
        "Unconditional floor — an AWS key, PEM header, or Slack token anywhere in the "
        "URL is exfiltration regardless of destination.",
    ),
    (
        "Base64-encoded credential",
        "Base64 chunks in the query are decoded and re-scanned, so an encoded secret "
        "cannot slip past the literal-marker floor.",
    ),
    (
        f"Long query string (>= {security.exfil_query_min_len()} chars)",
        "A query large enough to carry a stolen payload.",
    ),
    (
        "Credential-like query data",
        "Base64 blobs and key-shaped values in query parameters.",
    ),
    (
        "Heavy percent-encoding",
        "20+ consecutive percent-encoded characters — an obfuscated payload. Applies "
        "to every host, including exempted ones.",
    ),
)


def _own_namespace_prefixes() -> tuple[str, ...]:
    """Home-relative prefixes that belong to KiroCrew / kiro-cli itself.

    Derived from the crew data-home prefixes, plus the ``.kiro`` parent that
    holds kiro-cli's own state and the gateway's auth staging dir. Used only to
    label a row in the posture view — a misclassification is cosmetic, never a
    gate decision.
    """
    return tuple({p.split("/", 1)[0] for p in security.crew_home_prefixes()})


def _sensitive_path_items() -> list[PostureItem]:
    """Credential paths blocked at the hook layer, classified by owner."""
    items: list[PostureItem] = []
    own = _own_namespace_prefixes()
    for entry in security.sensitive_home_dirs():
        # Path-boundary match, so a sibling like `.kirocrew-notes` is not counted
        # as ours just because it shares a string prefix.
        first = entry.split("/", 1)[0]
        if first in own:
            detail = (
                "Kiro Crew trust root — agent file tools can neither read nor write it; "
                "shell reach follows its sandbox disposition"
            )
        else:
            detail = "Third-party credential store"
        items.append(PostureItem(label=f"~/{entry}", detail=detail))
    return items


def _write_protected_items() -> list[PostureItem]:
    """Paths readable but not writable by agent tools."""
    return [
        PostureItem(
            label=f"~/{entry}",
            detail=(
                "Reads allowed; the agent's file-edit tool cannot modify it when the "
                "call declares the ACP edit kind. Shell writes are not covered by "
                "this control"
            ),
        )
        for entry in security.write_protected_home_paths()
    ]


def _denied_command_items() -> list[PostureItem]:
    """Built-in deny rules, described (not raw-regex) and grouped by category."""
    return [
        PostureItem(label=rule.description, detail=rule.category)
        for rule in security.BUILTIN_DENIED_RULES
    ]


def _suspicious_pattern_items() -> list[PostureItem]:
    return [PostureItem(label=pattern) for pattern in security.SUSPICIOUS_BASH_PATTERNS]


#: Every MCP tool-schema dispatch registry in ``validation``, by attribute name. A
#: tool is only validated if it is IN one of these, so this list defines what the
#: posture view can see. Keep it complete — ``test_security_posture`` reads the same
#: names, so an omission fails there rather than quietly shrinking the report.
_SCHEMA_REGISTRY_NAMES: tuple[str, ...] = (
    "MCP_CORE_SCHEMAS",
    "MCP_CREW_LOG_SCHEMAS",
    "MCP_CRON_SCHEMAS",
    "MCP_COMPUTER_SCHEMAS",
    "MCP_DASHBOARD_SCHEMAS",
    "MCP_DEBUG_SCHEMAS",
    "MCP_WORK_SCHEMAS",
    "MCP_PANEL_SCHEMAS",
    "MCP_GUIDE_SCHEMAS",
    "MCP_MATE_SCHEMAS",
)


def _tool_schema_items() -> list[PostureItem]:
    """Validated tool schemas, keyed by the tool name they gate.

    Derived from EVERY dispatch registry in ``validation`` — the dicts
    ``mcp_core``/``mcp_cron``/``computer_use.tools`` actually look a tool up in —
    plus the module-level ``*_SCHEMA`` objects that gate dashboard handlers rather
    than MCP tools.

    Deriving from the registries (not the ``*_SCHEMA`` naming convention) makes
    the registry, not the convention, the source of truth. Several registered MCP
    tools (e.g. ``cron_trigger``) are defined as inline or shared ``ToolSchema``
    objects with no module-level name of their own, so a convention-only walk
    validates them but cannot see them here — a tool registered that way would add
    zero to the count.

    ``_SCHEMA_REGISTRY_NAMES`` is enumerated rather than discovered, but the drift
    test derives its expectation from the same module attributes, so a NEW registry
    that is not listed here fails that test instead of silently under-reporting.
    """
    seen: dict[str, str] = {}
    for registry_name in _SCHEMA_REGISTRY_NAMES:
        registry = getattr(_validation, registry_name, None) or {}
        for tool_name in registry:
            seen.setdefault(tool_name, registry_name)
    for name in dir(_validation):
        if not (name.endswith("_SCHEMA") and name.isupper()):
            continue
        tool_name = getattr(getattr(_validation, name, None), "tool_name", "") or ""
        # A registry entry wins the attribution: it names where the schema is
        # actually enforced. Nameless/helper constants are skipped so the count
        # and the list always agree.
        if tool_name:
            seen.setdefault(tool_name, name)
    return [PostureItem(label=tool, detail=source) for tool, source in sorted(seen.items())]


def _redaction_sink_items() -> list[PostureItem]:
    return [
        PostureItem(label=label, detail=f"{module} — {detail}")
        for label, module, detail in _REDACTION_SINKS
    ]


def _credential_family_items() -> list[PostureItem]:
    return [PostureItem(label=label, detail=detail) for label, detail in _CREDENTIAL_FAMILIES]


def _exfil_heuristic_items() -> list[PostureItem]:
    return [PostureItem(label=label, detail=detail) for label, detail in _EXFIL_HEURISTICS]


# Human-readable gloss per SEL ``source`` value. The KEYS are not authoritative —
# ``sel._infer_source`` is — so a source added there without a gloss here still
# gets a row (labelled by its raw token) rather than being silently dropped, and a
# pinned test fails so the gloss gets written.
_AUDIT_SURFACE_DETAIL: dict[str, str] = {
    "slack": "Messages, approvals, and owner-authorization decisions",
    "dashboard": "Tool invocations, permission decisions, and authenticated API access",
    "cron": "Scheduled job fires and their tool calls",
    "subagent": "Spawned agent lifecycle, tool calls, and results",
    "taskrunner": "Autonomous multi-step task execution",
    "background": "Background maintenance work",
    "heartbeat": "Liveness / watchdog activity",
    "cli": "Terminal chat sessions",
    "discord": "Discord messages, approvals, and owner-authorization decisions",
    "telegram": "Telegram messages, approvals, and owner-authorization decisions",
    "wecom": "WeCom messages, approvals, and owner-authorization decisions",
    "weixin": "Weixin messages, approvals, and owner-authorization decisions",
    "whatsapp": "WhatsApp messages, approvals, and owner-authorization decisions",
    "feishu": "Feishu messages, approvals, and owner-authorization decisions",
    "webex": "Webex messages, approvals, and owner-authorization decisions",
    "teams": "Microsoft Teams messages, approvals, and owner-authorization decisions",
    "imessage": "iMessage messages, approvals, and owner-authorization decisions",
    "host": "In-process governance checks not driven by a user-facing surface",
    "unknown": "Events that carry no surface signal (classified rather than misattributed)",
}


def _audit_surface_items() -> list[PostureItem]:
    """Audited surfaces, DERIVED from ``sel._infer_source``'s vocabulary.

    ``_infer_source`` maps a session key to a surface, so its return vocabulary is
    the set of surfaces SEL can infer — deriving from it means adding one moves
    this count automatically.

    SCOPE (deliberate): a caller may pass an explicit ``source=`` that bypasses
    inference entirely (``channel``, ``token_auth``, ``migration``, …; ~70 such
    literals exist, many of which are event *kinds* rather than surfaces). Those
    are NOT enumerable as a clean "audited surfaces" list, so this control is
    scoped to the inferred vocabulary and its unit/summary say so — a floor, not a
    total. Claiming otherwise would be the very overstatement this module exists
    to remove.
    """
    return [
        PostureItem(
            label=source.replace("_", " ").capitalize(),
            detail=_AUDIT_SURFACE_DETAIL.get(source, "Audited SEL event source"),
        )
        for source in sorted(_sel_mod.audit_sources())
    ]


def _token_auth_items() -> list[PostureItem]:
    # circular import: token_auth lives under kiro_crew.dashboard, whose package
    # __init__ imports the server (which imports this module via handlers.core).
    # A top-level import here would close that cycle, so the two TTL constants are
    # read at call time — the documented circular-import exception, and it keeps
    # the advertised windows derived from the enforcing module rather than
    # restated as literals that could drift.
    from kiro_crew.dashboard.token_auth import (
        LINK_WINDOW_SECS,
        MAX_SESSION_TTL_SECS,
        proxied_pin_observed,
    )

    # Tri-state, deliberately, and derived from the LIVE bindings so it recovers
    # on its own. A pin that has collapsed onto a same-host proxy's loopback
    # address is NOT the control this row advertises, and "nothing is
    # pinned right now" is not evidence that pins are effective — rendering
    # either as the plain claim is the failure this row exists to avoid.
    _pinned = proxied_pin_observed()
    if _pinned is None:
        _pin_detail = (
            "A session is bound to the address that first used it. No session is "
            "currently pinned, so the effective scope is not known yet"
        )
    elif _pinned:
        _pin_detail = (
            "SHARED, not per-client: sessions are binding to a proxy's address rather than a "
            "client's — either a same-host tunnel (cloudflared / ngrok / tailscale serve) or a "
            "reverse proxy in front of this gateway — so every client reaching the dashboard "
            "through it satisfies the same pin. Reach the dashboard directly, or over a "
            "transport that preserves the client address, for the pin to identify one client"
        )
    else:
        _pin_detail = (
            "Per-client: a session is bound to the client address — or the "
            "daemon-verified tailnet identity — that first used it"
        )

    return [
        PostureItem(
            label="HMAC-SHA256 signature",
            detail="Tokens are signed with a persisted secret; a forged or tampered token is rejected",
        ),
        PostureItem(
            label="IP pinning",
            detail=_pin_detail,
        ),
        PostureItem(
            label="Single-use link nonce",
            detail=f"A share link is consumed on first click, within a {LINK_WINDOW_SECS // 60}-minute window",
        ),
        PostureItem(
            label="Bounded session lifetime",
            detail=f"Session cookies expire after at most {MAX_SESSION_TTL_SECS // 3600} hours",
        ),
        PostureItem(
            label="Revocation generation",
            detail="Bumping the generation invalidates every outstanding token at once",
        ),
        PostureItem(
            label="App-token scoping",
            detail="An app token reaches only its own namespace plus its manifest-declared API paths (deny-by-default)",
        ),
    ]


# ── The registry ──
# Order is display order. Each entry's count is len(items_fn()).
_CONTROLS: tuple[PostureControl, ...] = (
    PostureControl(
        key="sensitive_paths",
        label="Sensitive path blocking",
        unit="credential paths",
        summary=(
            "Paths agent file tools cannot read or write. Enforced at the PreToolUse gate "
            "on the resolved target, so a symlink into a blocked directory is refused too. "
            "From a shell, each leaf's sandbox disposition decides: a READONLY leaf stays "
            "readable by design, and a VISIBLE leaf rests on this tool gate alone."
        ),
        source="src/kiro_crew/security/paths.py",
        items_fn=_sensitive_path_items,
    ),
    PostureControl(
        key="write_protected_paths",
        label="Write-protected paths",
        unit="config paths",
        summary=(
            "Readable but not writable by agent tools — config carrying resource ceilings "
            "and the data-home migration marker."
        ),
        source="src/kiro_crew/security/paths.py",
        items_fn=_write_protected_items,
    ),
    PostureControl(
        key="denied_commands",
        label="Denied commands",
        unit="built-in rules",
        summary=(
            "Destructive and credential-exfiltrating shell operations blocked at the "
            "PreToolUse gate. Configurable below; policy-pinned rules cannot be turned "
            "off. The count is the SHIPPED catalogue -- the set actually enforced is "
            "this minus any rule disabled below, so it can be smaller."
        ),
        source="src/kiro_crew/security/denied_rules_catalog.py",
        items_fn=_denied_command_items,
    ),
    PostureControl(
        key="suspicious_patterns",
        label="Suspicious bash patterns",
        unit="patterns",
        summary=(
            "Deletion, exfiltration, and pipe-to-interpreter shapes. Advisory: these "
            "are surfaced by the `kirocrew security audit` history scan, NOT blocked at the "
            "PreToolUse gate — the gate enforces the narrower denied-command rules "
            "and exfiltration checks above."
        ),
        source="src/kiro_crew/security/denied_rules.py",
        items_fn=_suspicious_pattern_items,
    ),
    PostureControl(
        key="tool_schemas",
        label="MCP input validation",
        unit="tool schemas",
        summary=(
            "MCP tool calls with a registered schema are checked for unicode "
            "normalization, length limits, enum allow-lists, and unknown-field "
            "rejection. Coverage is per tool, not universal: computer-use refuses an "
            "unregistered tool outright, while the core, cron and dashboard "
            "dispatchers pass one through unvalidated unless its own handler "
            "validates."
        ),
        source="src/kiro_crew/validation.py",
        items_fn=_tool_schema_items,
    ),
    PostureControl(
        key="redaction_paths",
        label="Output redaction",
        unit="output paths",
        summary=(
            "Every boundary where agent output reaches a human or an external "
            "service runs a redaction pass first. Most run both scanners; the few "
            "that run only one, or that the owner can narrow to one for their own "
            "view, say so on their own row."
        ),
        source="src/kiro_crew/security_posture.py",
        items_fn=_redaction_sink_items,
    ),
    PostureControl(
        key="credential_families",
        label="Credential patterns",
        unit="credential families",
        summary=(
            "Credential classes the redaction scanner recognizes, in plaintext and "
            "base64-encoded form."
        ),
        source="src/kiro_crew/security/redaction_patterns.py",
        items_fn=_credential_family_items,
    ),
    PostureControl(
        key="exfil_heuristics",
        label="URL exfiltration detection",
        unit="heuristics",
        summary=(
            "Domain-agnostic — flags the payload, not the destination. A URL matching "
            "any heuristic is replaced with a redaction marker."
        ),
        source="src/kiro_crew/security/exfil.py",
        items_fn=_exfil_heuristic_items,
    ),
    PostureControl(
        key="audit_surfaces",
        label="SEL audit logging",
        unit="session-key surfaces",
        summary=(
            "Append-only, HMAC-chained security event log; the chain is verifiable "
            "for tampering. Redaction is applied on the forward path, and on-disk "
            "records are redacted by each caller before `log`. Listed below are the "
            "surfaces SEL infers from a session key; a call site may also stamp a "
            "more specific source of its own, so this is a floor, not a total."
        ),
        source="src/kiro_crew/sel.py",
        items_fn=_audit_surface_items,
    ),
    PostureControl(
        key="token_auth",
        label="Dashboard token auth",
        unit="auth controls",
        summary="Layered controls on every authenticated dashboard request.",
        source="src/kiro_crew/dashboard/token_auth.py",
        items_fn=_token_auth_items,
    ),
)


def _control_payload(control: PostureControl) -> dict:
    """Serialize one control, degrading to a count-less entry on failure.

    A control whose ``items_fn`` raises must not take down the whole posture
    response — the panel should show the rest of the posture and mark this one
    unavailable, since this endpoint is purely informational.
    """
    try:
        items = control.items_fn()
    except Exception:
        logger.warning("Security posture: items unavailable for %r", control.key, exc_info=True)
        return {
            "key": control.key,
            "label": control.label,
            "unit": control.unit,
            "summary": control.summary,
            "source": control.source,
            "count": None,
            "items": [],
            "unavailable": True,
        }
    return {
        "key": control.key,
        "label": control.label,
        "unit": control.unit,
        "summary": control.summary,
        "source": control.source,
        "count": len(items),
        "items": [{"label": i.label, "detail": i.detail} for i in items],
        "unavailable": False,
    }


def build_posture_snapshot() -> dict:
    """Build the full security-posture detail payload.

    Blocking: ``_denied_command_items`` reads the built-in rule table (in-memory)
    but the caller may also fold in the denied-commands snapshot, which touches
    the filesystem — see ``build_posture_snapshot_async``.
    """
    controls = [_control_payload(c) for c in _CONTROLS]
    return {
        "controls": controls,
        "counts": {c["key"]: c["count"] for c in controls},
    }


def posture_counts() -> dict[str, int | None]:
    """Just the ``key → count`` map, without materializing the item lists.

    For a counts-only caller (``/api/security/stats``) building and serializing the
    full ~45 KB item payload to return three integers is pure waste. Still resolves
    every ``items_fn`` — the count IS ``len(items)``, which is the invariant that
    keeps a count honest — it simply does not carry the items back.
    """
    return {c["key"]: c["count"] for c in (_control_payload(c) for c in _CONTROLS)}


async def posture_counts_async() -> dict[str, int | None]:
    """``posture_counts`` off the event loop (see ``build_posture_snapshot_async``)."""
    return await asyncio.get_running_loop().run_in_executor(governance_executor(), posture_counts)


async def build_posture_snapshot_async() -> dict:
    """Build the snapshot off the event loop.

    ``_tool_schema_items`` walks the validation module and the denied-rule table
    walk is O(137); neither blocks on I/O today, but this is a read-only
    informational endpoint and keeping it off the loop costs nothing while making
    a future filesystem-backed control safe to add.

    Offloaded to the dedicated ``governance_executor`` (``mc-gov``) — NOT the
    shared default pool — for the same reason its sibling
    ``build_governance_policy_snapshot_async`` is: this GET is
    browser-triggerable, so the moment a control DOES read the filesystem (the
    case the paragraph above exists to keep safe), default-pool I/O would
    contend with the workers the event loop shares for DNS. Choosing the right
    pool now means adding that control is a one-line change, not a latent stall.
    """
    return await asyncio.get_running_loop().run_in_executor(
        governance_executor(), build_posture_snapshot
    )
