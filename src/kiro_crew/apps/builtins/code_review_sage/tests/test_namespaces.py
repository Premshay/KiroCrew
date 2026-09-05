"""Unit tests for namespaced learnings + config-driven model/effort settings.

Two features under test:
  1. Learnings grouped by namespace ("default" maps to common/ for backward
     compat; user namespaces live under namespaces/<name>/). Reviews load the
     UNION of the configured active namespaces.
  2. The review model + thinking effort are read from config.json's "review"
     section, with a fall-through to the agent default.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sage_lib import learning as L  # noqa: N812
from sage_lib import review_pool, store


def _pattern(title, **kw):
    p = {"title": title, "scope": "common", "repo_identity": "github.com/o/r",
         "dimension": "security", "impact": "high", "guidance": "guard carefully",
         "symptom_why": "it broke prod", "example": {"repo": "r", "ref": "#1", "text": "ex"}}
    p.update(kw)
    return p


def _set_active(root, names):
    """Write review.active_namespaces into the isolated root's config.json."""
    cfg_path = store.data_dir(root) / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg.setdefault("review", {})["active_namespaces"] = names
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _set_bindings(root, bindings):
    cfg_path = store.data_dir(root) / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg.setdefault("review", {})["namespace_bindings"] = bindings
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _source(owner="acme", repository="service"):
    return {
        "provider": "github",
        "host": "github.com",
        "owner": owner,
        "repository": repository,
    }


class TestNamespaceManagement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_namespace_always_present(self):
        self.assertEqual(L.list_namespaces(self.root), ["default"])

    def test_default_maps_to_common(self):
        # 'default' resolves to the common/ dir (backward compat with pre-namespace data).
        # `as_posix()` so the suffix assert holds on Windows, where str() renders backslashes.
        self.assertTrue(
            L.common_file(self.root, "default").as_posix().endswith("/common/learned-patterns.md")
        )
        self.assertEqual(L.common_file(self.root, "default"), L.common_file(self.root))

    def test_create_and_list(self):
        res = L.create_namespace("proj-a", self.root)
        self.assertTrue(res["ok"])
        self.assertIn("proj-a", L.list_namespaces(self.root))
        # non-default namespaces live under namespaces/<name>/
        self.assertTrue(
            L.common_file(self.root, "proj-a")
            .as_posix()
            .endswith("/namespaces/proj-a/learned-patterns.md")
        )

    def test_create_rejects_invalid_name(self):
        self.assertFalse(L.create_namespace("x", self.root)["ok"])           # too short
        self.assertFalse(L.create_namespace("Bad Name!", self.root)["ok"])   # spaces/punct
        self.assertFalse(L.create_namespace("default", self.root)["ok"])     # reserved

    def test_create_duplicate_rejected(self):
        L.create_namespace("dup", self.root)
        self.assertFalse(L.create_namespace("dup", self.root)["ok"])

    def test_delete_namespace(self):
        L.create_namespace("temp", self.root)
        self.assertTrue(L.delete_namespace("temp", self.root)["ok"])
        self.assertNotIn("temp", L.list_namespaces(self.root))

    def test_cannot_delete_default(self):
        self.assertFalse(L.delete_namespace("default", self.root)["ok"])

    def test_delete_rejects_path_traversal(self):
        # A crafted name must never escape namespaces/ and rmtree an arbitrary dir.
        for evil in ["../common", "../../etc", "..", "a/../../b", "/abs/path"]:
            res = L.delete_namespace(evil, self.root)
            self.assertFalse(res["ok"], f"{evil!r} should be rejected")
        # the real common ruleset is still present (nothing was deleted)
        self.assertTrue(L.common_file(self.root).exists())

    def test_namespace_dir_rejects_traversal(self):
        with self.assertRaises(ValueError):
            L._namespace_dir("../common", self.root)
        with self.assertRaises(ValueError):
            L._namespace_dir("a/b", self.root)

    def test_create_rejects_traversal(self):
        self.assertFalse(L.create_namespace("../evil", self.root)["ok"])


class TestNamespacedStaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stage_isolated_per_namespace(self):
        L.create_namespace("proj-a", self.root)
        L.stage_learning(_pattern("A-lesson"), "fix_introduce", self.root, namespace="proj-a")
        # the candidate landed in proj-a, NOT in default
        self.assertEqual(L.candidate_count(self.root, namespace="proj-a"), 1)
        self.assertEqual(L.candidate_count(self.root), 0)

    def test_consolidate_per_namespace(self):
        L.create_namespace("proj-a", self.root)
        L.stage_learning(_pattern("A-lesson"), "fix_introduce", self.root, namespace="proj-a")
        merged = "# ns\n\n" + L.render_pattern(L._normalize_pattern(_pattern("A-lesson")))
        res = L.consolidate_apply(merged, self.root, namespace="proj-a")
        self.assertTrue(res["ok"])
        self.assertEqual(res["namespace"], "proj-a")
        self.assertEqual(len(L.list_patterns(root=self.root, namespace="proj-a")), 1)
        # default namespace untouched
        self.assertEqual(len(L.list_patterns(root=self.root)), 0)


class TestActiveNamespaceUnion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)
        # seed a pattern in default (common) and one in proj-a
        L.consolidate_apply("# c\n\n" + L.render_pattern(L._normalize_pattern(_pattern("Default-lesson"))),
                            self.root)
        L.create_namespace("proj-a", self.root)
        L.consolidate_apply("# n\n\n" + L.render_pattern(L._normalize_pattern(_pattern("ProjA-lesson"))),
                            self.root, namespace="proj-a")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_only_active_by_default(self):
        _set_active(self.root, ["default"])
        titles = [p["title"] for p in L.list_patterns_for_review(self.root)]
        self.assertEqual(titles, ["Default-lesson"])

    def test_union_of_active_namespaces(self):
        _set_active(self.root, ["default", "proj-a"])
        titles = sorted(p["title"] for p in L.list_patterns_for_review(self.root))
        self.assertEqual(titles, ["Default-lesson", "ProjA-lesson"])

    def test_inactive_namespace_excluded(self):
        # proj-a active only -> default's pattern is NOT loaded
        _set_active(self.root, ["proj-a"])
        titles = [p["title"] for p in L.list_patterns_for_review(self.root)]
        self.assertEqual(titles, ["ProjA-lesson"])


class TestRepositoryNamespaceResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)
        L.consolidate_apply(
            "# default\n\n" + L.render_pattern(L._normalize_pattern(_pattern("Legacy rule"))),
            self.root,
        )
        L.create_namespace("service-rules", self.root)
        L.consolidate_apply(
            "# scoped\n\n" + L.render_pattern(L._normalize_pattern(_pattern("Service rule"))),
            self.root,
            namespace="service-rules",
        )
        _set_active(self.root, ["service-rules", "default"])
        _set_bindings(self.root, {
            "service-rules": {"scope": "repository", "repository": _source()},
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unrelated_repository_excludes_scoped_namespace(self):
        resolved = L.resolve_effective_rules(_source("other", "repo"), root=self.root)

        self.assertEqual(resolved["effective_namespaces"], ["default"])
        self.assertEqual([rule["pattern"]["title"] for rule in resolved["effective_rules"]], ["Legacy rule"])
        self.assertEqual(resolved["namespaces"][0]["reason"], "repository_binding_mismatch")

    def test_explicit_global_namespace_applies_to_every_repository(self):
        _set_bindings(self.root, {
            "service-rules": {"scope": "global"},
        })

        resolved = L.resolve_effective_rules(_source("other", "repo"), root=self.root)

        self.assertEqual(resolved["effective_namespaces"], ["service-rules", "default"])
        self.assertEqual(resolved["namespaces"][0]["reason"], "explicit_global_binding")

    def test_unbound_active_namespace_remains_legacy_global_with_warning(self):
        _set_bindings(self.root, {})

        resolved = L.resolve_effective_rules(_source("other", "repo"), root=self.root)

        self.assertEqual(resolved["effective_namespaces"], ["service-rules", "default"])
        self.assertEqual(resolved["namespaces"][0]["reason"], "legacy_active_namespace")
        self.assertTrue(any("service-rules" in warning for warning in resolved["warnings"]))

    def test_invalid_repository_binding_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "repository"):
            L.validate_namespace_bindings({
                "service-rules": {"scope": "repository", "repository": {"provider": "gitlab"}},
            })

    def test_rule_order_and_dedup_are_deterministic(self):
        L.create_namespace("global-rules", self.root)
        duplicated = _pattern("Duplicate rule")
        L.consolidate_apply(
            "# global\n\n" + L.render_pattern(L._normalize_pattern(duplicated)),
            self.root,
            namespace="global-rules",
        )
        L.consolidate_apply(
            L.common_file(self.root, "service-rules").read_text(encoding="utf-8")
            + "\n"
            + L.render_pattern(L._normalize_pattern(duplicated)),
            self.root,
            namespace="service-rules",
        )
        _set_active(self.root, ["service-rules", "global-rules", "service-rules"])
        _set_bindings(self.root, {
            "service-rules": {"scope": "global"},
            "global-rules": {"scope": "global"},
        })

        first = L.resolve_effective_rules(_source(), root=self.root)
        second = L.resolve_effective_rules(_source(), root=self.root)

        self.assertEqual(first, second)
        self.assertEqual(first["effective_namespaces"], ["service-rules", "global-rules"])
        duplicate = [rule for rule in first["effective_rules"] if rule["pattern"]["title"] == "Duplicate rule"]
        self.assertEqual(len(duplicate), 1)
        self.assertEqual(duplicate[0]["namespace"], "service-rules")


class TestConfigDrivenSettings(unittest.TestCase):
    """The reviewer model + effort are read from the review config section."""

    def test_effort_defaults_to_max(self):
        with patch.object(review_pool, "_get_review_settings",
                          return_value={"model": None, "effort": "max"}):
            self.assertEqual(review_pool.reviewer_info()["effort"], "max")

    def test_config_effort_override(self):
        with patch.object(review_pool, "_get_review_settings",
                          return_value={"model": None, "effort": "high"}):
            self.assertEqual(review_pool.reviewer_info()["effort"], "high")

    def test_config_model_override_wins(self):
        # an explicit config model beats whatever the agent json pins.
        with patch.object(review_pool, "_get_review_settings",
                          return_value={"model": "claude-sonnet-4.6", "effort": "max"}):
            info = review_pool.reviewer_info()
            self.assertEqual(info["model"], "claude-sonnet-4.6")
            self.assertEqual(info["model_source"], "config")

    def test_no_config_model_uses_agent_default_source(self):
        with patch.object(review_pool, "_get_review_settings",
                          return_value={"model": None, "effort": "max"}):
            self.assertEqual(review_pool.reviewer_info()["model_source"], "agent-default")

    def test_invalid_effort_falls_back(self):
        # _get_review_settings sanitizes; simulate a bad stored value via a tmp config.
        tmp = tempfile.mkdtemp()
        try:
            root = Path(tmp) / "apps" / "code-review-sage"
            store.ensure_layout(root)
            cfg_path = store.data_dir(root) / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["review"]["effort"] = "ludicrous"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            with patch.object(store, "app_root", return_value=root):
                # An invalid stored effort is sanitized back to the default
                # (_DEFAULT_EFFORT = "" = inherit the model/provider default).
                self.assertEqual(review_pool._get_review_settings()["effort"],
                                 review_pool._DEFAULT_EFFORT)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
