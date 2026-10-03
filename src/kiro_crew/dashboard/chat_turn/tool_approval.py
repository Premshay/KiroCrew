"""Tool-permission policy a dashboard turn applies before it answers a request."""

from __future__ import annotations

import ipaddress
import re
import shlex
import urllib.parse
from typing import TYPE_CHECKING, Any

from kiro_crew.deny_guidance import DENY_CLASS_AWS_CREDENTIAL, DENY_CLASS_SSO_CREDENTIAL

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DENY_CAUSE_POLICY,
        Refusal,
        _name_grant_refusal_off_loop,
        _split_command_segments,
        classify_deny,
        log_decline,
        resolve_credential_tool_hint,
        safety_override,
        sel,
        shell_command_for_event,
    )


def _pre_tool_hooks_should_block(pre_hook_results: Any) -> bool:
    """Deny-by-default for unexpected hook output, plus explicit BLOCKED:.

    PreToolUse script hooks return a list of strings (each either a
    stdout-injection string or a 'BLOCKED:<name>:<reason>' marker emitted
    by ``_fire`` when a hook exits 2). This helper returns True when the
    auto-approve path must reject the tool: anything that's not a list of
    strings is treated as suspicious (deny-by-default), and any
    BLOCKED:-prefixed string blocks. An empty list is the documented
    pass-through contract (no hooks registered, or all registered hooks
    exited 0 with no stdout) and returns False.
    """
    if pre_hook_results is None or not isinstance(pre_hook_results, list):
        return True
    return any(not isinstance(r, str) or r.startswith("BLOCKED:") for r in pre_hook_results)


def _pre_tool_block_reason(pre_hook_results: Any) -> str:
    """Return the first hook-authored block reason, or a safe fallback."""
    if isinstance(pre_hook_results, list):
        for result in pre_hook_results:
            if isinstance(result, str) and result.startswith("BLOCKED:"):
                parts = result.split(":", 2)
                reason = parts[2].strip() if len(parts) == 3 else ""
                if reason:
                    return reason
    return "blocked by a PreToolUse policy hook"


#: The block ``_fire`` returns for a PreToolUse when the agent spec's own hooks
#: could not be read. A deny hook that was never loaded gave no verdict, and a
#: PreToolUse gate with no verdict blocks, as it does for an uninitialized store.
_SPEC_HOOKS_UNREADABLE_BLOCK = "BLOCKED:system:the agent spec's hooks could not be read"


def _spec_keys_notice(agent: str, keys: list[str]) -> str:
    """The session-start notice for spec keys this backend never receives."""
    return (
        f"ℹ️ Agent {agent} sets {' and '.join(keys)}, which this backend does not "
        "receive, so they have no effect in this session."
    )


def _spec_confirm_hooks_notice(agent: str, count: int) -> str:
    """The session-start notice for ``confirm: true`` spec hooks this backend skips."""
    hooks = "hook asks" if count == 1 else "hooks ask"
    return (
        f"ℹ️ Agent {agent} has {count} {hooks} to be confirmed before running. "
        "This backend cannot ask, so they do not run in this session."
    )


#: Deny classes a credential-vending MCP server can actually resolve. Only these
#: justify the capability-manager lookup: the hint is appended for them alone, so
#: probing on any other refusal would spend a subprocess to produce a string
#: nothing reads.
_CREDENTIAL_HINT_CLASSES = frozenset({DENY_CLASS_AWS_CREDENTIAL, DENY_CLASS_SSO_CREDENTIAL})


async def _credential_tool_hint_for(reason: str, cause: str, subject: str = "") -> str:
    """The host's credential-vendor hint, when *reason* is a refusal it can answer.

    Gated on the class rather than resolved unconditionally because the lookup
    shells out to the edition's package manager. A refusal is already a bad moment
    to add latency to, and for every non-credential class the result would be
    discarded by :func:`build_refusal_steer_notice` anyway.
    """
    if cause != DENY_CAUSE_POLICY:
        return ""
    if classify_deny(reason, subject) not in _CREDENTIAL_HINT_CLASSES:
        return ""
    return await resolve_credential_tool_hint()


