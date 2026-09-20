#!/usr/bin/env python3
"""Consolidation: the step that makes staged learnings actually reach reviews.

Reviews load ``learned-patterns.md`` only, so a candidate that is never merged has
no effect at all. Before this endpoint existed the app described consolidation it
could not perform, and staged learnings sat inert indefinitely.

The merge is a judgment call and runs as one worker turn; the APPLY is
deterministic. These tests pin that split — a chatty, truncated, or failed turn
must leave the ruleset exactly as it was.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from backend import routes
from sage_lib import learning, store


def _pattern(title: str) -> dict:
    return {"title": title, "scope": "common", "impact": "high",
            "guidance": f"Guidance for {title}."}


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        store.ensure_layout()
        routes._CONSOLIDATING.clear()
        routes._CONSOLIDATE_STATE.clear()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)


class _FakeRequest:
    """Minimal stand-in: the handler reads only the JSON body."""

    def __init__(self, body: dict | None = None, bad: bool = False):
        self._body = body or {}
        self._bad = bad

    async def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


def _text(resp: web.Response) -> str:
    return resp.text or ""


class TestConsolidateEndpoint(_Base):
    async def test_refuses_when_nothing_is_staged(self):
        resp = await routes._handle_consolidate(
            _FakeRequest({"namespace": "default"}))  # type: ignore[arg-type]
        self.assertEqual(resp.status, 409)
        self.assertIn("nothing to consolidate", _text(resp))

    async def test_refuses_an_invalid_namespace(self):
        # The namespace names a directory, so a traversal attempt must not reach
        # the filesystem helpers.
        resp = await routes._handle_consolidate(
            _FakeRequest({"namespace": "../../etc"}))  # type: ignore[arg-type]
        self.assertEqual(resp.status, 400)

    async def test_refuses_a_second_concurrent_merge(self):
        learning.stage_learning(_pattern("A"), "fix_introduce")
        routes._CONSOLIDATING.add("default")
        resp = await routes._handle_consolidate(
            _FakeRequest({"namespace": "default"}))  # type: ignore[arg-type]
        # Two merges could interleave writes and lose patterns.
        self.assertEqual(resp.status, 409)
        self.assertIn("already running", _text(resp))

    async def test_two_concurrent_requests_start_only_one_merge(self):
        """The guard has to claim the namespace BEFORE its first await.

        Checking membership and then awaiting the staged count left a window
        where both requests passed the guard, then both dispatched a merge
        against the same scratch path — the second overwriting or unlinking the
        first's output.
        """
        learning.stage_learning(_pattern("Pending"), "fix_introduce")
        started: list = []

        def capture_task(coro):
            """Record the dispatch and close the coroutine so it never runs.

            Returns a stand-in that supports ``add_done_callback``, because the
            dispatch keeps a strong ref to the task and registers a discard
            callback -- a real ``create_task`` never returns None.
            """
            started.append(coro)
            coro.close()
            return unittest.mock.Mock()

        with patch.object(routes.asyncio, "create_task",
                          side_effect=capture_task):
            first, second = await asyncio.gather(
                routes._handle_consolidate(_FakeRequest({})),
                routes._handle_consolidate(_FakeRequest({})),
            )

        codes = sorted([first.status, second.status])
        self.assertEqual(codes, [200, 409])
        self.assertEqual(len(started), 1)

    async def test_starts_a_merge_when_candidates_exist(self):
        learning.stage_learning(_pattern("A"), "fix_introduce")
        with patch.object(routes.asyncio, "create_task") as spawn:
            resp = await routes._handle_consolidate(
                _FakeRequest({"namespace": "default"}))  # type: ignore[arg-type]
            self.assertEqual(resp.status, 200)
            spawn.assert_called_once()
        self.assertIn("default", routes._CONSOLIDATING)

    async def test_a_missing_body_defaults_to_the_default_namespace(self):
        learning.stage_learning(_pattern("A"), "fix_introduce")
        with patch.object(routes.asyncio, "create_task"):
            resp = await routes._handle_consolidate(
                _FakeRequest(bad=True))  # type: ignore[arg-type]
        self.assertEqual(resp.status, 200)


class TestConsolidateMerge(_Base):
    """The background half: what the worker writes, and what is done with it."""

    def _pool(self, writes: str | None, ok: bool = True, error: str = ""):
        """Fake the pool so the worker writes a proposal, never live state."""
        ns_dir = learning._namespace_dir("default")
        out = Path(ns_dir) / "consolidation-proposal.json"

        def dispatch(task, timeout=0, on_activity=None):
            if writes is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps({
                    "ruleset_markdown": writes,
                    "decisions": [
                        {"candidate_id": item["id"], "action": "merge",
                         "reason_code": "candidate_merged"}
                        for item in learning.list_candidate()
                    ],
                }), encoding="utf-8")
            return {"ok": ok, "output": "done", "error": error}

        pool = AsyncMock()
        pool.begin_batch = AsyncMock()
        pool.end_batch = AsyncMock()
        return dispatch, pool, out

    async def _run(self, writes: str | None, ok: bool = True, error: str = ""):
        dispatch, pool, out = self._pool(writes, ok, error)
        candidates = learning.list_candidate()
        candidate_ids = sorted({item["id"] for item in candidates})
        snapshot = [learning._candidate_snapshot_entry(item) for item in candidates]
        with patch.object(routes.review_pool, "get_pool", return_value=pool), \
                patch.object(routes.review_pool, "make_sync_dispatch",
                             return_value=dispatch):
            await routes._consolidate_bg("default", candidate_ids, snapshot)
        preview_id = routes._CONSOLIDATE_STATE["default"].get("preview_id")
        if preview_id:
            applied = learning.apply_consolidation_preview(preview_id, confirmed=True)
            self.assertTrue(applied["ok"])
        return out

    async def test_applies_the_merge_the_worker_wrote(self):
        learning.stage_learning(_pattern("Old lesson"), "fix_introduce")
        merged = (
            "# Common learned patterns\n\n"
            "### Sharpened lesson <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-07-29T00:00:00Z -->\nGuidance that survived.\n"
        )
        await self._run(merged)
        titles = [p["title"] for p in learning.list_patterns_for_review()]
        self.assertEqual(titles, ["Sharpened lesson"])
        # The candidate is consumed, so the next merge does not redo this one.
        self.assertEqual(learning.candidate_count(), 0)
        self.assertFalse(routes._CONSOLIDATE_STATE["default"]["running"])
        self.assertIsNone(routes._CONSOLIDATE_STATE["default"]["error"])

    async def test_a_worker_that_wrote_nothing_leaves_the_ruleset_alone(self):
        # The reviewer's memory must survive a failed merge: this is the case that
        # would otherwise wipe every learned pattern.
        learning.consolidate_apply(
            "# Common\n\n### Kept <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nStill here.\n")
        learning.stage_learning(_pattern("Pending"), "fix_introduce")
        await self._run(None, ok=False, error="turn failed")
        self.assertEqual([p["title"] for p in learning.list_patterns_for_review()],
                         ["Kept"])
        # And the candidate is NOT cleared, so nothing staged is lost either.
        self.assertEqual(learning.candidate_count(), 1)
        self.assertEqual(
            routes._CONSOLIDATE_STATE["default"]["error"],
            "the consolidation worker could not complete; try again",
        )

    async def test_a_failed_worker_that_wrote_a_partial_ruleset_is_refused(self):
        # The dangerous shape: the turn FAILED, but it had already emitted one
        # valid pattern. Non-empty and parseable, so only spawn["ok"] can catch it.
        # Applying it would replace the full ruleset with this fragment and clear
        # the candidates, losing the omitted rules from both copies.
        learning.consolidate_apply(
            "# Common\n\n"
            "### Kept one <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nStill here.\n"
            "### Kept two <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nAlso still here.\n")
        learning.stage_learning(_pattern("Pending"), "fix_introduce")

        partial = (
            "# Common learned patterns\n\n"
            "### Kept one <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nStill here.\n"
        )
        await self._run(partial, ok=False, error="timed out mid-write")

        # Both rules survive, not just the one the partial file happened to carry.
        self.assertEqual(
            sorted(p["title"] for p in learning.list_patterns_for_review()),
            ["Kept one", "Kept two"])
        # And the staged candidate is still staged, so nothing pending is lost.
        self.assertEqual(learning.candidate_count(), 1)
        self.assertEqual(
            routes._CONSOLIDATE_STATE["default"]["error"],
            "the consolidation worker could not complete; try again",
        )

    async def test_a_successful_worker_still_applies_its_merge(self):
        # The guard must not refuse legitimate merges: ok=True still applies.
        learning.stage_learning(_pattern("Pending"), "fix_introduce")
        merged = (
            "# Common learned patterns\n\n"
            "### Fresh rule <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-07-29T00:00:00Z -->\nApplied.\n"
        )
        await self._run(merged, ok=True)
        self.assertEqual([p["title"] for p in learning.list_patterns_for_review()],
                         ["Fresh rule"])
        self.assertIsNone(routes._CONSOLIDATE_STATE["default"]["error"])

    async def test_a_learning_staged_during_the_merge_survives(self):
        # Staged before the merge: this one IS represented in the merged ruleset.
        learning.stage_learning(_pattern("Known at dispatch"), "fix_introduce")

        ns_dir = learning._namespace_dir("default")
        out = Path(ns_dir) / "consolidation-proposal.json"
        merged = (
            "# Common learned patterns\n\n"
            "### Known at dispatch <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-07-29T00:00:00Z -->\nFolded in.\n"
        )

        def dispatch(task, timeout=0, on_activity=None):
            # A concurrent review stages a NEW learning while the merge runs. The
            # worker has already read the candidate file, so this cannot appear in
            # `merged` — and must therefore not be cleared by this merge.
            learning.stage_learning(_pattern("Staged mid-merge"), "fix_introduce")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({
                "ruleset_markdown": merged,
                "decisions": [{"candidate_id": learning.list_candidate()[0]["id"],
                               "action": "merge", "reason_code": "candidate_merged"}],
            }), encoding="utf-8")
            return {"ok": True, "output": "done", "error": ""}

        pool = AsyncMock()
        pool.begin_batch = AsyncMock()
        pool.end_batch = AsyncMock()
        with patch.object(routes.review_pool, "get_pool", return_value=pool), \
                patch.object(routes.review_pool, "make_sync_dispatch",
                             return_value=dispatch):
            before = learning.list_candidate()
            await routes._consolidate_bg(
                "default",
                [before[0]["id"]],
                [learning._candidate_snapshot_entry(item) for item in before],
            )

        preview_id = routes._CONSOLIDATE_STATE["default"]["preview_id"]
        self.assertTrue(learning.apply_consolidation_preview(preview_id, confirmed=True)["ok"])

        # The merge landed.
        self.assertEqual([p["title"] for p in learning.list_patterns_for_review()],
                         ["Known at dispatch"])
        # And the concurrently-staged learning is STILL staged, not silently gone.
        self.assertEqual([p["title"] for p in learning.list_candidate()],
                         ["Staged mid-merge"])
        self.assertIsNone(routes._CONSOLIDATE_STATE["default"]["error"])

    async def test_consolidating_everything_still_empties_the_candidate(self):
        # The selective clear must not leave consumed candidates behind forever.
        learning.stage_learning(_pattern("Only one"), "fix_introduce")
        merged = (
            "# Common learned patterns\n\n"
            "### Only one <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-07-29T00:00:00Z -->\nFolded in.\n"
        )
        await self._run(merged, ok=True)
        self.assertEqual(learning.candidate_count(), 0)

    async def test_an_empty_merge_file_is_refused(self):
        learning.stage_learning(_pattern("Pending"), "fix_introduce")
        await self._run("   \n")
        self.assertEqual(learning.candidate_count(), 1)
        self.assertTrue(routes._CONSOLIDATE_STATE["default"]["error"])

    async def test_a_symlinked_merge_file_is_refused(self):
        """The worker writes the merge file, so it can plant a symlink there.

        Following it would copy an arbitrary file into learned-patterns.md, which
        is rendered in the UI and injected into every later review prompt. The
        read goes through the hooks chokepoint, which opens O_NOFOLLOW.
        """
        secret = Path(self.tmp) / "outside-secret.txt"
        secret.write_text("### Stolen <!-- scope:common --> <!-- impact:high -->"
                          " <!-- added:2026-01-01T00:00:00Z -->\nleaked.\n",
                          encoding="utf-8")
        learning.consolidate_apply(
            "# Common\n\n### Kept <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nStill here.\n")
        learning.stage_learning(_pattern("Pending"), "fix_introduce")

        ns_dir = learning._namespace_dir("default")
        out = Path(ns_dir) / "learned-patterns.merge.md"

        def dispatch(task, timeout=0, on_activity=None):
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.exists() or out.is_symlink():
                out.unlink()
            out.symlink_to(secret)
            return {"ok": True, "output": "done", "error": ""}

        pool = AsyncMock()
        pool.begin_batch = AsyncMock()
        pool.end_batch = AsyncMock()
        with patch.object(routes.review_pool, "get_pool", return_value=pool), \
                patch.object(routes.review_pool, "make_sync_dispatch",
                             return_value=dispatch):
            await routes._consolidate_bg("default")

        # The ruleset is untouched and nothing from outside leaked into it.
        titles = [p["title"] for p in learning.list_patterns_for_review()]
        self.assertEqual(titles, ["Kept"])
        self.assertNotIn("Stolen", learning.common_file().read_text(encoding="utf-8"))
        self.assertEqual(learning.candidate_count(), 1)

    async def test_stale_merge_residue_is_not_applied(self):
        """A crash between the worker's write and the apply leaves the scratch file.

        The next consolidation whose worker produces nothing would otherwise read
        that stale output, apply it over the live ruleset, and clear a candidate
        that was never actually merged.
        """
        learning.consolidate_apply(
            "# Common\n\n### Kept <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nStill here.\n")
        learning.stage_learning(_pattern("Pending"), "fix_introduce")

        ns_dir = learning._namespace_dir("default")
        stale = Path(ns_dir) / "learned-patterns.merge.md"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(
            "### Stale <!-- scope:common --> <!-- impact:high -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nfrom a crashed run.\n",
            encoding="utf-8")

        def writes_nothing(task, timeout=0, on_activity=None):
            return {"ok": True, "output": "done", "error": ""}

        pool = AsyncMock()
        pool.begin_batch = AsyncMock()
        pool.end_batch = AsyncMock()
        with patch.object(routes.review_pool, "get_pool", return_value=pool), \
                patch.object(routes.review_pool, "make_sync_dispatch",
                             return_value=writes_nothing):
            await routes._consolidate_bg("default")

        titles = [p["title"] for p in learning.list_patterns_for_review()]
        self.assertEqual(titles, ["Kept"])
        self.assertNotIn("Stale", learning.common_file().read_text(encoding="utf-8"))
        # The candidate is still staged, because nothing was merged.
        self.assertEqual(learning.candidate_count(), 1)

    async def test_the_scratch_file_is_removed(self):
        learning.stage_learning(_pattern("A"), "fix_introduce")
        out = await self._run(
            "# C\n\n### T <!-- scope:common --> <!-- impact:low -->"
            " <!-- added:2026-01-01T00:00:00Z -->\nG.\n")
        # Left behind, the next run would read a stale merge as this run's output.
        self.assertFalse(out.exists())

    async def test_the_claim_is_released_even_when_the_merge_fails(self):
        learning.stage_learning(_pattern("A"), "fix_introduce")
        routes._CONSOLIDATING.add("default")
        with patch.object(routes.review_pool, "get_pool",
                          side_effect=RuntimeError("no pool")):
            await routes._consolidate_bg("default")
        # Otherwise the namespace could never be consolidated again this process.
        self.assertNotIn("default", routes._CONSOLIDATING)
        self.assertIn("no pool", routes._CONSOLIDATE_STATE["default"]["error"])


class TestConsolidationPrompt(unittest.TestCase):
    def test_the_prompt_names_both_inputs_and_the_output(self):
        from sage_lib import review_driver as D
        task = D.build_consolidation_task(
            "default", "/live.md", "/cand.md", "/out.md")
        for token in ("/live.md", "/cand.md", "/out.md", "default"):
            self.assertIn(token, task)

    def test_the_prompt_forbids_dropping_rules_casually(self):
        from sage_lib import review_driver as D
        task = D.build_consolidation_task("ns", "a", "b", "c")
        # The ruleset IS the reviewer's memory; a merge that silently drops rules
        # loses lessons permanently.
        self.assertIn("Keep every current pattern", task)
        self.assertIn("code-agnostic", task)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestConcurrentStagingKeepsEveryLearning(unittest.TestCase):
    """Two reviews staging at once must not overwrite each other.

    ``stage_learning`` reads the candidate file, appends one pattern, and rewrites
    the whole file. The write is atomic; the read-modify-write is not. The lock
    around it is what makes concurrent staging additive.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "app"
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pat(self, title):
        return {"title": title, "scope": "common", "impact": "high",
                "dimension": "correctness",
                "guidance": f"Guidance for {title}."}

    def test_concurrent_stagers_do_not_drop_a_learning(self):
        # Widen the read->write window so the race is deterministic, not luck.
        real_write = learning._atomic_write

        def slow_write(path, body):
            time.sleep(0.02)
            return real_write(path, body)

        titles = [f"Lesson {i}" for i in range(8)]
        errors: list[BaseException] = []

        def stage(t):
            try:
                learning.stage_learning(self._pat(t), "fix_introduce", self.root)
            except BaseException as e:       # pragma: no cover - surfaced below
                errors.append(e)

        with unittest.mock.patch.object(learning, "_atomic_write", slow_write):
            threads = [threading.Thread(target=stage, args=(t,)) for t in titles]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

        self.assertEqual(errors, [])
        staged = sorted(p["title"] for p in learning.list_candidate(self.root))
        self.assertEqual(staged, sorted(titles),
                         "a concurrently staged learning was overwritten")


