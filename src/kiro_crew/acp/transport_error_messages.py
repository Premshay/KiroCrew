"""Human-readable ACP failure recovery messages and typed raising."""

from __future__ import annotations

from typing import Sequence

from kiro_crew.acp.transport_error_classification import (  # noqa: F401 - facade compatibility surface
    _PROMPT_BUSY_RE,
    _RE_5XX_HINT,
    _RE_5XX_NAMED,
    _RE_5XX_PHRASE,
    _RE_5XX_STATUS,
    _RE_ADAPTER_INPUT_TOO_LONG,
    _RE_AUTH,
    _RE_CONNECTION,
    _RE_CONTEXT_WINDOW_OVERFLOW,
    _RE_GENERATE_FAILED,
    _RE_IMAGE_FORMAT_UNSUPPORTED,
    _RE_INVALID_MODEL_ID,
    _RE_MALFORMED_REQUEST,
    _RE_MODEL_TEMP_UNAVAILABLE,
    _RE_MODEL_UNAVAILABLE,
    _RE_OVERSIZED_REQUEST,
    _RE_PROCESS_FAILED,
    _RE_THROTTLE_GENERIC,
    _RE_THROTTLE_NAMED,
    _RE_USAGE_LIMIT,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_HOST_AUTH_CALLBACK,
    DEFAULT_MODEL,
    PROVIDER_ERROR_CONNECTION,
    PROVIDER_ERROR_THROTTLE,
    AcpContextWindowExceeded,
    AcpConversationBindingMismatch,
    AcpConversationPayloadExceeded,
    AcpError,
    AcpPromptBusy,
    _is_session_expired,
    _is_thinking_binding_mismatch,
    _is_transient_raw_error,
    _model_is_unentitled,
    _provider_detail,
    classify_provider_error,
    host_auth,
    is_credential_propagation_delay,
    logger,
    model_is_unusable,
    re,
    redact_credentials,
    redact_exfiltration_urls,
)


def _auto_remedy(available_models: Sequence[str] | None) -> str:
    """The "set agent.model to 'auto'" remediation step, or nothing when the
    partition does not serve ``auto``.

    The capacity-blip messages list three remedies, and the second is the
    ``auto`` sentinel. On a partition whose advertised list lacks ``auto`` that
    advice re-opens the circle the unentitled-``auto`` branch closes: the user
    follows it and the next turn dies on "no access to model 'auto'". So the
    step is emitted only when ``auto`` is served, or when the served list is
    unknown (nothing to check against, keep the historical advice). The
    numbering of the remaining step shifts so the list still reads (1)(2)(3)
    or (1)(2).
    """
    if model_is_unusable(DEFAULT_MODEL, available_models):
        return "or (2) "
    return f"(2) set agent.model to '{DEFAULT_MODEL}' in ~/.kiro/crew/config.json, or (3) "


def _format_acp_error(
    error: object,
    available_models: Sequence[str] | None = None,
    *,
    backend: str = "",
) -> str:
    """Format a JSON-RPC error from the ACP backend into actionable user text.

    See :func:`_format_acp_error_worded`, which also reports whether the text
    came from a curated branch.
    """
    return _format_acp_error_worded(error, available_models, backend=backend)[0]