def _slot_is_trusted(slot: Any) -> bool:
    """True when this slot's tool calls are auto-approved. TWO representations.

    * ``slot._trust`` — the interactive "trust this session" grant. A human clicked
      it, so it does not expire and the click is its own audit record.
    * ``slot._trust_scope`` — a ``SafetyOverride`` SCOPED grant, for an unattended
      app worker where there is no human to click anything. It is SEL-audited
      fail-closed at activation, TTL-bounded, and re-checked HERE on every approval
      via ``is_scope_active`` — so the grant lapsing is what revokes trust, with no
      cooperation required from whatever armed it.

    Strictly additive: the scope is consulted only when the slot actually carries a
    key, so a slot without the attribute — which is every ordinary chat session —
    takes exactly the decision it took before this existed.

    Deliberately does NOT renew the grant. The task runner slides its grant forward
    on tool activity because the run's own progress is the liveness signal; a crew's
    signal is its watchdog, and renewing here would let a crew whose watchdog died
    keep its grant alive off its own tool calls — which is the bound this is for.
    """
    if getattr(slot, "_trust", False):
        return True
    scope = str(getattr(slot, "_trust_scope", "") or "")
    if not scope:
        return False
    return bool(safety_override().is_scope_active(scope))


def _auto_approve_reason(slot: Any, yolo_active: bool) -> str:
    """SEL provenance for an auto-approval: yolo, session trust, or a scoped grant.

    Yolo first because it is process-wide and outranks anything per-slot, then the
    human's session flag, then the scoped grant — the same precedence
    :func:`_slot_is_trusted` decides by. Purely descriptive; it authorises nothing.
    """
    if yolo_active:
        return "yolo"
    if getattr(slot, "_trust", False):
        return "trust"
    if str(getattr(slot, "_trust_scope", "") or ""):
        return "trust_scope"
    return "trust"


def _persistable_session_policy(slot: Any, yolo_active: bool) -> str:
    """The session-level approval policy to STORE for this slot: ``"auto"`` or ``""``.

    Deliberately NOT :func:`_slot_is_trusted`, and that difference is the whole
    point of this function. Everything else on the trust path decides ONE approval
    and re-decides the next one; this value is written into the session store and
    read LATER — by the subagent spawn gate and by each subagent's own approval
    policy — at a point where nothing re-checks whether the grant still holds.

    So only a grant that cannot lapse may be cached here:

    * ``slot._trust`` — a human clicked "trust this session". It does not expire,
      and the click is its own audit record, so caching it changes nothing.
    * yolo — process-wide, and revoking it deactivates the override for everyone.

    A ``SafetyOverride`` SCOPED grant (``slot._trust_scope``) must NOT reach here.
    Its entire value is being re-checked on every approval, so a cached ``"auto"``
    would outlive it: pause or retire the crew, or disable the app, and a turn
    already in flight would keep auto-approving subagent tool calls off a policy
    written before the revocation — exactly the property the scoped grant exists to
    provide, defeated by caching it.

    A scope-trusted worker is not left stalling: its own tool approvals never
    consult this value. They go through :func:`_slot_is_trusted` per event, which
    re-checks the scope each time.
    """
    if yolo_active or getattr(slot, "_trust", False):
        return "auto"
    return ""