class TestCandidateDeleteEndpoint(_Base):
    """Deleting staged candidates: one, a bulk selection, and while a merge runs."""

    async def test_deletes_a_single_candidate(self):
        learning.stage_learning(_pattern("Keep me"), "fix_introduce")
        learning.stage_learning(_pattern("Drop me"), "fix_introduce")
        victim = [c for c in learning.list_candidate() if c["title"] == "Drop me"][0]
        resp = await routes._handle_candidates_delete(
            _FakeRequest({"namespace": "default", "candidate_ids": [victim["id"]]})
        )  # type: ignore[arg-type]
        self.assertEqual(resp.status, 200)
        body = json.loads(_text(resp))
        self.assertEqual((body["requested"], body["removed"], body["remaining"]), (1, 1, 1))
        self.assertEqual([c["title"] for c in learning.list_candidate()], ["Keep me"])

    async def test_deletes_a_bulk_selection_in_one_call(self):
        for index in range(4):
            learning.stage_learning(_pattern(f"Lesson {index}"), "fix_introduce")
        doomed = [c["id"] for c in learning.list_candidate()][:3]
        resp = await routes._handle_candidates_delete(
            _FakeRequest({"namespace": "default", "candidate_ids": doomed})
        )  # type: ignore[arg-type]
        self.assertEqual(json.loads(_text(resp))["removed"], 3)
        self.assertEqual(learning.candidate_count(), 1)

    async def test_deleting_a_candidate_leaves_the_ruleset_alone(self):
        learning.stage_learning(_pattern("Only staged"), "fix_introduce")
        before = learning.load_learning_records(namespace="default")
        victim = learning.list_candidate()[0]
        await routes._handle_candidates_delete(
            _FakeRequest({"namespace": "default", "candidate_ids": [victim["id"]]})
        )  # type: ignore[arg-type]
        # Discarding a staged learning is not a review decision: no rule is
        # unlearned and no governed record is rewritten.
        self.assertEqual(learning.list_patterns_for_review(), [])
        self.assertEqual(learning.load_learning_records(namespace="default"), before)

    async def test_refuses_ids_that_are_not_staged(self):
        learning.stage_learning(_pattern("Staged"), "fix_introduce")
        resp = await routes._handle_candidates_delete(
            _FakeRequest({"namespace": "default", "candidate_ids": ["nope"]})
        )  # type: ignore[arg-type]
        self.assertEqual(resp.status, 400)
        self.assertEqual(learning.candidate_count(), 1)

    async def test_refuses_an_empty_selection(self):
        resp = await routes._handle_candidates_delete(
            _FakeRequest({"namespace": "default", "candidate_ids": []})
        )  # type: ignore[arg-type]
        self.assertEqual(resp.status, 400)

    async def test_refuses_while_a_merge_is_running(self):
        learning.stage_learning(_pattern("Staged"), "fix_introduce")
        victim = learning.list_candidate()[0]
        routes._CONSOLIDATING.add("default")
        resp = await routes._handle_candidates_delete(
            _FakeRequest({"namespace": "default", "candidate_ids": [victim["id"]]})
        )  # type: ignore[arg-type]
        # The running merge snapped this catalog; removing an entry it saw would
        # make the apply land against a listing the worker never read.
        self.assertEqual(resp.status, 409)
        self.assertEqual(learning.candidate_count(), 1)