def _format_acp_error_worded(
    error: object,
    available_models: Sequence[str] | None = None,
    *,
    backend: str = "",
) -> tuple[str, bool]:
    """Format a JSON-RPC error, and say whether a curated branch worded it.

    The flag is False only for the fallback below, which shows the provider's own
    message or the raw error dict. Every other branch writes fixed recovery text.

    The ACP backend (kiro-cli or claude-agent-acp) surfaces upstream Bedrock
    failures as JSON-RPC ``error`` objects with shape
    ``{"code": int, "message": str, "data": str}``.  The ``data`` field
    typically contains the raw provider error string and a request_id.

    For known failure modes (model unavailable, throttling, auth) we rewrite
    the message into concrete recovery steps.  For everything else we fall
    back to the previous behaviour ``"Prompt error: <raw dict>"`` so we don't
    swallow new error shapes.

    The provider request_id is preserved in every variant so that operators
    can correlate against support tickets and Bedrock logs.

    Security: the ``data`` field originates from upstream and may contain
    credential patterns or exfiltration URLs (especially in the fallback
    path that echoes the raw dict).  The return value is therefore passed
    through ``redact_credentials`` and ``redact_exfiltration_urls`` before
    being raised to the dashboard / Slack / CLI surfaces.
    """
    worded = True
    if isinstance(error, dict):
        data = str(error.get("data", "") or "")
        message = str(error.get("message", "") or "")
        haystack = f"{data} {message}"

        req_id_match = re.search(
            r"request(?:_|\s+)id\s*[:=]\s*([0-9a-fA-F-]+)", data, re.IGNORECASE
        )
        req_id_suffix = f" (request_id: {req_id_match.group(1)})" if req_id_match else ""

        unentitled = _model_is_unentitled(data, available_models)

        # The fleet router admits requests before forwarding.  Its machine code
        # tells interactive callers to rebuild from bounded KiroCrew history;
        # do not leak the router's JSON error envelope to the user.
        if _CONVERSATION_PAYLOAD_EXCEEDED_RE.search(haystack):
            formatted = (
                "The provider conversation retained too much attachment data. "
                "The session must be rebuilt from bounded history before this request can run."
                f"{req_id_suffix}"
            )
        elif _CONTEXT_WINDOW_EXCEEDED_RE.search(haystack):
            formatted = (
                "The local model context window is full. The conversation must "
                "be rebuilt from a shorter history before this request can run."
                f"{req_id_suffix}"
            )
        # Entitlement failure: this account was never offered the model, so the
        # capacity/rollout advice below would be actively misleading (there is
        # nothing to wait for). Checked before the generic model-unavailable
        # branch because upstream uses the same string for both cases.
        elif unentitled:
            usable = [m.strip() for m in (available_models or []) if m and m.strip()]
            # Cap the list: an account with many models would otherwise bury the
            # message, and the picker shows the full set anyway.
            shown = ", ".join(usable[:8])
            more = f" (+{len(usable) - 8} more)" if len(usable) > 8 else ""
            if unentitled.strip().lower() == DEFAULT_MODEL:
                # The rejected id IS the "let the backend choose" sentinel: some
                # partitions do not serve it, so the usual "set agent.model to
                # 'auto'" advice would send the user in a circle. Every layer
                # that can name a model (session picker, per-agent pin, the
                # global default under Settings -> Chat) has to move off it.
                # The CAUSE is not named: the served list looks the same for a
                # regional partition and a plan/tier exclusion, so the message
                # states only what the evidence supports — not on this account.
                # The same row reaches the CLI, subagents and messaging
                # channels, where there is no picker and no Settings page, so
                # the config.json spelling of the default is named too.
                formatted = (
                    f"Your account does not have access to model '{unentitled}' — "
                    f"the automatic model choice is not available on your account. "
                    f"Available to you: {shown}{more}. Pick one of these in the "
                    f"model picker for this session, and change the default model "
                    f"under Settings → Chat (agent.model in ~/.kiro/crew/config.json) "
                    f"so new sessions do not start on 'auto' again. Retrying will "
                    f"not help."
                    f"{req_id_suffix}"
                )
            elif not model_is_unusable(DEFAULT_MODEL, available_models):
                # Same two-step shape as the branches around it (the error card
                # says "do both" under every entitlement row): the picker fixes
                # this session, the default stops the next one -- and here
                # 'auto' is served, so it is the natural value for the default.
                formatted = (
                    f"Your account does not have access to model '{unentitled}'. "
                    f"Available to you: {shown}{more}. Pick one of these in the "
                    f"model picker for this session, and change the default model "
                    f"under Settings → Chat if it is set to '{unentitled}' — set "
                    f"agent.model to 'auto' in ~/.kiro/crew/config.json to let the "
                    f"backend choose a model your plan includes. Retrying will not help."
                    f"{req_id_suffix}"
                )
            else:
                # A pinned model rejected on a partition that does not serve
                # ``auto`` either: recommending ``auto`` here would re-open the
                # circle the branch above closes, so only the picker is offered.
                formatted = (
                    f"Your account does not have access to model '{unentitled}'. "
                    f"Available to you: {shown}{more}. Pick one in the model picker "
                    f"for this session, and change the default model under "
                    f"Settings → Chat (agent.model in ~/.kiro/crew/config.json) if "
                    f"it is set to '{unentitled}'. Retrying will not help."
                    f"{req_id_suffix}"
                )
        # Bedrock model alias resolved to a version that is currently
        # unavailable (capacity throttle, region rollout in progress,
        # deprecated, etc.).
        elif _RE_USAGE_LIMIT.search(haystack):
            # Plan allowance exhausted. Quote the provider's own sentence rather
            # than paraphrasing it: it is the authoritative statement of WHICH
            # limit was hit, and the CLI shows exactly this, so the dashboard
            # matching it means the two surfaces cannot tell different stories.
            _limit_detail = _provider_detail(data)
            # The provider sentence has no trailing period, so add one before
            # appending guidance or the two run together as one sentence.
            if _limit_detail and _limit_detail[-1] not in ".!?":
                _limit_detail += "."
            formatted = (
                f"{_limit_detail} Retrying will not help until the limit resets. "
                f"Check your plan's usage allowance, or switch to a model or "
                f"account tier with remaining capacity."
                f"{req_id_suffix}"
            )
        elif _RE_CONTEXT_WINDOW_OVERFLOW.search(data):
            # The provider rejected the whole envelope it was sent. The formatter
            # is surface-blind: it cannot tell a first turn from a later one, so
            # it names neither and gives both remedies. Retrying on this native
            # session reproduces the failure either way; only the caller knows
            # which cause applies.
            formatted = (
                "The agent context exceeded the model window. "
                "Retrying on the same model session will not help. In an established "
                "conversation the accumulated history no longer fits, so start a fresh "
                "session; in a fresh session the always-loaded agent resources and "
                "tools already fill the window, so reduce them."
                f"{req_id_suffix}"
            )
        elif _RE_ADAPTER_INPUT_TOO_LONG.search(message):
            # The provider's sentence first (it is what the CLI prints), then
            # the fix. Surface-blind like its kiro-cli sibling above: it names
            # no dashboard control, only the remedy every surface has.
            formatted = (
                "This chat is too big for the model to read in one request (Input is "
                "too long), often because it holds many images such as screenshots. "
                "Retrying or switching models will not help. Start a fresh session."
                f"{req_id_suffix}"
            )
        elif _RE_IMAGE_FORMAT_UNSUPPORTED.search(data):
            # Deterministic image rejection. A new attachment can be repaired by
            # the sender; an image retained in native history needs a fresh
            # conversation. The formatter is surface-blind, so it names no
            # dashboard-only command.
            formatted = (
                "The model could not process an image in this conversation. "
                "Retrying the unchanged conversation will fail again. If this "
                "turn attached the image, remove it or re-encode it as PNG or "
                "JPEG; otherwise start a new conversation so retained image "
                "data is not replayed."
                f"{req_id_suffix}"
            )
        elif _RE_MODEL_UNAVAILABLE.search(data):
            model = str(_RE_MODEL_UNAVAILABLE.search(data).group(1))  # type: ignore[union-attr]
            formatted = (
                f"Model '{model}' is unavailable on the backend right now "
                f"(capacity throttle or region rollout). Try: (1) pick a "
                f"different model in the model picker, {_auto_remedy(available_models)}"
                f"wait a minute and retry."
                f"{req_id_suffix}"
            )
        elif _RE_MODEL_TEMP_UNAVAILABLE.search(data):
            # Same capacity/rollout rejection as above in kiro-cli >= 2.16
            # wording, which names no model. Rewritten for the same reasons as
            # its named sibling: the provider's advice quotes the '/model' TUI
            # command, which does nothing in the dashboard, Slack, or a cron —
            # and the "is unavailable on the backend" prose keeps the
            # _TRANSIENT_MARKERS string fallback recognising it for free.
            formatted = (
                f"The selected model is unavailable on the backend right now "
                f"(capacity throttle or region rollout). Try: (1) pick a "
                f"different model in the model picker, {_auto_remedy(available_models)}"
                f"wait a minute and retry."
                f"{req_id_suffix}"
            )
        elif _RE_THROTTLE_NAMED.search(haystack) or _RE_THROTTLE_GENERIC.search(haystack):
            # Throttle / rate limit (the spent usage limit above was checked
            # first). Worded by backend: only kiro-cli reaches the model through
            # Bedrock, so only it names Bedrock (POSITIVE identity per AGENTS.md
            # "Harness parity"; a "not claude" test would answer Bedrock for
            # untested harnesses). The verb stays "throttling" so
            # llm_helpers._TRANSIENT_MARKERS ("throttl") keeps classifying it
            # transient: a throttle IS retryable, unlike the usage-limit branch.
            subject = "Bedrock" if backend == ACP_BACKEND_KIRO else "The model provider"
            formatted = (
                f"{subject} is throttling requests. Try: (1) wait a few seconds "
                "and retry, or (2) switch to a different model in the picker."
                f"{req_id_suffix}"
            )
        elif is_credential_propagation_delay(haystack):
            # Not-yet-propagated credential (see is_credential_propagation_delay).
            # Ahead of the Bedrock-auth and session-expiry branches, which match
            # the same frame and would tell the operator to re-authenticate a
            # credential that is already valid. Names "credential-propagation
            # delay" because llm_helpers._TRANSIENT_MARKERS keys on that phrase,
            # so the string-fallback classifier recognises this wording too.
            formatted = (
                "The AWS credential was rejected as invalid — usually a transient IAM "
                "credential-propagation delay right after a credential is minted, in "
                "which case the same credential works within a few seconds. Retry in a "
                "moment; if it keeps failing the access key itself is invalid (deleted, "
                "rotated, or mistyped) — refresh your AWS credentials."
                f"{req_id_suffix}"
            )
        elif _RE_AUTH.search(haystack):
            # Bedrock auth failure — almost always missing/expired AWS
            # credentials.
            formatted = (
                "Bedrock authentication failed. Refresh your AWS credentials "
                "(e.g. re-run your SSO/login or 'aws sso login'), then retry. If "
                "the failure persists, check that the configured AWS profile has "
                "Bedrock InvokeModel access."
                f"{req_id_suffix}"
            )
        elif _is_session_expired(haystack):
            # Session expiry (401/403, or prose saying as much) — distinct from
            # the Bedrock credential errors above. Retrying or switching models
            # cannot succeed, so the message must not suggest either.
            #
            # The sign-in half comes from the harness's own declaration: which
            # command re-authenticates is a per-harness fact. This arm HAS
            # evidence -- a 401/403 or prose saying the session expired -- so it
            # takes the signed-out message, which is allowed to assert the state;
            # the standing caveat the panel renders unprobed is not.
            #
            # An empty *backend* resolves to the kiro declaration, because the
            # kiro id IS the empty string. That is correct rather than a fallback:
            # a caller reaching here with no backend in hand is on the shared
            # runtime, whose harnesses are the two on the host identity store.
            # The retry verdict stays hard-coded -- that is this arm's own finding
            # about the error, not sign-in advice.
            formatted = (
                "Your session has expired. "
                f"{host_auth.signed_out_message(backend)} "
                "Retrying or switching models will not help — this is a "
                "sign-in issue, not a backend error."
                f"{req_id_suffix}"
            )
        elif _RE_CONNECTION.search(haystack):
            formatted = (
                "Could not reach the model backend (connection refused, reset, "
                "or timed out). Retry in a moment. If it keeps happening, check "
                "that the backend endpoint is up and listening."
                f"{req_id_suffix}"
            )
        elif (
            _RE_5XX_NAMED.search(haystack)
            or _RE_5XX_STATUS.search(haystack)
            or _RE_5XX_HINT.search(haystack)
            or _RE_5XX_PHRASE.search(haystack)
        ):
            # Transient backend 5xx — Bedrock/Codewhisperer surfaces a
            # momentary InternalServerError (often wrapped in a
            # CodewhispererChatResponseStream ServiceError with a
            # "please try again" hint). Distinct from throttling: this is a
            # server-side blip, not a rate limit, so the guidance is just to
            # retry rather than switch models or back off.
            #
            # Match the combined `data + message` haystack so a 5xx token in
            # either field is caught. Scanning `message` is safe
            # here precisely because we require a real transient *token* (named
            # exception, HTTP/status-50[0234]/529, or an explicit retry hint):
            # -32603's canonical message is literally "Internal error", which
            # carries no such token, so a bare uncaught -32603 (malformed-
            # request, context-length, deterministic backend bug) still falls
            # through to the unknown-shape branch rather than being mis-told to
            # retry a condition that will never succeed. A bare numeric like
            # "max_tokens 500" is likewise not a status code and won't match.
            formatted = (
                "The model backend hit a transient error (HTTP 5xx). This is "
                "usually momentary — retry in a moment. If it keeps happening, "
                "switch to a different model in the picker."
                f"{req_id_suffix}"
            )
        elif _RE_GENERATE_FAILED.search(data):
            # kiro-cli's generic pre-stream generation failure ("Kiro failed
            # to generate a response"): the backend call died before a
            # response stream existed, so there is no request_id and no error
            # class. Almost always a momentary model capacity / rollout blip
            # — same guidance as the 5xx branch. Kept as its own branch (not
            # folded into _RE_5XX_HINT) because it matches `data` only, so a
            # stray phrase echo in the JSON-RPC `message` can't flip an
            # otherwise-terminal error to transient.
            formatted = (
                "The model failed to generate a response (transient error — the "
                "backend call died before streaming started, usually a momentary "
                "capacity blip). Retry in a moment; if it keeps happening, switch "
                "to a different model in the picker."
                f"{req_id_suffix}"
            )
        elif _CONTEXT_WINDOW_EXCEEDED_RE.search(haystack):
            formatted = (
                "The local model context window is full. The conversation must "
                "be rebuilt from a shorter history before this request can run."
                f"{req_id_suffix}"
            )
        elif _RE_PROCESS_FAILED.search(data):
            # kiro-cli's sibling wrapper ("The service failed to process the
            # request (request_id: ...)"): the backend call failed after the
            # turn was under way, so a request_id exists but no error class.
            # Same momentary-blip guidance as the branch above, scoped to
            # `data` for the same reason. The phrase is kept in the rewrite so
            # the string classifier in llm_helpers matches either form.
            formatted = (
                "The model backend failed to process the request (transient error, "
                "usually a momentary capacity blip). Retry in a moment; if it keeps "
                "happening, switch to a different model in the picker."
                f"{req_id_suffix}"
            )
        elif _RE_MALFORMED_REQUEST.search(data):
            # Structural rejection: the backend refused the payload
            # because of its shape, not a momentary fault. Retrying the same
            # payload will be rejected identically, so the guidance is to REPAIR
            # or reset the conversation rather than retry. /compact shrinks and
            # rebuilds the conversation; a new conversation drops whatever in the
            # accumulated context tripped the parser. Kept plain and
            # non-alarmist, matching the other branches. Matched against `data`
            # only via the shared _RE_MALFORMED_REQUEST, so a phrase echo in the
            # JSON-RPC `message` can't flip an unrelated error into this branch.
            #
            # Only `/compact` is named, because this formatter does not know
            # which surface renders its output and the reset command differs per
            # surface -- see docs/system-specs/common/error-handling.md.
            formatted = (
                "The request was rejected as malformed. This is a structural "
                "problem with the request, so retrying it as-is will not help. "
                "Try `/compact` to shrink and repair the conversation, or start "
                "a new conversation."
                f"{req_id_suffix}"
            )
        elif _PROMPT_BUSY_RE.search(data):
            # The backend still has an in-flight prompt on this session.
            # This means a previous turn didn't complete cleanly (tool stall,
            # timeout, or race between messages). The session will auto-recover
            # on the next attempt once the stale turn expires.
            #
            # Names no command, for the reason given in the malformed-request
            # branch above: this formatter is surface-blind. Telling the user to
            # "send `!restart`" would be wrong three ways -- it is a Slack-only
            # bang alias, it is owner-gated even there, and it restarts the
            # GATEWAY rather than the session. It is also unnecessary: the
            # handlers reset and re-queue on AcpPromptBusy by themselves (see
            # _on_acp_retryable in dashboard/chat_turn/acp_recovery.py).
            formatted = (
                "I'm still processing a previous request. Please wait a moment "
                "and try again; it clears on its own once the stale turn "
                "expires. If it persists, start a new conversation."
            )
        elif host_auth.reports_signed_out(backend, haystack):
            # The harness's OWN words for "no provider / no key", declared per
            # harness in ``host_auth``. Without this the answer fell through to the
            # branch below and reached the user as a raw -32603 frame that names
            # no fix; the declared message names the one that works.
            formatted = f"{host_auth.signed_out_message(backend)}{req_id_suffix}"
        else:
            worded = False
            # Unrecognised failure mode. Show the PROVIDER'S OWN message when
            # there is one — it is the true error, and the same words the CLI
            # prints, so the two surfaces agree. This is the path every provider
            # failure without a curated branch above takes; an over-broad 5xx
            # match would swallow such an error and report a momentary blip, so
            # the real cause would never reach the user at all.
            #
            # Falls back to the raw dict only when there is no usable detail
            # (empty/odd data), so a genuinely opaque shape still loses nothing.
            # Redaction below scrubs any embedded secrets either way.
            _detail = _provider_detail(data)
            if _detail:
                # Keep the JSON-RPC `message` too when it carries signal. For
                # -32603 it is the fixed boilerplate "Internal error" (pure
                # noise next to the provider text), but other codes use it as
                # the actual summary, and dropping it there would lose the only
                # description the error has.
                _summary = "" if message.strip().lower() == "internal error" else message.strip()
                if _summary and _summary.lower() not in _detail.lower():
                    formatted = f"{_summary}: {_detail}{req_id_suffix}"
                else:
                    formatted = f"{_detail}{req_id_suffix}"
            else:
                formatted = f"Prompt error: {error}"
    else:
        worded = False
        formatted = f"Prompt error: {error}"

    # Defense-in-depth: scrub any credentials or suspicious exfiltration URLs
    # that may have been embedded in the upstream provider response before
    # the message reaches dashboard / Slack / CLI surfaces.
    redacted, url_warnings = redact_exfiltration_urls(formatted)
    redacted, cred_warnings = redact_credentials(redacted)
    if url_warnings or cred_warnings:
        # Log so security review can spot when an upstream provider is
        # echoing sensitive content back. The warning lists are bounded
        # (one entry per match) and intentionally do NOT include the matched
        # values themselves — those have already been redacted.
        logger.warning(
            "ACP error contained sensitive content (scrubbed before raise): "
            "%d suspicious url(s), %d credential pattern(s)",
            len(url_warnings),
            len(cred_warnings),
        )
    return redacted, worded


