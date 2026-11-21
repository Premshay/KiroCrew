"""Routing metadata supplied by a platform provider registry."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]*")
_SEAT_TOKEN = re.compile(r"[a-z0-9]+")
_SEAT_SEPARATOR = re.compile(r"[-_.:/]")
_MODEL_PREFIX = re.compile(r"[a-z0-9][a-z0-9_.:/-]*")


@dataclass(frozen=True)
class VendorSpec:
    id: str
    seat_tokens: tuple[str, ...] = ()
    model_prefixes: tuple[str, ...] = ()
    native_backend: str = ""
    metered: bool = False


def validate_vendors(specs: Iterable[VendorSpec]) -> tuple[VendorSpec, ...]:
    """Validate a whole registry before publishing it to the router."""
    result = tuple(specs)
    ids: set[str] = set()
    backends: set[str] = set()
    seat_tokens: set[str] = set()
    prefixes: set[str] = set()
    for spec in result:
        if not isinstance(spec, VendorSpec):
            raise ValueError("Expected a VendorSpec")
        if not isinstance(spec.id, str) or not _IDENTIFIER.fullmatch(spec.id) or spec.id in ids:
            raise ValueError(f"Invalid or duplicate routing vendor {spec.id!r}")
        ids.add(spec.id)
        if type(spec.metered) is not bool:
            raise ValueError("Expected a boolean metered flag")
        backend = spec.native_backend
        if not isinstance(backend, str) or (
            backend and (not _IDENTIFIER.fullmatch(backend) or backend in backends)
        ):
            raise ValueError(f"Invalid or duplicate native backend {backend!r}")
        if backend:
            backends.add(backend)
        for values, seen, pattern in (
            (spec.seat_tokens, seat_tokens, _SEAT_TOKEN),
            (spec.model_prefixes, prefixes, _MODEL_PREFIX),
        ):
            if not isinstance(values, tuple):
                raise ValueError("Expected a tuple of routing tokens")
            for token in values:
                if not isinstance(token, str) or not pattern.fullmatch(token) or token in seen:
                    raise ValueError(f"Invalid or duplicate routing token {token!r}")
                seen.add(token)
    return result


def routing_vendors() -> tuple[VendorSpec, ...] | None:
    """Return declared vendors, or None for registries using the legacy route."""
    from kiro_crew.platform.context import current_context

    getter = getattr(current_context().providers, "routing_vendors", None)
    if not callable(getter):
        return None
    try:
        specs = getter()
        return None if specs is None else validate_vendors(specs)
    except Exception:  # An unreadable provider must not admit fallback vendors.
        logger.warning("Ignoring invalid routing vendor registry", exc_info=True)
        return ()


def identify_vendor(specs: tuple[VendorSpec, ...], seat: str, model: str = "") -> str:
    """Prefer explicit IDs and seat tokens, then the longest matching model prefix."""
    seat = seat.lower()
    for spec in specs:
        if seat and seat in (spec.id, spec.native_backend):
            return spec.id
    segments = set(_SEAT_SEPARATOR.split(seat))
    matches = [spec.id for spec in specs if segments.intersection(spec.seat_tokens)]
    if matches:
        return matches[0] if len(matches) == 1 else ""
    model = model.lower()
    match = max(
        (
            (len(prefix), spec.id)
            for spec in specs
            for prefix in spec.model_prefixes
            if model.startswith(prefix)
        ),
        default=(0, ""),
    )
    return match[1]
