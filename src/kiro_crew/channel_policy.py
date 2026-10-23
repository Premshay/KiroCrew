"""Channel command policy and serialized message, member and attach records.

The channel facade forwards patches here; routing and execution remain there.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("kiro_crew.channel")

_MAX_AGENTS = 3
_MAX_CHANNELS = 1
_MAX_MESSAGES = 200
# One bound for EVERY string an approval message retains: the prose input,
# the card title, and each ``meta`` value. ``_MAX_MESSAGES`` caps the ring by
# count; this caps what each retained entry can weigh, so a model-authored
# command cannot grow the persisted channel without bound. A title that would
# not fit is REFUSED, never cut: the title is the one place the channel reader
# sees the whole command, and a cut title beside a live Approve button is an
# approval of a suffix nobody read. The refusal notice is itself bounded.
_APPROVAL_FIELD_MAX_CHARS = 500
# The longest a posted approval card waits for a reader before the HOST
# declines it, and the wait while ``agent.tool_approval_timeout_secs`` is left at
# its default (see :func:`_channel_approval_window_secs`). The in-band notice
# quotes the window the wait actually used.
_APPROVAL_TIMEOUT_SECS = 3600
# The card title of a grantable shell command is this prefix plus the command;
# the exact tier sends the title back (minus the prefix) as its consent proof.
_APPROVAL_SHELL_TITLE_PREFIX = "Running: "
_MAX_A2A_EXCHANGES = 3

# Max time an agent blocks on its inbox before re-checking its stop condition,
# guaranteeing subscribe() can never park indefinitely.
_INBOX_POLL_SECS = 1.0

# The dispatch verbs: a channel agent may not START work that outlives its own
# confined turn.  Every verb here creates or drives an execution context the
# channel agent does not itself occupy, and that context is NOT confined -- a
# spawned descendant's session key is ``subagent:<id>``, so a containment check
# keyed on a ``channel:`` identity does not recognise the descendant, and the
# descendant holds the full default toolset including every name in the list
# below.  The verbs are therefore refused at the channel agent's OWN hop, where
# its ``channel:`` identity is the one thing already verified.
#
# Per verb: ``spawn_run`` and ``spawn_sub_agents`` create a descendant outright;
# ``spawn_continue`` dispatches a fresh task into an existing run's
# conversation; ``spawn_steer`` injects text a running descendant executes as
# part of its turn; ``workflow_run`` and ``workflow_rerun_subtree`` run an
# orchestration of agents, and ``workflow_author`` exists only to feed them;
# ``task_run`` starts the autonomous task runner; ``register_hook`` opens a
# dedicated agent session an external POST drives later; ``pod_up`` boots a
# whole preview gateway as its own host process on its own port, which keeps
# serving after the turn that started it has ended.
#
# The observe-and-tear-down verbs are deliberately ABSENT, and their absence is
# the qualifier this invariant needs rather than an omission: ``spawn_list``,
# ``spawn_status``, ``spawn_release``, ``workflow_status``, ``workflow_result``,
# ``workflow_list``, ``workflow_cancel``, ``workflow_library_list``,
# ``pod_ls``, ``pod_status`` and ``pod_down`` read or
# end a context that already exists and start no turn.  ``spawn_status`` returns
# a retained transcript, so how widely that read is scoped is a question about
# read scope and not about this boundary.
CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS: tuple[str, ...] = (
    "spawn_run",
    "spawn_sub_agents",
    "spawn_continue",
    "spawn_steer",
    "workflow_run",
    "workflow_author",
    "workflow_rerun_subtree",
    "task_run",
    "register_hook",
    "pod_up",
)

# One tool on the same boundary cannot be held by NAME.  ``ops_mission_control_api``
# is a passthrough: one tool carrying a whole API surface, most of which reads.
# ``POST /rotation/arm`` is the operation that starts work outliving the turn --
# it arms the app's crons, which then fire unattended once the confined turn has
# ended -- so the deny is keyed on the operation and the tool's read surface stays
# reachable.  Method and path are the two fields that identify an operation; the
# tool's own schema admits nothing but an exact member of its allowlist in
# either, so an operation named here cannot be reached under a second spelling.
#
# Enforced at MCP dispatch alone, unlike the name list, which is also matched at
# the permission-request event: that matcher reads the rendered title, where an
# operation does not appear.  Containment still holds, because the dispatch guard
# is what refuses the call -- approving the prompt only means the refusal arrives
# one step later, and the interactive guard's job is to beat an AUTO-approval,
# which the dispatch guard beats as well.
CHANNEL_AGENT_BLOCKED_DISPATCH_OPERATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "ops_mission_control_api": (("POST", "/rotation/arm"),),
}

# Direct-to-user messaging tools a channel agent may never invoke — channel
# agents communicate exclusively through channel posts.  send_notification
# reaches the user like send_message does (notification feed publish, badge,
# and sound), so both sit
# behind the same containment boundary.  All THREE session-control tools sit
# behind it, in both directions: stopping one of the user's dashboard sessions
# reaches the user through that session's transcript, and READING one pulls a
# private dashboard conversation into a channel other humans can see.  The read
# tool mutates nothing, but containment here is about what crosses the boundary,
# not about who writes — so the exfiltration direction is blocked alongside the
# control one.  CREATE is blocked for a different reason than the other two: it
# writes nothing into an existing conversation, but a session it opened would be
# one the channel agent then owns and may act on, which is exactly the
# containment this list exists to hold.  SEND is the sharpest of the four: stop
# only cancels and read only exfiltrates, but send delivers text that the target
# session RUNS as a turn — so external channel content would execute inside a
# private dashboard conversation.
# The dispatch verbs above are appended rather than respelled here, so the
# interactive guard and the MCP-dispatch guard read ONE list.
# Matched against the rendered
# permission-request text/title via _blocked_tool_named() (boundary-aware,
# not naive substring — "Editing send_notification.py" must NOT match).
CHANNEL_AGENT_BLOCKED_TOOLS: tuple[str, ...] = (
    "send_message",
    "send_notification",
    "session_stop",
    # Ending another session's wait moves that turn forward on a schedule the
    # channel's thread text would then be choosing; same containment reason.
    "session_end_wait",
    # Re-running a turn spends the target's model and tools on work a channel
    # message did not ask for; same containment reason as stop.
    "session_retry",
    # Continuing a thread spends the target's model and tools the same way.
    "session_continue",
    # Changing a session's model decides what the user's next turn there runs
    # on and spends; same containment reason as stop.
    "session_set_model",
    # Reload relaunches another session's agent process; same containment
    # reason as stop.
    "session_reload",
    # Renaming relabels a session in the person's sidebar, named from thread text
    # other people wrote; same containment reason as the tree verbs below.
    "session_rename",
    # Retitling spends a model call and relabels a session in the person's
    # sidebar; same containment reason as stop.
    "session_generate_title",
    "session_send",
    "session_read_message",
    # A digest of the same transcript `session_read_message` returns, so it is
    # blocked for the same exfiltration reason.
    "session_summary",
    # The fan-out verb, blocked for the reason `session_send` is and then some: one
    # call reaches every session the caller created, so a channel agent acting on
    # words from a thread other people are in would relay them into the user's
    # whole worker fleet at once.
    "session_broadcast",
    # The roster verb. It returns other sessions' keys and TITLES, so a channel
    # agent calling it puts the names of the user's private work in front of
    # whoever is in that thread -- the same exfiltration `session_read_message` is
    # blocked for, one step shallower.
    "session_status",
    "session_create",
    "session_fork",
    "session_close",
    # The tree verbs, blocked on the containment reason the rest share: a channel
    # agent acts on words from a thread other people are in, and these two rearrange
    # what the person sees in their sidebar -- an adoption takes a session and its
    # whole subtree under another one.
    "session_adopt",
    "session_release",
    # Pinning belongs with the tree verbs: it moves another session to or from
    # the top of the person's sidebar, and a channel agent names that session
    # from thread text other people wrote. This entry covers the permission
    # prompt; the chat_session_pin handler in mcp_dashboard_tag_runs.py refuses
    # a ``channel:`` caller at dispatch, which is what holds for auto-approval.
    "chat_session_pin",
    # The board-column writes, on the same reason as pinning: they rearrange
    # the person's own sidebar board. The handlers in mcp_dashboard_tag_runs.py
    # also refuse a ``channel:`` caller at dispatch, for auto-approved calls.
    # ``chat_tag_column_list`` stays available: the layout names no session.
    "chat_tag_column_create",
    "chat_tag_column_move",
    # Moving a card between board columns retags another session, which a
    # channel agent would name from thread text; the handler also refuses a
    # ``channel:`` caller at dispatch.
    "session_move_to_column",
    # Reordering pins is the same containment case: it rewrites the person's
    # sidebar order, and its handler refuses a ``channel:`` caller at dispatch.
    "chat_session_pin_move",
    "session_revive",
    # Renaming a folder changes the person's sidebar the same way, and a channel
    # caller reaches the endpoint with the person's folder authority. The
    # chat_folder_update handler refuses a ``channel:`` caller at dispatch.
    "chat_folder_update",
    # Lists archived session titles and keys: a channel thread other people
    # read is not where the person's closed-session history belongs.
    "session_history_list",
    # The four work-ledger tools, blocked for the same containment reason and not
    # for a new one: a channel agent has no dispatch relationship, so it is
    # neither a conductor nor a bound worker and has no business holding one.
    # Reading a brief would pull a private dispatch's acceptance bar into a
    # channel other humans can see, and a report or a record would write into a
    # conductor's own decision record from outside it.
    "work_brief",
    "work_report",
    "work_ledger_read",
    "work_ledger_record",
    "work_ledger_rebuild",
) + CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS

# Boundary-aware matcher: the tool name must stand alone in the rendered
# title — not embedded in a filename/path/identifier ("send_notification.py",
# "/tmp/send_message_backup"). MCP separator runs of 2+ underscores are
# normalized to spaces first so BOTH qualified invocation forms match:
# "kirocrew-core___send_message" (kiro-cli) and
# "mcp__kirocrew-core__send_message" (canonical MCP prefix form).
_BLOCKED_TOOL_RE = re.compile(
    r"(?<![\w.\-/])("
    + "|".join(re.escape(t) for t in CHANNEL_AGENT_BLOCKED_TOOLS)
    + r")(?![\w.\-/])"
)
_MCP_SEPARATOR_RE = re.compile(r"_{2,}")
# A THIRD qualified spelling: opencode joins server and tool with a SINGLE
# underscore ("kirocrew-core_send_message", measured on 1.18.30), which the run
# of 2+ above leaves untouched — and a lone "_" before the tool name is a word
# character, so the boundary lookbehind then refuses the match and the whole
# containment list read as absent on that harness. Keyed on Crew's OWN
# server-name shape rather than on "one underscore", because a bare single
# underscore also unblocks "do_send_message" and every other identifier that
# merely ends in a blocked name. Applied AFTER the run normalization, so
# "mcp__kirocrew-core__send_message" is already spaced out and does not come
# back here as "mcp _send_message" with the boundary re-broken. Recognising one
# name too many can only ever BLOCK more, which is the safe direction for a
# containment list — unlike the grant in ``session_directive``, which is why
# that one matches the server name exactly.
# Left boundary is the same class ``_BLOCKED_TOOL_RE`` uses, NOT ``\b``: ``\b``
# treats ``/`` and ``.`` as boundaries, so a rendered PATH
# ("cat /tmp/kirocrew-core_send_message") normalized to " send_message" and
# over-blocked -- the same filename-versus-tool confusion the negative cases
# in ``test_channel_blocked_tools.py`` exist to catch.
_CREW_MCP_SERVER_PREFIX_RE = re.compile(r"(?<![\w.\-/])kirocrew-[A-Za-z0-9-]+_")


def _shell_base_binary(cmd: str) -> str | None:
    """The single binary a SIMPLE shell command invokes, else None.

    Shell-aware (shlex) tokenization, fail-closed on everything that is not
    one plain invocation: parse failures, empty commands, shell operators or
    substitution anywhere (``|``, ``&``, ``;``, newline, backtick, ``$(``,
    redirects), and env-assignment or quoted/space-bearing first tokens. A
    base-command trust grant keyed on anything richer than one unambiguous
    binary name has already been shown to widen scope (naive first-token
    slicing turned ``"./my tool" --safe`` into a ``"./my`` prefix grant, and
    env prefixes into match-anything grants), so anything ambiguous simply
    has no base.
    """
    if not cmd or _CHANNEL_SHELL_OPERATOR_RE.search(cmd):
        return None
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    if not tokens:
        return None
    base = tokens[0]
    # An env-assignment first token means the binary is the SECOND token —
    # refuse rather than guess (the grant would silently cover any command
    # run under that prefix).
    if not base or "=" in base:
        return None
    # A base containing whitespace can only come from quoting; a quoted
    # executable path is matched by the exact-command tier, not by base.
    if any(c.isspace() for c in base):
        return None
    # POSITIVE shape check: the base must look like a plain binary name or
    # path. Enumerating bad metacharacters one by one (``!``, ``(``, …) is a
    # losing game — the shell always has one more; anything outside this
    # conservative charset is refused instead.
    if not _CHANNEL_BASE_BINARY_RE.fullmatch(base):
        return None
    # Shell reserved words pass the charset but are interpreted by the shell,
    # not executed as a binary — ``time rm ...`` must not yield a ``time``
    # grant that covers ``time <anything>``.
    if base in _CHANNEL_SHELL_RESERVED_WORDS:
        return None
    return base


def _offerable_base_binary(cmd: str) -> str | None:
    """The base a "Trust all <base> commands" consent may name, else None.

    ``_shell_base_binary`` tokenizes in POSIX mode, where a backslash is an
    escape, so the Windows path ``.\\deploy.bat`` derives ``.deploy.bat``: a
    name the user never typed, and one that also covers a different file
    literally called ``.deploy.bat``. A consent label must name what the user
    read, so a first token holding a backslash gets no base offer and no base
    grant; the exact-command tier still covers that command.
    """
    stripped = cmd.lstrip() if cmd else ""
    first = stripped.split(None, 1)[0] if stripped else ""
    if "\\" in first:
        return None
    return _shell_base_binary(cmd)


# Anything that makes a command more than ONE plain invocation. Deliberately
# broader than the enforcement splitter's operator set: over-matching here
# fails toward "no per-command grant available", never toward a wider grant.
# Parentheses and braces cover subshell / brace grouping — "(rm /tmp/a)" must
# not yield "(rm" as a trusted binary.
_CHANNEL_SHELL_OPERATOR_RE = re.compile(r"[|&;`\n\r<>(){}]|\$\(")

# What a trusted base binary is ALLOWED to look like: alphanumerics plus the
# few characters real binary names and paths use. Everything else — negation
# ``!``, test ``[``, arithmetic ``((`` — simply has no derivable base.
_CHANNEL_BASE_BINARY_RE = re.compile(r"[A-Za-z0-9._/+-]+")

# Words the shell interprets instead of executing. A grant keyed on one would
# cover whatever command the shell runs under it.
_CHANNEL_SHELL_RESERVED_WORDS = frozenset(
    "! case coproc do done elif else esac fi for function if in select "
    "then time until while".split()
)


def _match_trusted_channel_command(cmd: str, agent: "ChannelAgent") -> str | None:
    """Match a pending shell command against the agent's trust grants.

    Two literal forms only, both case-sensitive (on POSIX ``./Deploy.sh`` and
    ``./deploy.sh`` are different executables):

    - exact: the whole command text equals a granted command, OR
    - base: the command is a SIMPLE invocation (see ``_shell_base_binary``)
      whose binary equals a granted base.

    Compound commands never match a base grant and only match an exact grant
    granted for that identical compound; there is no pattern language and no
    per-segment matching, so a grant can never cover text the user did not
    read. The base is derived with ``_offerable_base_binary``, the same rule the
    card offers from: a first token holding a backslash has no base, because
    POSIX tokenizing drops the backslash and ``n\\pm`` would otherwise match a
    ``npm`` grant while Windows runs a different file. Returns an audit label
    or None.
    """
    if cmd in agent._trusted_commands:
        return f"command:{cmd}"
    base = _offerable_base_binary(cmd)
    if base is not None and base in agent._trusted_bases:
        return f"base:{base}"
    return None


def _blocked_tool_named(rendered: str) -> bool:
    """True when a blocked messaging tool is named (as a tool) in *rendered*."""
    normalized = _CREW_MCP_SERVER_PREFIX_RE.sub(" ", _MCP_SEPARATOR_RE.sub(" ", rendered))
    return bool(_BLOCKED_TOOL_RE.search(normalized))


class ListenMode(Enum):
    """How an agent receives channel messages."""

    ALL = "all"  # every message (orchestrator)
    MENTION = "mention"  # only @mention or human broadcast
    SILENT = "silent"  # initial task only, then done


class ApprovalPolicy(Enum):
    """What tool calls require human approval."""

    ALL = "all"  # every tool call
    WRITES = "writes"  # only mutating ops (default)
    TRUSTED = "trusted"  # auto-approve everything


@dataclass
class ChannelMessage:
    """A single message in a channel."""

    id: str
    from_id: str  # "human" or agent id
    from_role: str  # display name
    content: str
    mention: str | list[str] | None = None  # target agent id(s)
    msg_type: str = "progress"  # progress|mention|broadcast|approval|done|system
    delivery: str = "next_turn"  # next_turn|interrupt; meaningful for mentions
    delivery_status: dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    thread_id: str | None = None  # parent message ID (None = top-level)
    reply_to: str | None = None  # agent ID of parent message sender
    reply_count: int = 0  # thread reply count (top-level only)
    # Structured facts beside the prose, for renderers that make decisions
    # from the message rather than display it. An approval carries the
    # server's own verdict on which trust tiers it can record (see the
    # approval post in ``_stream_task``), so the card is gated by server fact
    # instead of a client regex over ``content``. Flat string values only,
    # mirroring chat's ``perm_meta``; every value is already display-redacted.
    # ``None`` for every other message and for messages persisted before the
    # field existed -- their prose is unchanged, so a renderer that ignores
    # ``meta`` (Slack mirrors, older dashboards) shows exactly what it did.
    meta: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from_id": self.from_id,
            "from_role": self.from_role,
            "content": self.content,
            "mention": self.mention,
            "msg_type": self.msg_type,
            "delivery": self.delivery,
            "delivery_status": self.delivery_status,
            "timestamp": self.timestamp,
            "thread_id": self.thread_id,
            "reply_to": self.reply_to,
            "reply_count": self.reply_count,
            "meta": self.meta,
        }


@dataclass
class ChannelAgent:
    """A persistent agent in a channel."""

    id: str
    role: str
    agent_name: str
    task: str
    session_key: str = ""  # f"channel:{channel_id}:{agent_id}"
    state: str = "pending"  # pending|working|listening|done|failed
    is_orchestrator: bool = False
    approval_policy: ApprovalPolicy = ApprovalPolicy.WRITES
    listen_mode: ListenMode = ListenMode.MENTION
    attached_session: bool = False
    collaboration_onboarded: bool = False
    inbox: asyncio.Queue[ChannelMessage] = field(default_factory=asyncio.Queue)
    _approval_future: asyncio.Future | None = field(default=None, repr=False)
    # Per-command trust grants (trust_command / trust_base tiers) — runtime
    # only, like the chat slot's session-scoped grants: agents are relaunched
    # with fresh sessions on gateway restart, so grants deliberately do not
    # persist. Grants are OPAQUE LITERALS, never patterns: ``_trusted_commands``
    # holds exact full command texts (matched by string equality) and
    # ``_trusted_bases`` holds single binary names (matched by shlex-token
    # equality). No pattern language exists here by design — every derived
    # sub-pattern scheme reviewed on this surface (segment globs, base globs
    # from naive tokenization) leaked scope the user never consented to.
    _trusted_commands: set[str] = field(default_factory=set, repr=False)
    _trusted_bases: set[str] = field(default_factory=set, repr=False)
    # Canonical shell command of the approval currently awaiting a decision,
    # extracted server-side from the provider event's ``tool_input`` when the
    # provider classified the tool as shell ("" otherwise). The approve
    # endpoint binds trust_command / trust_base grants to THIS value — display
    # titles and request-body patterns are LLM-influenced and never scope a
    # grant. Set while ``_approval_future`` is pending, cleared with it.
    _pending_approval_command: str = field(default="", repr=False)
    # The provider's text when the last lost-session verdict was a credential
    # failure ("" otherwise), so the recovery notices can name the real cause.
    _credential_error: str = field(default="", repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "agent_name": self.agent_name,
            "task": self.task,
            "session_key": self.session_key,
            "state": self.state,
            "is_orchestrator": self.is_orchestrator,
            "approval_policy": self.approval_policy.value,
            "listen_mode": self.listen_mode.value,
            "attached_session": self.attached_session,
        }


# A coordinator may ask to attach an existing dashboard session, but only the
# dashboard user grants it: the request is recorded here and resolved by the
# operator-authenticated channel route, never by the session-bound MCP route.
_ATTACH_REQUEST_REASON_MAX_CHARS = 500


@dataclass
class AttachRequest:
    """One coordinator request to attach an existing dashboard session."""

    id: str
    slot: str
    title: str
    role: str
    listen_mode: ListenMode
    reason: str
    requested_by: str
    created_at: float = field(default_factory=time.time)

    @property
    def session_key(self) -> str:
        return f"dashboard:{self.slot}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slot": self.slot,
            "title": self.title,
            "role": self.role,
            "listen_mode": self.listen_mode.value,
            "reason": self.reason,
            "requested_by": self.requested_by,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AttachRequest | None":
        try:
            return cls(
                id=str(data["id"]),
                slot=str(data["slot"]),
                title=str(data.get("title", "")),
                role=str(data["role"]),
                listen_mode=ListenMode(data.get("listen_mode", "all")),
                reason=str(data.get("reason", "")),
                requested_by=str(data.get("requested_by", "")),
                created_at=float(data.get("created_at", time.time())),
            )
        except (KeyError, TypeError, ValueError):
            return None


for _record in (ListenMode, ApprovalPolicy, ChannelMessage, ChannelAgent, AttachRequest):
    _record.__module__ = "kiro_crew.channel"
del _record