def _native_crew_should_auto_approve(native_tracker, state, slot) -> bool:
    """Return True only when a native crew subagent is ACTIVE *and* an
    auto-approve condition holds — otherwise deny (CWE-1188 secure default).

    Active-crew is a NECESSARY precondition: with no live native subagent the
    parent turn is not blocked on a crew tool, so this path must never
    auto-approve — regardless of the ``auto_approve_subagent_tools`` hook,
    the slot's trust, or yolo. Only when a crew is active do those signals grant
    approval; with all three false the tool still falls through to the normal
    interactive/trust gate rather than being silently approved here.
    """
    has_active_crew = bool(native_tracker) and any(
        not info.get("done") for info in native_tracker.values()
    )
    if not has_active_crew:
        return False
    return bool(
        (state.context_builder and state.context_builder.hooks.auto_approve_subagent_tools)
        or _slot_is_trusted(slot)
        or state.is_yolo_active()
    )


async def _name_grant_refusal_for(event: object) -> Refusal | None:
    """Why a shell *event* may not be auto-approved by program NAME, or ``None``.

    Every auto-approve tier is a statement about a PROGRAM, and the shell
    resolves the name itself afterwards through a ``PATH`` that legitimately
    leads with directories the agent can write.

    This lives here rather than inside ``HookManager.on_tool_call``, which is
    synchronous and called ON the loop. The hook layer decides its own tiers and
    this downgrades an auto-approve it granted, so a refusal costs one
    interactive prompt and never blocks.

    A thin wrapper over :func:`kiro_crew.name_grant.refusal_for_event` rather
    than an alias to it, so the module-level ``_name_grant_refusal_off_loop``
    stub seam still covers this path. The decline-not-raise guard lives inside
    :func:`kiro_crew.name_grant.refusal_for_command_off_loop` (the chokepoint
    every tier reaches), so this — and the trusted-pattern and trust-reads
    rungs that call the seam directly — inherit it without a second copy.

    ``None`` for a non-shell tool or an unrecoverable command: there is no
    program name to vouch for, and those tiers are unchanged.
    """

    command = shell_command_for_event(event)
    if command is None:
        return None
    return await _name_grant_refusal_off_loop(command)


def _audit_name_grant_refusal(
    *, session_key: str, slot: Any, event: Any, refusal: Refusal, tier: str
) -> None:
    """Record that a name-based auto-approve was DECLINED, and on which tier.

    A thin wrapper over :func:`kiro_crew.name_grant.log_decline`, which owns
    the payload convention (the CODE, never the ``detail``; redacted title;
    not ``critical``) for every surface. This module's ``sel`` binding is
    passed through so the dashboard's audit seam still observes the row.
    """

    log_decline(
        source="dashboard",
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        event=event,
        refusal=refusal,
        tier=tier,
        sel_factory=sel,
    )


_BROWSER_CLI_BIN = "playwright-cli"


_BROWSER_CLI_PAGE_VERBS = frozenset(
    {
        # Core / lifecycle. `close` is deliberately absent -- see the
        # exclusion note below. `detach` stays: it releases the session
        # without taking the operator's window with it.
        "open",
        "attach",
        "detach",
        "goto",
        "resize",
        # Interaction
        "type",
        "click",
        "dblclick",
        "fill",
        "drag",
        "drop",
        "hover",
        "select",
        "check",
        "uncheck",
        # Reading the page
        "snapshot",
        "find",
        "generate-locator",
        "highlight",
        # Dialogs
        "dialog-accept",
        "dialog-dismiss",
        # Navigation
        "go-back",
        "go-forward",
        "reload",
        # Keyboard / mouse
        "press",
        "keydown",
        "keyup",
        "mousemove",
        "mousedown",
        "mouseup",
        "mousewheel",
        # Capture (writes only into the service's own output dir)
        "screenshot",
        "pdf",
        # Tabs. `tab-close` is absent for the same reason as `close`.
        "tab-list",
        "tab-new",
        "tab-select",
        # Read-only request metadata: route-list prints the mock table
        # (pattern strings, no URLs) and config-print prints the session's
        # launch configuration.
        "route-list",
        # DevTools / diagnostics
        "console",
        "tracing-start",
        "tracing-stop",
        "video-stop",
        "video-chapter",
        "video-show-actions",
        "video-hide-actions",
        "show",
        "pause-at",
        "resume",
        "step-over",
        # Session management. The listing only; `close-all` / `kill-all`
        # are absent -- they are the widest-blast-radius verbs the CLI has.
        "list",
    }
)


