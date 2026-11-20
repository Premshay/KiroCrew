"""The ``context.shared_resources`` documents: one rule set for every session.

An operator who wants every agent session to follow the same document (a
delegation guide, a house style) would otherwise list it in each agent spec's
``resources``, and a copied or enrolled spec cannot be hand-edited without
blocking its sessions. ``context.shared_resources`` names those documents once;
:func:`load_shared_resources` reads them for a session, and
``inclusion.shared_resources_part`` decides who receives them.

Each entry is an absolute path or an absolute ``file://`` URI naming one file;
a relative, home-anchored or glob entry is refused with a warning, since there is
no project every session shares to anchor it at. A document is admitted as a seat's
own absolute resource is (:func:`kiro_crew.context._load_seat_resources`): no
symlink in its path, a regular non-sensitive file read through the bounded
:func:`kiro_crew.context.safe_read_file`, front matter stripped and a
``manual``/``auto``/``fileMatch`` document withheld
(:func:`kiro_crew.folder_steering.every_session_body`). Bodies are scrubbed with
``_neutralize_structural_markers`` before any frame is placed around them. A
missing, refused or unreadable file is skipped with a WARNING naming it, never an
error. Documents are admitted whole, in configured order, while they fit
:data:`SHARED_RESOURCES_MAX_CHARS`; one that does not fit is left out and named
in-band, so the session knows a shared rule exists that it was not given.

:func:`spec_delivered_paths` resolves what an agent spec's own ``resources``
already deliver, so a session whose spec lists a shared document does not get it
twice.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Collection
from pathlib import Path

logger = logging.getLogger("kiro_crew.context")

#: Combined size bound on the shared documents one session receives. The block is
#: appended as required (never trimmed by the background budget), so it needs its
#: own finite bound; a document past it is named, not sliced.
SHARED_RESOURCES_MAX_CHARS = 32_000


def configured_shared_resources() -> tuple[str, ...]:
    """The configured ``context.shared_resources`` entries; empty on any failure."""
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    try:
        section = getattr(ctx.KiroCrewConfig.load(), "context", None)
        entries = getattr(section, "shared_resources", None) or ()
    except Exception as exc:
        logger.warning("context.shared_resources unreadable: %s", type(exc).__name__)
        return ()
    return tuple(e for e in entries if isinstance(e, str) and e)


def shared_resource_path(entry: str) -> Path | None:
    """The absolute path *entry* names, or ``None`` (logged) when it names none."""
    from kiro_crew.file_uri import file_uri_path_text

    text = file_uri_path_text(entry) if entry.startswith("file://") else entry
    if text.startswith("~") or not Path(text).is_absolute():
        logger.warning("shared resource %s refused: not an absolute path", entry)
        return None
    if any(c in text for c in "*?["):
        logger.warning("shared resource %s refused: globs are not supported", entry)
        return None
    return Path(text)


def _declared_paths(declared: object, project: str | None, *, relative: bool) -> set[Path]:
    """Resolved files a spec's ``resources`` list names.

    Absolute ``file://`` entries always count. A relative or ``~/`` entry counts
    only when *relative* says the session's harness loads one (kiro-cli natively,
    the default agent's steering load, a member envelope); a Claude Code or Codex
    seat loads only its absolute entries.
    """
    from kiro_crew.context_assembly.agent_spec import _steering_glob_anchor, seat_loads_resource
    from kiro_crew.file_uri import file_uri_path_text

    project_root = Path(project).expanduser() if project else None
    found: set[Path] = set()
    for res in declared if isinstance(declared, list) else []:
        if not isinstance(res, str) or not res.startswith("file://"):
            continue
        if not relative and not seat_loads_resource(res):
            continue
        try:
            anchor, pattern = _steering_glob_anchor(file_uri_path_text(res), project_root)
            found.update(p.resolve() for p in anchor.glob(pattern))
        except Exception as exc:
            logger.debug("spec resource %s not resolved: %s", res, type(exc).__name__)
    return found


def spec_delivered_paths(
    templates: Collection[str],
    project: str | None,
    *,
    relative: bool,
) -> set[Path]:
    """Resolved files the named agent specs already list in ``resources``.

    A spec that cannot be read contributes nothing, so a shared document it might
    have listed is sent anyway: a duplicate, never a loss.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.agent_files import is_kirocrew_default_agent
    from kiro_crew.member_essential_context import read_template_spec

    found: set[Path] = set()
    for template in dict.fromkeys(t for t in templates if t):
        try:
            if is_kirocrew_default_agent(template):
                spec = _read_agent_spec(
                    ctx.kiro_agents_dir() / "kirocrew.json",
                    operation="shared_resources",
                    source="context",
                )
            else:
                resolved = read_template_spec(
                    template, project, operation="shared_resources", source="context"
                )
                spec = resolved[1] if resolved is not None else None
        except Exception as exc:
            logger.debug("spec %s unreadable for shared dedup: %s", template, type(exc).__name__)
            continue
        if isinstance(spec, dict):
            found |= _declared_paths(spec.get("resources", []), project, relative=relative)
    return found


def load_shared_resources(
    entries: Collection[str],
    *,
    delivered: Collection[Path] = (),
    max_chars: int = SHARED_RESOURCES_MAX_CHARS,
) -> str:
    """The scrubbed text of each shared document not in *delivered*, joined.

    ``""`` when nothing is left to send. A document is read once even when two
    entries name it.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.context_assembly.agent_spec import steering_target_admissible
    from kiro_crew.folder_steering import every_session_body

    seen: set[Path] = {Path(p) for p in delivered}
    parts: list[str] = []
    omitted: list[str] = []
    used = 0
    for entry in entries:
        path = shared_resource_path(entry)
        if path is None:
            continue
        try:
            resolved = path.resolve()
            if resolved in seen:
                continue
            if not path.exists():
                logger.warning("shared resource %s skipped: file not found", entry)
                continue
            if resolved != Path(os.path.abspath(path)):
                logger.warning("shared resource %s refused: path holds a symlink", entry)
                continue
            if not steering_target_admissible(resolved, base=resolved.parent):
                logger.warning("shared resource %s refused: not admissible", entry)
                continue
            body = every_session_body(ctx.safe_read_file(str(path)))
        except Exception as exc:
            logger.warning("shared resource %s unreadable: %s", entry, type(exc).__name__)
            continue
        seen.add(resolved)
        if body is None:
            logger.warning("shared resource %s withheld: its inclusion is not always", entry)
            continue
        text = ctx._neutralize_structural_markers(body.strip("\n"))
        if used + len(text) > max_chars:
            logger.warning(
                "shared resource %s omitted: %d chars past the %d-char cap",
                entry,
                len(text),
                max_chars,
            )
            omitted.append(str(path))
            continue
        used += len(text)
        parts.append(f"--- {path} ---\n{text}")
    if omitted:
        parts.append(
            "[Shared resources not included (over the size cap): "
            + ", ".join(omitted)
            + ". Read them with your file tools if the task needs them.]"
        )
    return "\n\n".join(parts)