def _governed_candidate(count: int = 1, namespace: str = "default"):
    """One staged candidate plus the sidecar record that governs it.

    Staging alone never creates a record: a namespace becomes governed only once
    its sidecar already holds records, so the record is written here explicitly —
    with the rule text the candidate renders, which is how the preview matches the
    two.
    """
    learning.stage_learning(_pattern("Governed lesson"), "fix_introduce", namespace=namespace)
    candidates = learning.list_candidate(namespace=namespace)
    evidence = [
        {
            "review": f"review-{index}",
            "change": f"change-{index}",
            "observed_at": f"2026-01-0{index + 1}T00:00:00Z",
        }
        for index in range(count)
    ]
    record = {
        "id": "governed-record",
        "text": "Governed lesson",
        "rule": candidates[0]["guidance"],
        "namespace": namespace,
        "scope": "common",
        "lifecycle": "candidate",
        "origin": {"source": "fix_introduce", "reference": "review-1"},
        "repository_identity": None,
        "timestamps": {
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": None,
            "archived_at": None,
        },
        "recurrence": {"count": count, "evidence": evidence},
        "legacy": False,
    }
    learning._write_learning_records(
        {
            "schema": learning.LEARNING_RECORDS_SCHEMA,
            "version": learning.LEARNING_RECORDS_VERSION,
            "records": [record],
        },
        None,
        namespace,
    )
    snapshot = [learning._candidate_snapshot_entry(item) for item in candidates]
    return candidates, snapshot