_BROWSER_CLI_BARE_ONLY_VERBS = frozenset({"video-start"})


_BROWSER_CLI_SAFE_FLAGS = frozenset(
    {
        # MEASURED against the installed CLI: `-s=`, `--s=` and `--session=`
        # are all accepted and all name the same session. Only `-s` was listed,
        # so the named-session form this repo's own prompt.md tells the agent to
        # use (`--s=chrome`) fell through to interactive approval on EVERY
        # command after `attach` -- the documented primary workflow.
        "-s",
        "--s",
        "--session",
        "--json",
        "--raw",
        "--help",
        "--version",
        "--headed",
        "--browser",
        "--persistent",
        "--extension",
        "--cdp",
        "--endpoint",
        "--domain",
        "--hide",
        # Shape-only capture options: they change the image, not its location.
        "--type",
        "--full-page",
        "--hires",
    }
)


_BROWSER_CLI_SESSION_FLAGS = frozenset({"-s", "--s", "--session"})


_URI_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")


_LOOPBACK_HOST_NAMES = frozenset({"localhost"})


_MAX_DNS_NAME_LEN = 253


_DNS_HOST_RE = re.compile(r"(?:[^\s./\\:@\[\]]+\.)*[^\W\d_][^\s./\\:@\[\]]*", re.ASCII)


_SESSION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _is_remote_navigable_host(host: str) -> bool:
    """Whether http(s) navigation to *host* may be auto-approved.

    Refused, so that navigation there costs one interactive approval:

    * **Every local control plane.** Kiro Crew's own dashboard is served over
      loopback, and the approval mode, trust settings and YOLO switch all live on
      it -- so an auto-approved ``goto`` plus auto-approved clicks would let the
      agent widen its own ceiling with no human in the loop. The rule is the
      whole loopback range and the loopback names rather than one port number:
      the dashboard's port is only known at runtime (a pod gets its own), and
      "never auto-drive a local admin UI" covers the dashboard, pods, and
      anything else the operator happens to run on the host under one rule.
      ``0.0.0.0`` / ``::`` are refused with it, because a listener bound to the
      unspecified address is reachable that way too.
    * **Link-local**, where cloud instance metadata and its credentials live: a
      ``goto`` followed by an auto-approved ``snapshot`` would print an instance
      role's credentials into the agent's context.
    * **Private** (RFC 1918: 10/8, 172.16/12, 192.168/16), **CGNAT/shared**
      (100.64/10), and **all other non-globally-routable addresses** including
      multicast, reserved/future-use, documentation, and benchmarking ranges.
      A ``goto http://10.0.0.5/admin`` followed by an auto-approved ``snapshot``
      prints internal infrastructure responses into the agent's context -- the
      same SSRF vector as link-local, aimed at internal services rather than
      the metadata endpoint.

    Ranges are tested by ``ipaddress``' own ``is_global`` property (True only
    for globally-routable addresses), applied to both the address itself and
    any embedded IPv4 (``ipv4_mapped``, ``sixtofour``). ``is_global`` subsumes
    loopback, link-local, unspecified, private, CGNAT/shared, multicast,
    reserved, documentation, and benchmarking ranges in one predicate, without
    hand-rolled CIDRs.

    DNS names are NOT resolved. Resolving inside the approval predicate is a
    blocking network call on the hot path AND a DNS-rebinding TOCTOU: a name can
    answer a public address at approval time then resolve to a private one when
    the browser re-resolves milliseconds later. The residual risk (a public name
    pointing at a private address) is accepted; browser-side network policy is
    the correct mitigation layer for that class.

    Ordinary public http(s) browsing is unaffected and stays auto-approved.
    """
    lowered = host.lower().rstrip(".")
    if not lowered:
        return False
    if lowered in _LOOPBACK_HOST_NAMES or lowered.endswith(".localhost"):
        return False
    try:
        addr: Any = ipaddress.ip_address(lowered)
    except ValueError:
        # Not an address literal, so it can only be a name -- and only when it
        # actually looks like one. See `_DNS_HOST_RE`.
        if len(lowered) > _MAX_DNS_NAME_LEN:
            return False
        return _DNS_HOST_RE.fullmatch(lowered) is not None
    candidates = [addr]
    for embedding in ("ipv4_mapped", "sixtofour"):
        embedded = getattr(addr, embedding, None)
        if embedded is not None:
            candidates.append(embedded)
    return all(c.is_global for c in candidates)