# ---------------------------------------------------------------------------
_CONTEXT_WINDOW_EXCEEDED_RE = re.compile(r"\bcontext_window_exceeded\b", re.IGNORECASE)
_CONVERSATION_PAYLOAD_EXCEEDED_RE = re.compile(
    r"request too large\s*\(max\s*\d+\s*mb\).*accumulated images and attachments",
    re.IGNORECASE | re.DOTALL,
)


def _rejected_model_from_error(error: object) -> str | None:
    """Return the model id a prompt-time error reports as invalid/unavailable.

    Powers the reactive fallback in ``run_bg_oneliner``: on the SUBSTITUTE
    (background) path, a rejected model is retried once against the account's
    advertised list. Matches both the MPS ``Invalid model ID: X``
    ValidationException (including the ``auto`` sentinel where a partition does
    not serve it) and the ``The model 'X' is not
    available`` wording. Returns None when the error names no specific model.
    """
    if not isinstance(error, dict):
        return None
    data = f"{error.get('data', '')} {error.get('message', '')}"
    m = _RE_INVALID_MODEL_ID.search(data) or _RE_MODEL_UNAVAILABLE.search(data)
    return m.group(1) if m else None


def _error_kind_from_frame(error: object) -> str:
    """Return the structured ``errorKind`` a raw ACP frame's ``data`` carries.

    claude-agent-acp names some provider answers with a machine-readable kind
    alongside the prose (``{"code": -32603, "message": "Internal error",
    "data": {"errorKind": "cloud_credential_error"}}`` -- the same field the
    ``{'errorKind': 'unknown'}`` wrapper note at ``_RE_5XX_HINT`` describes).
    Only a ``data`` that IS a mapping is read: the formatter pastes a
    non-mapping ``data`` verbatim into the user-visible message, so matching
    the kind out of that string would be the prose path structured readers
    exist to avoid. Returns "" when the frame names no kind.
    """
    if not isinstance(error, dict):
        return ""
    data_field = error.get("data")
    if isinstance(data_field, dict):
        kind = data_field.get("errorKind")
        return kind if isinstance(kind, str) else ""
    return ""


