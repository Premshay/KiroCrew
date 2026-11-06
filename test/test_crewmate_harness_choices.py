"""The runnable harness choices a Bot profile offers and accepts."""

from test_crewmate_harness import _cfg, _selectable  # noqa: F401


class TestCrewmateHarnesses:
    """The picker's one server-side list: selectable here AND crewmate-capable."""

    def test_only_selectable_and_crewmate_capable_harnesses_are_offered(self, monkeypatch):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import crewmate_harnesses

        # opencode/goose dispatch but have no member-spec capture; strands neither.
        selectable = {"", "kas", "claude", "codex", "opencode", "goose", "strands"}
        monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset(selectable))
        assert crewmate_harnesses(_cfg()) == ["claude", "codex", "kas"]

    def test_a_capable_harness_that_is_not_selectable_is_not_offered(self, monkeypatch):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import crewmate_harnesses

        monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset({"", "kas"}))
        assert crewmate_harnesses(_cfg()) == ["kas"]

    def test_a_governance_lock_removes_the_harness(self, monkeypatch):
        """The ``agent_backend`` scope narrows ``selectable_backends()``; the list follows."""
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import crewmate_harnesses

        monkeypatch.setattr(
            backends, "selectable_backends", lambda: frozenset(backends._selectable)
        )
        try:
            backends.apply_selectable_denials({"claude", "codex"})
            offered = crewmate_harnesses(_cfg())
        finally:
            backends.apply_selectable_denials(set())
        assert "claude" not in offered and "codex" not in offered
        assert "kas" in offered

    def test_a_kiro_pin_is_offered_only_where_it_runs_on_kas(self):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import crewmate_harnesses

        assert "kiro" not in crewmate_harnesses(_cfg())
        backends.confirm_kiro_cli_offers_kas()
        assert "kiro" in crewmate_harnesses(_cfg())
        # The KAS bundle route keeps the kiro-cli launch, which is not capable.
        assert "kiro" not in crewmate_harnesses(_cfg(kas_bundle=True))

    def test_worker_limits_name_their_harnesses(self, monkeypatch):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import worker_harnesses

        monkeypatch.setattr(
            backends, "selectable_backends", lambda: frozenset({"", "kas", "claude", "codex"})
        )
        assert worker_harnesses(_cfg()) == sorted(
            b for b in ("kas", "claude", "codex") if b in backends.ACP_BACKENDS_MATE_WORKER_KIT
        )

    def test_a_kas_boot_proved_unstartable_is_not_offered(self, monkeypatch):
        """Enterprise boot recorded KAS cannot start: neither the picker nor a save offers it."""
        from kiro_crew import enterprise_boot
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import (
            crewmate_harnesses,
            harness_pin_rejected,
            worker_harnesses,
        )

        monkeypatch.setattr(
            backends, "selectable_backends", lambda: frozenset({"", "kas", "claude"})
        )
        monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
        monkeypatch.setattr(enterprise_boot, "_kas_refusal", ("KAS failed", "fix it"))
        cfg = _cfg()
        assert crewmate_harnesses(cfg) == ["claude"]
        assert "must be one of" in (harness_pin_rejected("kas", cfg) or "")
        assert "kas" not in worker_harnesses(cfg) and "claude" in worker_harnesses(cfg)
        # Nothing recorded: KAS is offered again.
        monkeypatch.setattr(enterprise_boot, "_kas_refusal", None)
        assert "kas" in crewmate_harnesses(cfg)

    def test_a_kiro_pin_on_kas_has_kas_worker_limits(self):
        """A worker checks its runtime, so a Kiro CLI pin that starts on KAS has KAS's kit."""
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import worker_harnesses

        assert "kiro" not in worker_harnesses(_cfg())
        backends.confirm_kiro_cli_offers_kas()
        assert "kiro" in worker_harnesses(_cfg())
        # The KAS bundle route keeps the kiro-cli launch: no worker kit there.
        assert "kiro" not in worker_harnesses(_cfg(kas_bundle=True))

    def test_the_save_accepts_exactly_the_offered_list(self, monkeypatch):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.crewmate_harness import crewmate_harnesses, harness_pin_rejected

        monkeypatch.setattr(
            backends, "selectable_backends", lambda: frozenset({"", "kas", "codex", "opencode"})
        )
        cfg = _cfg()
        for stored in crewmate_harnesses(cfg):
            assert harness_pin_rejected(stored, cfg) is None
        assert harness_pin_rejected("", cfg) is None
        for refused in ("kiro", "opencode", "claude"):
            assert "must be one of" in (harness_pin_rejected(refused, cfg) or "")
