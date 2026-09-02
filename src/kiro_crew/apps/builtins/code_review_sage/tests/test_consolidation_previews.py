"""Snapshot-safe confirmation contracts for Sage consolidation previews."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sage_lib import learning, store


def _pattern(title: str) -> dict:
    return {
        "title": title,
        "scope": "common",
        "impact": "high",
        "guidance": "Review " + title + " carefully.",
    }


class TestConsolidationPreviews(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "app"
        store.ensure_layout(self.root)

    def _stage(self, *titles: str) -> list[dict]:
        for title in titles:
            learning.stage_learning(_pattern(title), "fix_introduce", self.root)
        return learning.list_candidate(self.root)

    def _proposal(self, candidates: list[dict], ruleset: str | None = None, action: str = "merge") -> dict:
        rendered = ruleset or (
            "# Rules\n\n"
            + learning.render_pattern(learning._normalize_pattern(_pattern("Merged rule")))
        )
        return {
            "ruleset_markdown": rendered,
            "decisions": [
                {"candidate_id": item["id"], "action": action, "reason_code": "candidate_merged"}
                for item in candidates
            ],
        }

    def _preview(self, candidates: list[dict], **kwargs: object) -> dict:
        return learning.create_consolidation_preview(
            self._proposal(candidates),
            candidate_ids=[item["id"] for item in candidates],
            root=self.root,
            **kwargs,
        )

    def test_preview_of_a_subset_is_durable_and_does_not_mutate_any_live_file(self) -> None:
        candidates = self._stage("First", "Second")
        active_before = learning.common_file(self.root).read_bytes()
        candidate_before = learning.candidate_file(self.root).read_bytes()

        preview = self._preview([candidates[0]])

        self.assertEqual(preview["selected_candidate_ids"], [candidates[0]["id"]])
        self.assertEqual(preview["candidate_snapshot_digest"], learning._sha256_json(preview["candidate_snapshot"]))
        self.assertEqual(learning.common_file(self.root).read_bytes(), active_before)
        self.assertEqual(learning.candidate_file(self.root).read_bytes(), candidate_before)
        listed = learning.list_consolidation_previews(root=self.root)
        self.assertEqual([item["preview_id"] for item in listed], [preview["preview_id"]])

    def test_apply_consumes_only_the_selected_snapshot_and_keeps_concurrent_staging(self) -> None:
        candidates = self._stage("Selected", "Unselected")
        preview = self._preview([candidates[0]])
        learning.stage_learning(_pattern("Concurrent"), "fix_introduce", self.root)

        applied = learning.apply_consolidation_preview(
            preview["preview_id"], confirmed=True, root=self.root
        )

        self.assertTrue(applied["ok"])
        self.assertEqual(
            [item["title"] for item in learning.list_candidate(self.root)], ["Unselected", "Concurrent"]
        )
        self.assertEqual(
            [item["title"] for item in learning.list_patterns(root=self.root)], ["Merged rule"]
        )

    def test_unselected_or_unknown_ids_are_rejected_without_preview(self) -> None:
        candidates = self._stage("Only")
        with self.assertRaisesRegex(ValueError, "invalid_candidate_ids"):
            learning.create_consolidation_preview(
                self._proposal(candidates), candidate_ids=["not-a-candidate"], root=self.root
            )
        self.assertEqual(learning.list_consolidation_previews(root=self.root), [])
        self.assertEqual(learning.candidate_count(self.root), 1)

    def test_expired_stale_and_repeated_apply_cannot_consume_candidates(self) -> None:
        candidates = self._stage("Expiry")
        preview = self._preview(candidates, now=10.0)
        expired = learning.apply_consolidation_preview(
            preview["preview_id"], confirmed=True, root=self.root, now=1000.0
        )
        self.assertEqual(expired["code"], "preview_expired")
        self.assertEqual(learning.candidate_count(self.root), 1)

        fresh = self._preview(candidates)
        learning.common_file(self.root).write_text("# Changed\n", encoding="utf-8")
        stale = learning.apply_consolidation_preview(fresh["preview_id"], confirmed=True, root=self.root)
        self.assertEqual(stale["code"], "preview_stale")
        self.assertEqual(learning.candidate_count(self.root), 1)

        current = learning.list_candidate(self.root)
        applied_preview = self._preview(current)
        applied = learning.apply_consolidation_preview(
            applied_preview["preview_id"], confirmed=True, root=self.root
        )
        repeated = learning.apply_consolidation_preview(
            applied_preview["preview_id"], confirmed=True, root=self.root
        )
        self.assertTrue(applied["ok"])
        self.assertEqual(repeated["code"], "preview_already_applied")

    def test_malformed_worker_output_and_unconfirmed_apply_leave_state_intact(self) -> None:
        candidates = self._stage("Malformed")
        with self.assertRaisesRegex(ValueError, "malformed_worker_output"):
            learning.create_consolidation_preview(
                {"ruleset_markdown": "# prose", "decisions": []},
                candidate_ids=[candidates[0]["id"]],
                root=self.root,
            )
        preview = self._preview(candidates)
        self.assertEqual(
            learning.apply_consolidation_preview(preview["preview_id"], confirmed=False, root=self.root)["code"],
            "confirmation_required",
        )
        self.assertEqual(learning.candidate_count(self.root), 1)

    def test_failed_candidate_write_rolls_back_the_live_ruleset(self) -> None:
        learning.common_file(self.root).write_text(
            "# Existing\n\n" + learning.render_pattern(learning._normalize_pattern(_pattern("Existing"))),
            encoding="utf-8",
        )
        candidates = self._stage("Rollback")
        preview = self._preview(candidates)
        before = learning.common_file(self.root).read_bytes()
        real_write = learning._atomic_write

        def fail_candidate(path: Path, body: str) -> None:
            if path == learning.candidate_file(self.root):
                raise OSError("candidate write failed")
            real_write(path, body)

        with patch.object(learning, "_atomic_write", side_effect=fail_candidate):
            result = learning.apply_consolidation_preview(
                preview["preview_id"], confirmed=True, root=self.root
            )
        self.assertEqual(result["code"], "apply_rolled_back")
        self.assertEqual(learning.common_file(self.root).read_bytes(), before)
        self.assertEqual(learning.candidate_count(self.root), 1)

    def test_governed_and_legacy_sidecars_have_distinct_preview_contracts(self) -> None:
        candidates = self._stage("Legacy")
        legacy = self._preview(candidates)
        self.assertIsNone(legacy["proposed_sidecar_document"])
        learning.clear_candidate(self.root)
        governed_pattern = _pattern("Governed")
        learning.stage_learning(governed_pattern, "fix_introduce", self.root)
        candidate = learning.list_candidate(self.root)[0]
        record = {
            "id": "governed-record",
            "text": "Governed rule",
            "rule": candidate["guidance"],
            "namespace": "default",
            "scope": "common",
            "lifecycle": "candidate",
            "origin": {"source": "fix_introduce", "reference": "review-1"},
            "repository_identity": None,
            "timestamps": {"created_at": "2026-01-01T00:00:00Z", "updated_at": None, "archived_at": None},
            "recurrence": {
                "count": 2,
                "evidence": [
                    {"review": "one", "change": "one", "observed_at": "2026-01-01T00:00:00Z"},
                    {"review": "two", "change": "two", "observed_at": "2026-01-02T00:00:00Z"},
                ],
            },
            "legacy": False,
        }
        learning._write_learning_records(
            {"schema": learning.LEARNING_RECORDS_SCHEMA, "version": learning.LEARNING_RECORDS_VERSION, "records": [record]},
            self.root,
            None,
        )
        governed = self._preview([candidate])
        self.assertTrue(governed["budget_impact"]["governed"])
        self.assertEqual(governed["proposed_sidecar_document"]["records"][0]["lifecycle"], "active")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
