"""The synced core splits preserve every patch seam used by the test corpus."""

from __future__ import annotations

import importlib
import sys
from functools import lru_cache

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror

_KNOWN = {
    "kiro_crew.channel": {"_APPROVAL_TIMEOUT_SECS", "atomic_write"},
    "kiro_crew.agent_capabilities": {"atomic_write", "member_backend_warning"},
    "kiro_crew.acp.transport_errors": {"_is_transient_raw_error"},
    "kiro_crew.apps.builtins.design_critique.backend.routes": {
        "app_data_dir",
        "redact_credentials",
        "redact_exfiltration_urls",
    },
}


@lru_cache(maxsize=1)
def _patches() -> dict[str, set[str]]:
    spellings = {name: name for name in _KNOWN}
    # Include source modules that import a facade, so handler.facade.foo patches
    # are measured as well as direct imports and dotted patch strings.
    import ast

    for path, _text, tree in source_corpus.parsed_candidates(
        require_any=("channel", "agent_capabilities", "transport_errors", "design_critique")
    ):
        rel = path.relative_to(source_corpus.src_root()).with_suffix("")
        module = ".".join(("kiro_crew", *rel.parts)).removesuffix(".__init__")
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    target = f"{node.module}.{alias.name}"
                    if target in _KNOWN and module != target:
                        spellings[f"{module}.{alias.asname or alias.name}"] = target
    result = {name: set() for name in _KNOWN}
    root = source_corpus.repo_root()
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if not (
            rel == "conftest.py"
            or rel.startswith("test/")
            or (rel.startswith("src/kiro_crew/apps/builtins/") and "/tests/" in rel)
        ):
            continue
        text = path.read_text(encoding="utf-8")
        found = facade_mirror_scan.text_patched_names(text, spellings)
        if facade_mirror_scan.needs_syntax_tree(text, spellings):
            events = facade_mirror_scan.patch_events(text, spellings)
            for target, names in facade_mirror_scan.names_by_target(events, _KNOWN).items():
                found.setdefault(target, set()).update(names)
        for target in result:
            result[target].update(found.get(target, ()))
    return result


@pytest.mark.parametrize("module", _KNOWN)
def test_every_scanned_patch_reaches_all_holders(module):
    facade = importlib.import_module(module)
    index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    # Keep pinned seams covered independently of current patch sites; scans
    # add coverage rather than defining the floor.
    names = _patches()[module] | _KNOWN[module]
    family = [facade, *(sys.modules[name] for name in index.owners)]
    for name in sorted(names):
        if not hasattr(facade, name):
            continue  # A test's raising=False creation seam.
        holders = [owner for owner in family if name in vars(owner)]
        replacement = object()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(facade, name, replacement)
            assert getattr(facade, name) is replacement
            assert all(vars(owner)[name] is replacement for owner in holders), (module, name)
    assert index.divergent() == ()


def test_moved_channel_records_keep_the_original_identity():
    from kiro_crew import channel

    for name in ("ListenMode", "ApprovalPolicy", "ChannelMessage", "ChannelAgent", "AttachRequest"):
        assert getattr(channel, name).__module__ == "kiro_crew.channel"


def test_capability_safe_view_reads_a_facade_patch(monkeypatch):
    from kiro_crew import agent_capabilities

    monkeypatch.setattr(
        agent_capabilities, "redact_via_context", lambda text: text.replace("secret", "x")
    )
    assert agent_capabilities.safe_view("secret") == "[REDACTED]"
