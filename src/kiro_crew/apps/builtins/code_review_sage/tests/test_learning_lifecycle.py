"""Focused contracts for governed Sage learning-record lifecycle and prompt context."""

import shutil
import tempfile
import unittest
from pathlib import Path

from sage_lib import learning, review_driver, store


def _evidence(review: str, change: str) -> dict:
    return {"review": review, "change": change, "observed_at": "2026-09-02T00:00:00Z"}


def _record(
    record_id: str,
    lifecycle: str = "candidate",
    evidence: list[dict] | None = None,
    repository_identity: str | None = None,
    updated_at: str = "2026-09-02T00:00:00Z",
) -> dict:
    signals = evidence or [_evidence("review-" + record_id, "change-" + record_id)]
    return {
        "id": record_id,
        "text": record_id + " rule",
        "rule": "Check " + record_id + " carefully.",
        "namespace": "default",
        "scope": "common",
        "lifecycle": lifecycle,
        "origin": {"source": "fix_introduce", "reference": "origin-" + record_id},
        "repository_identity": repository_identity,
        "timestamps": {
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": updated_at,
            "archived_at": None,
        },
        "recurrence": {"count": max(1, len(signals)), "evidence": signals},
        "legacy": False,
    }


class TestLearningLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "apps" / "code-review-sage"
        store.ensure_layout(self.root)

    def _write_records(self, *records: dict) -> None:
        learning._write_learning_records(
            {
                "schema": learning.LEARNING_RECORDS_SCHEMA,
                "version": learning.LEARNING_RECORDS_VERSION,
                "records": list(records),
            },
            self.root,
            None,
        )

    def _config(self, global_rules: int = 60, repository_rules: int = 40) -> dict:
        return {
            "review": {
                "active_context": {
                    "global": {"max_rules": global_rules, "max_tokens": 12000},
                    "repository": {"max_rules": repository_rules, "max_tokens": 8000},
                }
            }
        }

    def test_recurrence_requires_independent_review_and_change(self) -> None:
        candidate = _record("candidate")
        self._write_records(candidate)

        duplicate = learning.record_recurrence_evidence(
            "candidate", _evidence("review-candidate", "another-change"), root=self.root
        )
        independent = learning.record_recurrence_evidence(
            "candidate", _evidence("review-two", "change-two"), root=self.root
        )

        self.assertFalse(duplicate["changed"])
        self.assertEqual(duplicate["reason"], "duplicate_recurrence_evidence")
        self.assertTrue(independent["changed"])
        self.assertEqual(independent["record"]["recurrence"]["count"], 2)
        promoted = learning.transition_learning_record(
            "candidate", "active", root=self.root, config=self._config()
        )
        self.assertEqual(promoted["record"]["lifecycle"], "active")

    def test_operator_pin_promotes_candidate_without_recurrence(self) -> None:
        self._write_records(_record("candidate"))

        with self.assertRaisesRegex(ValueError, "operator identity"):
            learning.transition_learning_record("candidate", "pinned", root=self.root)
        result = learning.transition_learning_record(
            "candidate", "pinned", operator="luna", root=self.root, config=self._config()
        )

        self.assertEqual(result["record"]["lifecycle"], "pinned")

    def test_invalid_transition_and_evidence_free_promotion_are_rejected(self) -> None:
        self._write_records(_record("candidate"))

        with self.assertRaisesRegex(ValueError, "requires independent recurrence"):
            learning.transition_learning_record("candidate", "active", root=self.root)
        with self.assertRaisesRegex(ValueError, "invalid lifecycle transition"):
            learning.transition_learning_record("candidate", "candidate", root=self.root)

    def test_bounded_selection_ranks_pin_then_evidence_then_recency_and_scope(self) -> None:
        pinned = _record("pinned", "pinned", updated_at="2026-09-01T00:00:00Z")
        evidence = _record(
            "evidence",
            "active",
            [_evidence("r1", "c1"), _evidence("r2", "c2")],
            updated_at="2026-09-01T00:00:00Z",
        )
        recent = _record("recent", "active", updated_at="2026-09-03T00:00:00Z")
        repository = _record(
            "repository",
            "active",
            repository_identity="github://github.com/acme/service",
            updated_at="2026-09-04T00:00:00Z",
        )
        self._write_records(pinned, evidence, recent, repository)

        resolved = learning.resolve_effective_rules(None, root=self.root, config=self._config(2))

        self.assertEqual(
            [rule["sidecar_record_ids"][0] for rule in resolved["effective_rules"]],
            ["pinned", "evidence"],
        )
        decisions = {
            item["record_id"]: item["reason"] for item in resolved["active_context"]["decisions"]
        }
        self.assertEqual(decisions["recent"], "excluded_global_rule_budget")
        self.assertEqual(decisions["repository"], "excluded_global_rule_budget")

    def test_unpinned_overflow_is_archived_not_deleted(self) -> None:
        preferred = _record("preferred", "active", [_evidence("r1", "c1"), _evidence("r2", "c2")])
        overflow = _record("overflow", "active")
        self._write_records(preferred, overflow)

        outcome = learning.enforce_active_context_budgets(
            root=self.root, config=self._config(global_rules=1)
        )
        records = {item["id"]: item for item in learning.load_learning_records(self.root)}

        self.assertEqual(outcome["archived"], ["overflow"])
        self.assertEqual(records["overflow"]["lifecycle"], "archived")
        self.assertIsNotNone(records["overflow"]["timestamps"]["archived_at"])
        self.assertIn("overflow", records)

    def test_repository_budget_applies_after_the_global_budget(self) -> None:
        self._write_records(
            _record(
                "repository-first", "active", repository_identity="github://github.com/acme/service"
            ),
            _record(
                "repository-second", "active", repository_identity="github://github.com/acme/other"
            ),
        )

        resolved = learning.resolve_effective_rules(
            None, root=self.root, config=self._config(global_rules=10, repository_rules=1)
        )

        self.assertEqual(
            [rule["sidecar_record_ids"][0] for rule in resolved["effective_rules"]],
            ["repository-first"],
        )
        decisions = {
            item["record_id"]: item["reason"] for item in resolved["active_context"]["decisions"]
        }
        self.assertEqual(decisions["repository-second"], "excluded_repository_rule_budget")

    def test_markdown_namespace_remains_compatible_without_a_populated_sidecar(self) -> None:
        pattern = learning._normalize_pattern(
            {
                "title": "Markdown rule",
                "scope": "common",
                "impact": "medium",
                "guidance": "Keep the documented compatibility behavior.",
            }
        )
        learning.common_file(self.root).write_text("# Rules\n\n" + learning.render_pattern(pattern))

        resolved = learning.resolve_effective_rules(None, root=self.root, config=self._config())

        self.assertEqual(
            [item["pattern"]["title"] for item in resolved["effective_rules"]], ["Markdown rule"]
        )
        self.assertEqual(resolved["namespaces"][0]["sidecar"], "legacy_markdown")

    def test_prompt_freezes_bounded_governed_records(self) -> None:
        self._write_records(
            _record("candidate"),
            _record("archived", "archived"),
            _record("pinned", "pinned"),
        )
        resolution = learning.resolve_effective_rules(None, root=self.root, config=self._config())
        prompt = review_driver.build_review_task(
            "https://github.com/acme/service/pull/7", resolution
        )

        self.assertIn('"sidecar_record_ids": ["pinned"]', prompt)
        self.assertNotIn("Check archived carefully.", prompt)
        self.assertNotIn("Check candidate carefully.", prompt)
        decisions = {
            item["record_id"]: item["reason"]
            for item in resolution["namespaces"][0]["record_decisions"]
        }
        self.assertEqual(decisions["candidate"], "excluded_lifecycle_candidate")
        self.assertEqual(decisions["archived"], "excluded_lifecycle_archived")

    def test_malformed_active_context_fails_closed(self) -> None:
        self._write_records(_record("active", "active"))
        bad_config = {"review": {"active_context": {"global": {"max_rules": 1}}}}

        resolved = learning.resolve_effective_rules(None, root=self.root, config=bad_config)

        self.assertTrue(resolved["active_context"]["fail_closed"])
        self.assertEqual(resolved["effective_rules"], [])

    def test_malformed_governing_sidecar_does_not_fall_back_to_markdown(self) -> None:
        learning.common_file(self.root).write_text(
            "# Rules\n\n"
            + learning.render_pattern(
                learning._normalize_pattern(
                    {
                        "title": "Unsafe fallback",
                        "scope": "common",
                        "impact": "medium",
                        "guidance": "Do not load.",
                    }
                )
            )
        )
        learning.learning_records_file(self.root).write_text(
            '{"schema":"code-review-sage-learning-records","version":1,"records":[{"id":"bad"}]}',
            encoding="utf-8",
        )

        resolved = learning.resolve_effective_rules(None, root=self.root, config=self._config())

        self.assertEqual(resolved["effective_rules"], [])
        self.assertEqual(resolved["namespaces"][0]["sidecar"], "invalid")


if __name__ == "__main__":
    unittest.main()