def _raise_acp_error(
    error: object,
    available_models: Sequence[str] | None = None,
    *,
    backend: str = "",
) -> None:
    """Format and raise the appropriate :class:`AcpError` for *error*.

    Delegates formatting to ``_format_acp_error`` and raises a typed error for
    known recovery paths, including ``AcpPromptBusy`` and a context-window
    admission rejection. Other failures receive structural, model, auth, or usage tags from their raw frame.

    *available_models* is passed to BOTH the formatter and the transient
    classifier so a model-rejection's wording and its retry verdict are decided
    from the same evidence.

    *backend* only reaches the formatter, where an auth-expiry needs the failing
    harness's own sign-in message. It defaults to empty, which is the KIRO id
    rather than a sentinel: a caller with no backend in reach is on the shared
    runtime, and both harnesses there resolve tokens from kiro-cli's own store,
    so kiro's message is the right answer for it. The retry verdict does not
    depend on it.
    """
    formatted, worded = _format_acp_error_worded(error, available_models, backend=backend)
    # Detect prompt-busy from the raw error (before formatting rewrites it)
    raw_data = ""
    if isinstance(error, dict):
        raw_data = f"{error.get('data', '')} {error.get('message', '')}"
    if _CONVERSATION_PAYLOAD_EXCEEDED_RE.search(raw_data):
        raise AcpConversationPayloadExceeded(formatted, transient=False, user_worded=worded)
    if _CONTEXT_WINDOW_EXCEEDED_RE.search(raw_data):
        raise AcpContextWindowExceeded(formatted, transient=False, user_worded=worded)
    if _PROMPT_BUSY_RE.search(raw_data):
        raise AcpPromptBusy(formatted, user_worded=worded)
    # A binding mismatch changes only the exception TYPE and its retry verdict;
    # it is built here, not raised early, so the tags below still read the frame.
    err: AcpError
    if _is_thinking_binding_mismatch(error):
        err = AcpConversationBindingMismatch(formatted, transient=False, user_worded=worded)
    else:
        err = AcpError(
            formatted,
            transient=_is_transient_raw_error(error, available_models),
            user_worded=worded,
        )
    # Tag a network-path drop so the interactive retry ladder can wait out a
    # short outage instead of spending the fixed provider-error budget. Only a
    # transient verdict qualifies, so a terminal frame never becomes waitable.
    err.connection_failure = (
        bool(err.transient) and classify_provider_error(raw_data).kind == PROVIDER_ERROR_CONNECTION
    )
    err.throttle = (
        bool(err.transient) and classify_provider_error(raw_data).kind == PROVIDER_ERROR_THROTTLE
    )
    # Tag deterministic STRUCTURAL rejections so self-driving callers can
    # stop resending identical context. Keep the classifier data-scoped: a phrase
    # echoed only in JSON-RPC ``message`` cannot stamp an unrelated error. Four
    # answers carry the tag: context-window overflow, malformed request, image
    # validation failure, and kiro-cli's own refusal of a request it could not
    # shrink to fit -- each reproduced exactly by re-sending the same context.
    raw_data_field = str(error.get("data", "") or "") if isinstance(error, dict) else ""
    raw_message_field = str(error.get("message", "") or "") if isinstance(error, dict) else ""
    # The claude adapter's overflow rides in ``message`` (anchored match, see
    # _RE_ADAPTER_INPUT_TOO_LONG); every other tag below stays data-scoped.
    _context_overflow = bool(
        _RE_CONTEXT_WINDOW_OVERFLOW.search(raw_data_field)
        or _RE_ADAPTER_INPUT_TOO_LONG.search(raw_message_field)
    )
    _image_format_unsupported = bool(_RE_IMAGE_FORMAT_UNSUPPORTED.search(raw_data_field))
    if (
        _context_overflow
        or _RE_MALFORMED_REQUEST.search(raw_data_field)
        or _image_format_unsupported
        or _RE_OVERSIZED_REQUEST.search(raw_data_field)
    ):
        err.structural_terminal = True
    if _context_overflow:
        # Explicit even though the shared classifier reaches the same verdict:
        # subtype metadata and same-session retry eligibility stay inseparable.
        err.transient = False
        err.context_overflow = True
    if _image_format_unsupported:
        err.image_format_unsupported = True
    # Tag a model-rejection so the SUBSTITUTE (background) retry layer can pick a
    # served model; harmless on every other error (attributes just stay unset).
    rejected = _rejected_model_from_error(error)
    if rejected:
        err.rejected_model = rejected
        err.advertised = list(available_models or [])
    # Tag a session-expiry / rejected-credential answer so the dashboard can offer
    # the fix -- the Kiro sign-in card -- instead of a Continue that hits the same
    # wall. Decided from the raw frame, never from the prose, and only when the
    # formatter would have reached its sign-in branch: a Bedrock-named credential
    # exception (`_RE_AUTH`, a different remedy) or a usage-limit answer that
    # happens to carry a 401/403 is not a Kiro sign-in problem. Gated on the
    # harness signing in through Crew's own identity (harness parity H6): for a
    # harness with its own credential store the card would sign in the wrong
    # thing, so its 401 stays a plain error carrying that harness's remedy.
    if (
        not rejected
        and backend in ACP_BACKENDS_HOST_AUTH_CALLBACK
        and _is_session_expired(raw_data)
        and not _RE_AUTH.search(raw_data)
        and not _RE_USAGE_LIMIT.search(raw_data)
    ):
        err.auth_required = True
    # Tag a spent plan allowance the same way: from the raw frame, and only when
    # the formatter reached its usage-limit branch -- an entitlement rejection
    # that happens to carry limit wording keeps its own (served-model) remedy.
    # The sign-in tag above already withholds itself for this wording, so the
    # two tags are exclusive and the row's kind is unambiguous.
    if _RE_USAGE_LIMIT.search(raw_data) and not _model_is_unentitled(
        raw_data_field, available_models
    ):
        err.usage_limit = True
    # A stale-credential answer, read from the structured field (not the prose),
    # recycles the process instead of retrying it. Pinned non-transient: the
    # prose ends in "try again", and a retry would re-ask the one process that
    # cannot reload. Not ``auth_required``: that card signs in the wrong identity
    # for a harness that loads its own credentials (harness parity H6).
    if _error_kind_from_frame(error) == "cloud_credential_error":
        err.credential_reload = True
        err.transient = False
    raise err