class TestPromotionEligibility(_Base):
    """The recurrence floor that refuses a first-occurrence promotion."""

    def test_a_first_occurrence_candidate_is_ineligible(self):
        candidates, snapshot = _governed_candidate()
        blocked = learning.promotion_ineligible_candidate_ids(
            snapshot, [c["id"] for c in candidates], namespace="default"
        )
        # The governed record sits at recurrence 1, so promoting it would let a
        # single incident govern every later review.
        self.assertEqual(blocked, [candidates[0]["id"]])

    def test_a_recurring_candidate_clears_the_gate(self):
        candidates, snapshot = _governed_candidate(count=2)
        self.assertEqual(
            learning.promotion_ineligible_candidate_ids(
                snapshot, [c["id"] for c in candidates], namespace="default"
            ),
            [],
        )

    def test_a_candidate_without_a_governed_record_is_not_gated(self):
        # A markdown-only namespace has no sidecar, so nothing can be refused.
        learning.stage_learning(_pattern("Legacy only"), "fix_introduce", namespace="legacy-ns")
        candidates = learning.list_candidate(namespace="legacy-ns")
        self.assertEqual(len(candidates), 1)
        snapshot = [learning._candidate_snapshot_entry(item) for item in candidates]
        self.assertEqual(
            learning.promotion_ineligible_candidate_ids(
                snapshot, [c["id"] for c in candidates], namespace="legacy-ns"
            ),
            [],
        )

    async def test_the_merge_task_is_given_every_id_and_the_blocked_ones(self):
        candidates, snapshot = _governed_candidate()
        ids = [c["id"] for c in candidates]

        def dispatch(task, timeout=0, on_activity=None):
            return {"ok": True, "output": "done", "error": ""}

        pool = AsyncMock()
        pool.begin_batch = AsyncMock()
        pool.end_batch = AsyncMock()
        seen: dict = {}
        real = routes.review_driver.build_consolidation_task

        def capture(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        with patch.object(routes.review_pool, "get_pool", return_value=pool), \
                patch.object(routes.review_pool, "make_sync_dispatch",
                             return_value=dispatch), \
                patch.object(routes.review_driver, "build_consolidation_task",
                             side_effect=capture):
            await routes._consolidate_bg("default", ids, snapshot)

        self.assertEqual([item["id"] for item in seen["selection"]], ids)
        self.assertEqual(seen["blocked_promotions"], ids)
        prompt = real("default", "a", "b", "c", selection=seen["selection"],
                      blocked_promotions=seen["blocked_promotions"])
        # A legacy block carries no id on disk, so the prompt is the only place the
        # worker can learn the ids its decisions must name.
        for candidate_id in ids:
            self.assertIn(candidate_id, prompt)
        self.assertIn("cannot be promoted or merged", prompt)
        self.assertIn("retain", prompt)


class TestConsolidationFailureIsNamed(_Base):
    """A refused proposal reports why, instead of one sentence for every cause."""

    async def _state(self, error: str):
        routes._CONSOLIDATE_STATE.clear()
        routes._CONSOLIDATE_STATE["default"] = {
            "running": False,
            "code": "malformed_worker_output",
            "error_code": error,
            "error": "malformed_worker_output",
        }
        try:
            with patch.object(routes.learning, "list_rule_entries", return_value=[]), \
                    patch.object(routes.learning, "list_candidate", return_value=[]):
                resp = await routes._handle_learnings(_LearningsRequest("default"))
        finally:
            routes._CONSOLIDATE_STATE.clear()
        return json.loads(_text(resp))

    async def test_the_view_reports_the_machine_readable_cause(self):
        body = await self._state("candidate_promotion_not_eligible")
        self.assertEqual(body["consolidate_error_code"], "candidate_promotion_not_eligible")

    async def test_the_rejected_proposal_is_logged_with_its_reason(self):
        candidates, snapshot = _governed_candidate()
        ids = [c["id"] for c in candidates]

        def dispatch(task, timeout=0, on_activity=None):
            out = Path(learning._namespace_dir("default")) / "consolidation-proposal.json"
            out.write_text(json.dumps({
                "ruleset_markdown": "### Promoted <!-- scope:common -->"
                                    " <!-- impact:high --> <!-- added:2026-09-18T00:00:00Z -->\nG.\n",
                "decisions": [
                    {"candidate_id": item["id"], "action": "promote",
                     "reason_code": "candidate_merged"}
                    for item in candidates
                ],
            }), encoding="utf-8")
            return {"ok": True, "output": "done", "error": ""}

        pool = AsyncMock()
        pool.begin_batch = AsyncMock()
        pool.end_batch = AsyncMock()
        with patch.object(routes.review_pool, "get_pool", return_value=pool), \
                patch.object(routes.review_pool, "make_sync_dispatch",
                             return_value=dispatch), \
                self.assertLogs("kirocrew.app.code-review-sage", level="WARNING") as logs:
            await routes._consolidate_bg("default", ids, snapshot)
        self.assertTrue(
            any("consolidation proposal rejected" in line for line in logs.output),
            logs.output,
        )
        self.assertEqual(
            routes._CONSOLIDATE_STATE["default"]["error_code"],
            "candidate_promotion_not_eligible",
        )


class _LearningsRequest:
    """The learnings view reads only the namespace query parameter."""

    def __init__(self, namespace: str):
        self.query = {"namespace": namespace}
