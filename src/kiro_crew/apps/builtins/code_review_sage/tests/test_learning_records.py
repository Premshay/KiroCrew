"""Tests for the explicit, versioned Sage learning-record sidecar."""

import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from sage_lib import learning, store


def _pattern(title: str) -> dict:
    return {
        "title": title,
        "scope": "common",
        "impact": "high",
        "guidance": f"Review {title.lower()} carefully.",
    }


class TestLearningRecordExport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "apps" / "code-review-sage"
        store.ensure_layout(self.root)

    def _write_active_and_candidate(self) -> tuple[Path, Path]:
        active = learning.common_file(self.root)
        candidate = learning.candidate_file(self.root)
        active.write_text(
            "# Active\n\n"
            + learning.render_pattern(learning._normalize_pattern(_pattern("Active rule"))),
            encoding="utf-8",
        )
        candidate.write_text(
            "# Candidate\n\n"
            + learning.render_pattern(learning._normalize_pattern(_pattern("Candidate rule"))),
            encoding="utf-8",
        )
        return active, candidate

    def test_reading_absent_sidecar_does_not_migrate_or_touch_markdown(self) -> None:
        active, candidate = self._write_active_and_candidate()
        active_before = active.read_text(encoding="utf-8")
        candidate_before = candidate.read_text(encoding="utf-8")

        self.assertEqual(learning.load_learning_records(self.root), [])

        self.assertFalse(learning.learning_records_file(self.root).exists())
        self.assertEqual(active.read_text(encoding="utf-8"), active_before)
        self.assertEqual(candidate.read_text(encoding="utf-8"), candidate_before)

    def test_export_creates_explicit_legacy_records_with_unknown_provenance(self) -> None:
        self._write_active_and_candidate()

        result = learning.migrate_legacy_learning_records(self.root)
        records = learning.load_learning_records(self.root)

        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], 2)
        self.assertEqual({record["lifecycle"] for record in records}, {"active", "candidate"})
        for record in records:
            self.assertTrue(record["id"].startswith("legacy-"))
            self.assertTrue(record["legacy"])
            self.assertEqual(record["origin"], {"source": "unknown", "reference": None})
            self.assertIsNone(record["repository_identity"])
            self.assertEqual(record["recurrence"], {"count": 1, "evidence": []})
            self.assertIn("created_at", record["timestamps"])

    def test_repeat_export_is_idempotent_and_preserves_markdown(self) -> None:
        active, candidate = self._write_active_and_candidate()
        first = learning.export_learning_records(self.root)
        records_path = learning.learning_records_file(self.root)
        records_before = records_path.read_bytes()
        active_before = active.read_bytes()
        candidate_before = candidate.read_bytes()

        second = learning.export_learning_records(self.root)

        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(second["added"], 0)
        self.assertEqual(records_path.read_bytes(), records_before)
        self.assertEqual(active.read_bytes(), active_before)
        self.assertEqual(candidate.read_bytes(), candidate_before)

    def test_export_keeps_an_existing_id_when_an_unrelated_rule_is_prepended(self) -> None:
        active, _candidate = self._write_active_and_candidate()
        learning.export_learning_records(self.root)
        first_id = next(
            record["id"]
            for record in learning.load_learning_records(self.root)
            if record["text"].startswith("Active rule\n")
        )
        active.write_text(
            "# Active\n\n"
            + learning.render_pattern(learning._normalize_pattern(_pattern("Unrelated rule")))
            + active.read_text(encoding="utf-8").split("\n\n", maxsplit=1)[1],
            encoding="utf-8",
        )

        learning.export_learning_records(self.root)
        second_id = next(
            record["id"]
            for record in learning.load_learning_records(self.root)
            if record["text"].startswith("Active rule\n")
        )

        self.assertEqual(second_id, first_id)

    def test_malformed_sidecar_refuses_export_without_erasing_markdown(self) -> None:
        active, candidate = self._write_active_and_candidate()
        active_before = active.read_bytes()
        candidate_before = candidate.read_bytes()
        records_path = learning.learning_records_file(self.root)
        records_path.write_text('{"schema":"wrong","records":[]}', encoding="utf-8")

        with self.assertRaises(learning.LearningRecordError):
            learning.export_learning_records(self.root)

        self.assertEqual(active.read_bytes(), active_before)
        self.assertEqual(candidate.read_bytes(), candidate_before)
        self.assertEqual(
            records_path.read_text(encoding="utf-8"), '{"schema":"wrong","records":[]}'
        )

    def test_export_backup_rollback_restores_existing_records_without_touching_markdown(
        self,
    ) -> None:
        active, candidate = self._write_active_and_candidate()
        learning.export_learning_records(self.root)
        initial_records = learning.learning_records_file(self.root).read_bytes()
        candidate.write_text(
            candidate.read_text(encoding="utf-8")
            + "\n"
            + learning.render_pattern(learning._normalize_pattern(_pattern("Later candidate"))),
            encoding="utf-8",
        )
        active_before = active.read_bytes()
        candidate_before = candidate.read_bytes()

        exported = learning.export_learning_records(self.root)
        rollback = learning.rollback_learning_records_export(self.root)

        self.assertTrue(exported["changed"])
        self.assertIn("backup", exported)
        self.assertTrue(rollback["ok"])
        self.assertEqual(learning.learning_records_file(self.root).read_bytes(), initial_records)
        self.assertEqual(active.read_bytes(), active_before)
        self.assertEqual(candidate.read_bytes(), candidate_before)
        self.assertEqual(len(learning.load_learning_records(self.root)), 2)

    def test_export_and_rollback_take_the_candidate_lifecycle_lock(self) -> None:
        self._write_active_and_candidate()
        lock_calls: list[str | None] = []
        real_lock = learning._candidate_lock

        @contextmanager
        def tracked_lock(root: Path | None, namespace: str | None):
            lock_calls.append(namespace)
            with real_lock(root, namespace):
                yield

        with patch.object(learning, "_candidate_lock", tracked_lock):
            learning.export_learning_records(self.root)
            learning.rollback_learning_records_export(self.root)

        self.assertEqual(lock_calls, [None, None])


if __name__ == "__main__":
    unittest.main()
