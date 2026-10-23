"""Credential-safe rendering of capability transport documents."""

from __future__ import annotations

from typing import Any

from kiro_crew.platform import redact_via_context


def safe_view(value: Any) -> Any:
    """Keep wire types intact; credential-bearing maps never expose their values."""
    return _safe_view(value, ())


def _argument_secrets(args: Any) -> list[str]:
    """Collect credential option values before masking any transport leaves.

    Keep argv intact: an inline option is masked as one scalar so retain_paths
    restores the exact original argument, not a reconstructed command line.
    """
    import re

    if not isinstance(args, list):
        return []
    values = []
    credential_value = False
    options_ended = False
    for index, arg in enumerate(args):
        if not isinstance(arg, str):
            credential_value = False
            continue
        if credential_value:
            if arg:
                values.append(arg)
            credential_value = False
            continue
        if arg == "--":
            options_ended = True
            continue
        option, separator, value = arg.partition("=")
        # Complete environment assignments are credential-bearing without an
        # option prefix (including values passed to -e/--env). A bare name must
        # never consume the following argv element as a credential value.
        assignment = bool(separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", option))
        # The option terminator does not terminate environment assignments.
        if options_ended and not assignment:
            continue
        # -u is also used for non-auth flags (for example Python unbuffered).
        # Mask only its colon-bearing basic-auth shape, not arbitrary short options.
        if arg in ("-u", "--user") and index + 1 < len(args):
            candidate = args[index + 1]
            if isinstance(candidate, str) and ":" in candidate:
                values.append(candidate)
        elif arg.startswith("-u") and ":" in arg[2:]:
            values.append(arg[2:])
        elif arg.startswith("--user=") and ":" in arg[7:]:
            values.append(arg[7:])
        # Match names, not credential formats or minimum secret lengths.
        if (option.startswith("-") or assignment) and re.search(
            r"(?:^|[-_])(?:key|token|secret|password|passwd|passphrase|credentials?|auth|authorization)$",
            re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", option),
            re.IGNORECASE,
        ):
            if separator:
                if value:
                    values.append(value)
            else:
                credential_value = True
    return values


def _safe_view(value: Any, inherited_secrets: tuple[str, ...]) -> Any:
    """Carry known credentials through their subtree, not unrelated response rows."""
    from urllib.parse import urlsplit

    if isinstance(value, dict):
        private_values = (
            [
                v
                for field in ("env", "headers")
                for v in (value.get(field) or {}).values()
                if isinstance(v, str) and v
            ]
            if all(isinstance(value.get(field, {}), dict) for field in ("env", "headers"))
            else []
        )
        oauth = value.get("oauth")
        if isinstance(oauth, dict):
            secret = oauth.get("clientSecret")
            if isinstance(secret, str) and secret:
                private_values.append(secret)
        private_values.extend(_argument_secrets(value.get("args")))
        private_values.extend(inherited_secrets)
        secrets = tuple(private_values)
        result: dict[str, Any] = {}
        for key, item in value.items():
            field = str(key).lower()
            if field in {"env", "headers"} and isinstance(item, dict):
                result[redact_via_context(str(key))] = {
                    redact_via_context(str(k)): "[REDACTED]" if v else v for k, v in item.items()
                }
            elif field == "url" and isinstance(item, str):
                try:
                    parsed = urlsplit(item)
                    result[key] = (
                        "[REDACTED]"
                        if parsed.username or parsed.password or parsed.query
                        else _safe_view(item, secrets)
                    )
                except ValueError:
                    result[key] = "[REDACTED]"
            else:
                result[redact_via_context(str(key))] = _safe_view(item, secrets)
        return result
    if isinstance(value, list):
        return [_safe_view(v, inherited_secrets) for v in value]
    if isinstance(value, str):
        if any(secret in value for secret in inherited_secrets):
            return "[REDACTED]"
        redacted = redact_via_context(value)
        return "[REDACTED]" if redacted != value else value
    return value


def _contains_mask(value: Any) -> bool:
    """A displayed secret is not a replacement value, even in a direct HTTP call."""
    if isinstance(value, str):
        return any(marker in value.lower() for marker in ("[redacted", "<redacted>", "***", "•••"))
    if isinstance(value, dict):
        return any(_contains_mask(v) for v in value.values())
    return isinstance(value, list) and any(_contains_mask(v) for v in value)
