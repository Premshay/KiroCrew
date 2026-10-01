"""Task-budget advice independent of the gateway, model catalog and embedding runtime."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Literal, Mapping, Sequence

Budget = Literal["fast", "balanced", "frontier"]
BUDGETS: tuple[Budget, ...] = ("fast", "balanced", "frontier")


@dataclass(frozen=True)
class PreferenceExample:
    id: str
    group: str
    task: str
    role: str
    budget: Budget
    weight: float = 1.0


@dataclass(frozen=True)
class Advice:
    budget: Budget | None
    reason: str
    examples: tuple[str, ...] = ()
    support: float = 0.0
    margin: float = 0.0


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    if not all(math.isfinite(x) for x in (*left, *right)):
        return 0.0
    norm = math.sqrt(sum(x * x for x in left) * sum(x * x for x in right))
    return sum(a * b for a, b in zip(left, right)) / norm if norm else 0.0


def advise(
    task: str,
    role: str,
    examples: Sequence[PreferenceExample],
    encode: Callable[[str], Sequence[float] | None],
    *,
    continuation: bool = False,
    explicitly_pinned: bool = False,
    similarity_floor: float = 0.65,
    margin_floor: float = 0.2,
) -> Advice:
    """Retrieve reviewed preferences; support is a vote share, not a probability."""
    if not 0 <= similarity_floor <= 1 or not 0 <= margin_floor <= 1:
        raise ValueError("similarity and margin must be between zero and one")
    if continuation or explicitly_pinned:
        return Advice(None, "keep_current")
    eligible = [e for e in examples if e.role == role and e.budget in BUDGETS]
    if not task.strip() or not eligible:
        return Advice(None, "no_examples")
    query = encode(task)
    if not query:
        return Advice(None, "embedding_unavailable")
    groups: dict[str, tuple[float, PreferenceExample]] = {}
    group_budgets: dict[str, set[Budget]] = {}
    for example in eligible:
        group_budgets.setdefault(example.group, set()).add(example.budget)
    conflicting = {group for group, budgets in group_budgets.items() if len(budgets) > 1}
    for example in eligible:
        if example.group in conflicting:
            continue
        if not math.isfinite(example.weight) or not 0 < example.weight <= 1:
            continue
        vector = encode(example.task)
        if not vector:
            return Advice(None, "embedding_unavailable")
        similarity = _cosine(query, vector)
        if similarity < similarity_floor:
            continue
        score = similarity * example.weight
        if example.group not in groups or score > groups[example.group][0]:
            groups[example.group] = (score, example)
    neighbors = sorted(groups.values(), key=lambda row: (-row[0], row[1].id))[:5]
    if len(neighbors) < 2:
        return Advice(None, "insufficient_support")
    votes = {budget: sum(s for s, e in neighbors if e.budget == budget) for budget in BUDGETS}
    ranked = sorted(BUDGETS, key=lambda budget: -votes[budget])
    total = sum(votes.values())
    support = votes[ranked[0]] / total
    margin = (votes[ranked[0]] - votes[ranked[1]]) / total
    winners = tuple(e.id for _, e in neighbors if e.budget == ranked[0])
    if margin < margin_floor or len(winners) < 2:
        return Advice(None, "ambiguous", winners, support, margin)
    return Advice(ranked[0], "similar_preferences", winners, support, margin)


def resolve_budget_model(
    advice: Advice, mapping: Mapping[str, str], advertised: Sequence[str]
) -> str | None:
    model = mapping.get(advice.budget or "", "")
    return model if model and model in advertised and model not in ("auto", "auto:jev") else None
