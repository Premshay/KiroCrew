"""Provider-declared routing identities and admission."""

from types import SimpleNamespace

import pytest

from kiro_crew.decisions.points import spawn_route
from kiro_crew.decisions.vendors import VendorSpec, identify_vendor, validate_vendors
from kiro_crew.platform import context


def test_model_prefix_uses_most_specific_match():
    broad = VendorSpec("broad", model_prefixes=("model-",))
    specific = VendorSpec("specific", seat_tokens=("runner",), model_prefixes=("model-pro-",))
    specs = validate_vendors([broad, specific])

    assert identify_vendor(specs, "", "MODEL-PRO-7") == "specific"
    assert identify_vendor(specs, "worker-runner", "model-small") == "specific"


@pytest.mark.parametrize("seat", ["crew-local-claude", "crew-codex-local"])
def test_a_seat_with_two_vendor_segments_is_ambiguous(seat):
    specs = tuple(VendorSpec(name, (name,)) for name in ("claude", "codex", "local"))

    assert identify_vendor(specs, seat) == ""
    assert identify_vendor(tuple(reversed(specs)), seat) == ""


def test_a_partial_seat_segment_does_not_name_a_vendor():
    specs = (VendorSpec("local", ("local",)), VendorSpec("claude", ("claude",)))

    assert identify_vendor(specs, "crew.claude/localize") == "claude"
    assert identify_vendor(specs, "crew-localize") == ""


@pytest.mark.parametrize(
    "spec",
    [
        VendorSpec("same"),
        VendorSpec("bad id"),
        VendorSpec("another", seat_tokens=("two-parts",)),
        VendorSpec("another", seat_tokens=("worker",)),
        VendorSpec("another", model_prefixes=("model-",)),
    ],
)
def test_refuses_duplicate_or_invalid_metadata(spec):
    with pytest.raises(ValueError):
        validate_vendors([VendorSpec("same", ("worker",), ("model-",)), spec])


def test_only_registered_unmetered_vendors_can_receive_cross_routes(monkeypatch):
    providers = SimpleNamespace(
        routing_vendors=lambda: (
            VendorSpec("free", ("runner",), ("model-free-",)),
            VendorSpec("metered", ("paid",), ("model-paid-",), metered=True),
        ),
    )
    monkeypatch.setattr(context, "current_context", lambda: SimpleNamespace(providers=providers))
    monkeypatch.setattr(
        "kiro_crew.execution_seats.declared_execution_seats",
        lambda cfg: {
            "team-runner": {"runtime_agent": "runner"},
            "team-paid": {"runtime_agent": "paid"},
            "unknown": {"runtime_agent": "unknown"},
        },
    )

    assert spawn_route.vendor_of_seat("", "model-paid-v1") == "metered"
    assert spawn_route.route_backends("") == {
        "free": {"backend": "team-runner", "runtime_agent": "runner", "native": ""},
    }