def _is_safe_browser_cli_argument(arg: str) -> bool:
    """Whether a page verb's positional argument is safe to auto-approve.

    A positional that is not URI-shaped is ordinary page input -- an element ref,
    a key name, literal text -- and passes. A URI-shaped one passes only as plain
    http(s) to a host :func:`_is_remote_navigable_host` accepts; every other
    shape falls through to interactive approval, because a non-http scheme is not
    "a page action" at all:

    * ``file:`` reads local disk into the page, and the next ``snapshot`` prints
      it into the agent's context -- an arbitrary local file read.
    * ``data:`` and ``javascript:`` inject script into the page.
    * ``view-source:`` does both.

    So the rule matches the one used for flags: recognized shape or refuse.
    """
    m = _URI_SCHEME_RE.match(arg)
    if m is None:
        return True  # not URI-shaped: an element ref, a key name, literal text
    if m.group(1).lower() not in ("http", "https"):
        return False
    # Refuse before parsing anything a browser and `urlsplit` read DIFFERENTLY.
    # `urlsplit` follows RFC 3986; a browser follows the WHATWG URL spec, and
    # where they disagree the browser's answer is the one that gets navigated:
    #
    #   * a backslash is a path separator in a special scheme, so
    #     `http://<target>\@innocuous/` ends its authority at the backslash and
    #     navigates to <target> -- while `urlsplit` reads everything before the
    #     last `@` as userinfo and reports `innocuous` as the host, which is the
    #     value this guard would have checked.
    #   * tab, CR and LF are STRIPPED from a URL before parsing, so they can be
    #     inserted mid-host to break up a literal the guard would recognize.
    #
    # There is no safe spelling of either inside an http(s) URL a page actually
    # needs, so an argument carrying one costs an approval prompt rather than
    # being reconciled between two parsers.
    if "\\" in arg or any(ch in arg for ch in ("\t", "\n", "\r")):
        return False
    try:
        host = urllib.parse.urlsplit(arg).hostname
    except ValueError:
        return False  # unparseable authority -- cannot reason about it
    if not host:
        return False
    return _is_remote_navigable_host(host)


def _unquoted_shell_hazard(text: str) -> str | None:
    """Name the first shell construct in *text* the CLI never sees, or ``None``.

    Load-bearing for approval, not cosmetic: every construct here is performed by
    the SHELL before the command runs, so the verb and flag allowlists cannot see
    it. They inspect the tokens they are handed; the shell decides what those
    tokens become.

    * **Redirection.** An otherwise-approved ``playwright-cli snapshot`` with
      ``> somefile`` appended CREATES OR TRUNCATES that file. The segment splitter
      does not cut on ``>``, so ``>`` and the path arrive as ordinary positionals
      and the whole thing reads as "snapshot with two extra arguments".
    * **Expansion.** ``open "${PATH:+file:///etc/passwd}"`` is not URI-shaped when
      the guard sees it, so it passes as ordinary page input — and the shell then
      expands it into a ``file://`` URL, making the next ``snapshot`` an arbitrary
      local file read. ``$VAR`` and backticks are the same mechanism, and so is
      brace expansion: ``{file:///etc/passwd,}`` expands to that URL with no
      variable and no substitution involved. A leading ``~`` expands to a home
      directory the same way.

    Globbing (``*``, ``?``, ``[]``) is deliberately NOT treated as a hazard, and
    the asymmetry is the point: brace and tilde expansion ALWAYS rewrite the
    token, while an unmatched glob is left literal by the shell — and refusing
    ``?`` would deny every URL carrying a query string, which is most of them.
    A glob that does match names a local file, and no auto-approved verb takes a
    local path as a positional.

    One quote-aware walker serves all of it, because a second shell parser is how
    a bypass gets introduced. Quote rules are the shell's own: single quotes make
    everything literal, so ``type 'price is $5'`` and ``click "div > span"`` are
    legitimate arguments and stay approved; a backslash escapes the next
    character everywhere except inside single quotes.
    """
    quote: str | None = None
    escaped = False
    at_word_start = True
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            continue
        # Double quotes suppress word splitting and globbing but NOT parameter or
        # command substitution, so `$` and a backtick stay dangerous inside them.
        if ch in ("$", "`"):
            return "expansion"
        if ch in ("{", "}"):
            return "brace-expansion"
        if quote == '"':
            if ch == '"':
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch in "><":
            return "redirection"
        if ch == "~" and at_word_start:
            return "tilde-expansion"
        at_word_start = ch.isspace()
    return None


def _is_browser_cli_command(tool_title: str) -> bool:
    """True when EVERY segment of a shell command is an auto-approvable
    ``playwright-cli`` page-scoped verb.

    Matched against the REAL command recovered from ``tool_input`` — never the
    model-authored title, which an injected agent controls and could forge.
    Reuses :func:`_split_command_segments`, so command substitution and quoted
    separators are handled by the one hardened splitter.
    """
    split = _split_command_segments(tool_title)
    if split is None:
        return False
    _, segments = split
    if not segments:
        return False
    for seg in segments:
        # BEFORE tokenizing: redirection and expansion are the shell's work, not
        # the CLI's, so no amount of verb checking can make them safe.
        if _unquoted_shell_hazard(seg) is not None:
            return False
        try:
            tokens = shlex.split(seg)
        except ValueError:
            return False  # unbalanced quotes — cannot reason about it
        if len(tokens) < 2 or tokens[0] != _BROWSER_CLI_BIN:
            return False
        # Separate flags from positionals. Every flag must be recognized as
        # path-free and code-free; an UNKNOWN flag denies the whole command
        # rather than being skipped over on the way to the verb.
        positionals: list[str] = []
        for tok in tokens[1:]:
            if tok.startswith("-"):
                name, _, value = tok.partition("=")
                if name not in _BROWSER_CLI_SAFE_FLAGS:
                    return False
                # A session name becomes a directory under the CLI's own data
                # dir, so a traversal-shaped value writes outside it. Restrict it
                # to a plain label. This also closes the same hole on the
                # pre-existing `-s`, which never validated its value.
                if name in _BROWSER_CLI_SESSION_FLAGS and not _SESSION_NAME_RE.fullmatch(value):
                    return False
            else:
                positionals.append(tok)
        if not positionals:
            return False
        verb = positionals[0]
        if verb in _BROWSER_CLI_PAGE_VERBS:
            # Positionals carry the navigation target, so they are validated
            # like flags are -- see `_is_safe_browser_cli_argument`.
            if not all(_is_safe_browser_cli_argument(a) for a in positionals[1:]):
                return False
            continue
        # Bare only: MEASURED, an output name is resolved against the CLI's CWD,
        # so any argument here is an arbitrary local write. Bare, both write into
        # the service's own directory.
        if verb in _BROWSER_CLI_BARE_ONLY_VERBS and len(positionals) == 1:
            continue
        return False
    return True
