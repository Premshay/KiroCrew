"""Commit a kept winner on the working branch: stage, commit, amend, or roll back.

Both tracks apply, then draft, then commit (the spec's keep/draft ordering invariant): the
winner's diff is applied and committed with a placeholder message, so the draft push carries
the fix; once the pipeline has reproduced it the commit is amended with the attributable,
redacted message, and when nothing was filed the branch is reset to where the winner found
it. A rollback that fails latches ``_rollback_failed``, stops the run and quarantines the
clone, so nothing is published on top of a commit that was refused.

Each function here whose first parameter is ``self`` is a ``Driver`` method: the class binds
it by name, so it reads and writes the run's state on the instance, and a patch of the method
on the class or on an instance reaches every caller.

Instance fields written: ``_repository_retired``, ``_rollback_failed``, ``pushed_sha``.

Instance fields read: ``_publication_failure``, ``archive``, ``branch``, ``clone``,
``guardrail_tolerances``, ``ledger``, ``log``, ``measurer``, ``pr_pipeline``, ``profile``,
``stats``.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from kiro_crew.platform.context import redact_log_via_context

from . import ledger as L
from . import pr_description as D
from .contracts import Proposal
from .driver_git import _full_head_id, _git
from .keeper import KEPT
from .push_policy import normalize_branch

if TYPE_CHECKING:
    from .contracts import BugGateResult
    from .driver import Driver


def _apply_verdict_owned(
    self, cycle, base_sha, verdict, archived, fresh_count, gated_sha, publication
) -> int:
    if self._publishing_is_halted():
        return 0
    prepared_sha = None
    if (
        verdict.keep
        and verdict.winner is not None
        and getattr(self.profile, "final_regression_required", False) is True
    ):
        prepared_sha = self._prepare_finalist(verdict.winner, self._commit_winner_provisional)
        if prepared_sha is None:
            self.stats.not_kept += 1
            return fresh_count
    # Archive ALL survivors (the whole population is evolutionary memory). The kept
    # winner's diff_ref is reused as the CR's ``diff-ref`` (06_*.md §3.1/§3.2).
    winner_diff_ref = ""
    for prop, status, meas in archived:
        diff_ref = self.archive.save_candidate(
            cand_id=prop.cand_id,
            diff=prop.diff,
            detail={"proposal": prop, "status": status, "measurement": meas},
        )
        if verdict.winner is not None and prop.cand_id == verdict.winner.cand_id:
            winner_diff_ref = diff_ref
        self.archive.append_row(
            {
                "cycle": cycle,
                "cand_id": prop.cand_id,
                "commit": "-",
                "status": status,
                "tests_pass": True,
                "reps": self.measurer.reps,
                "primary_delta": (meas.primary_delta if meas else ""),
                "noise_band": (meas.noise_band if meas else ""),
                "description": prop.description,
                "diff_ref": diff_ref,
                # The structured metric blob (primary_cand/base + stages + guardrails +
                # the NON-BLOCKING secondary metrics) so the data-store reader recovers
                # the per-candidate absolute value + secondary columns from the archive
                # (data_store.read_progress reads row["metric"]["primary_cand"]); the
                # ``note`` string stays available under "note" for the greppable TSV.
                "metric": (self._metric_blob(meas) if meas else ""),
                "note": (meas.note if meas else ""),
                # Flattened secondary metrics (rss/cpu/throughput) so they land as their
                # own greppable results.tsv columns beyond the control set (METRICS.md §6).
                "secondary": (dict(meas.secondary) if meas else {}),
            }
        )
        if status != KEPT:
            # Map the keeper's real discard reason to the correct ledger status — NOT a
            # blanket ``discarded_noise``. Only a delta inside the band is noise; a
            # guardrail/tests/RH failure is a verification failure (failed_verify) and a
            # measurement error is retryable (error). Hard-coding discarded_noise here
            # mislabeled the row AND permanently dedup-blocked a transient RH-probe miss.
            self._record(prop, L.map_perf_discard_to_status(status), status)

    if not verdict.keep or verdict.winner is None:
        self.stats.not_kept += 1
        self.log.info("cycle %d: no keep (%s)", cycle, verdict.reason)
        return fresh_count

    winner = verdict.winner
    self.stats.kept += 1
    self.log.info(
        "cycle %d: KEPT %s (%s) — running REPRODUCE", cycle, winner.cand_id, verdict.reason
    )

    # M5 PIPELINE: VERIFY (the keeper, above) → REPRODUCE (second independent A/B) →
    # DRAFT CR → ledger (06_*.md §1.3). The CR pipeline owns the reproduce + draft +
    # record boundary; the driver owns the commit-on-keep. Only a delta that survives
    # the SECOND independent A/B becomes a CR — a first-run fluke is recorded as
    # ``failed_verify`` and does NOT advance the branch (06_*.md §1.1).
    # COMMIT the winner into the shared clone BEFORE drafting. Staging alone is not
    # enough: ``git push HEAD:refs/heads/<b>`` sends the COMMIT that HEAD points at, and
    # `git apply` + `git add -A` only touch the index — verified against a local bare
    # repo, where a staged-but-uncommitted fix pushed the ORIGINAL file content. So the
    # fix has to be a commit, not just staged. Raised by review of this branch after a
    # first attempt that only staged.
    #
    # The commit message needs ``outcome.reproduce``, which only the pipeline produces,
    # so this lands a PLACEHOLDER message and `_finalize_winner_commit` amends it with
    # the real numbers once the pipeline returns — and resets the branch if nothing was
    # filed, so a fluke never advances HEAD (06_*.md §1.1).
    pre_sha = prepared_sha or _git(["rev-parse", "HEAD"], self.clone).stdout.strip()
    if prepared_sha is None and not self._commit_winner_provisional(winner):
        self.ledger.record(
            L.LedgerEntry(
                fp=L.fingerprint(
                    kind=winner.candidate.kind,
                    target=winner.candidate.target,
                    signature=winner.candidate.signature or "",
                ),
                kind=winner.candidate.kind,
                target=winner.candidate.target,
                status=L.STATUS_ERROR,
                note="winner diff did not apply to the working branch",
            )
        )
        return fresh_count

    outcome = self.pr_pipeline.emit_perf(
        profile=self.profile,
        winner=winner,
        verify=verdict.measurement,
        cycle=cycle,
        gated_commit_sha=gated_sha.get(winner.cand_id, ""),
        diff_ref=winner_diff_ref,
        base_anchor=f"{self.branch} @ {base_sha[:12]}",
    )
    if outcome.filed:
        publication["cr"] = outcome.cr
        if outcome.bookkeeping_error:
            raise RuntimeError(outcome.bookkeeping_error)
    if outcome.repository_retired:
        self.stats.kept -= 1
        return fresh_count
    if outcome.filed or outcome.committed_ready:
        # AMEND the provisional commit with the §2.4 attributable message, derived from
        # the SAME measured numbers as the CR (§3.2 end). The pipeline's INDEPENDENT
        # reproduce measurement (outcome.reproduce) is what makes the commit message and
        # the CR agree (06_*.md §3.1/§3.2; CrOutcome.reproduce) — it does not exist until
        # the pipeline has run, which is why the commit is amended rather than authored
        # here.
        committed = self._finalize_winner_commit(
            winner,
            verify=verdict.measurement,
            reproduce=outcome.reproduce,
            cycle=cycle,
            diff_ref=winner_diff_ref,
        )
        if outcome.committed_ready:
            # F10 direct-commit: push the verified commit to the authorized branch and
            # record ``committed`` with the real sha (only on a successful push — a
            # refused/failed push already recorded ``error`` and nothing left the sandbox).
            self.pushed_sha = ""
            pushed = self._direct_push(
                fp=outcome.fp, kind="perf", target=winner.candidate.target, sha=committed
            )
            if pushed is True:
                # `pushed_sha`, not `committed`: a rebase-and-retry inside the push
                # rewrites HEAD, and recording the pre-rebase sha would point the
                # ledger at a commit that is not in the remote's history.
                landed = self.pushed_sha or committed
                publication["landed_sha"] = landed
                self.ledger.record(
                    L.LedgerEntry(
                        fp=outcome.fp,
                        kind="perf",
                        target=winner.candidate.target,
                        status=L.STATUS_COMMITTED,
                        cr=landed,
                        note=f"direct-pushed to {self.branch} ({landed})"[:200],
                    )
                )
                self.stats.filed += 1
                self.log.info(
                    "cycle %d: COMMITTED %s → %s (%s)",
                    cycle,
                    winner.cand_id,
                    self.branch,
                    landed,
                )
            elif pushed is False:
                # ROLL BACK the refused commit. Leaving it at HEAD is a credential LEAK,
                # not just untidy bookkeeping: the direct-push scan range is
                # `HEAD~1..HEAD` (one commit), so the NEXT winner's scan does not see this
                # commit while its push publishes both. Measured on a real repo: candidate
                # A refused for a planted `AKIAIOSFODNN7EXAMPLE`, candidate B's scan range
                # showed the credential = False while its pushed range showed it = True.
                # Raised by the GPT review of this branch.
                self._reset_provisional(pre_sha)
                self.stats.kept -= 1  # push refused/failed → not a realized outcome
            else:
                # The clone was atomically retired; any Git rollback would trust
                # metadata that just failed validation.
                self.stats.kept -= 1
            return fresh_count
        self.stats.filed += 1
        self.log.info(
            "cycle %d: FILED %s cr=%s commit=%s",
            cycle,
            winner.cand_id,
            outcome.cr,
            committed,
        )
        # Announce the filed CR so the app can start a watcher session that keeps it
        # mergeable + drives it to passing-all-checks (tasks #21/#24). Opaque to the
        # spine — the backend's on_progress sink decides what to do with it.
        self._progress(
            cr_filed={
                "fp": outcome.fp,
                "cr": outcome.cr,
                "kind": "perf",
                "target": winner.candidate.target,
                "title": getattr(winner, "description", ""),
                "base_ref": getattr(self.profile.isolation, "base_ref", ""),
                "branch": getattr(winner, "branch", ""),
            }
        )
        # DELIBERATELY NOT reset here, unlike the bug track's filed path.
        #
        # Review asked for `_reset_provisional(pre_sha)` after this progress event, because
        # a filed perf winner stays on the local branch and a LATER cycle's PR therefore
        # carries it (measured: pushing whole HEAD for PR#2 included cycle 1's `FIX_1`).
        # The observation is correct, but the remedy would break the perf track's premise:
        # this loop is EVOLUTIONARY — "current best == HEAD" is its documented durable state
        # (see the module docstring), `base_sha = self.head_sha()` is re-read every cycle,
        # and every measurement is reported as "Δ vs current best". Resetting would make
        # each cycle re-measure against the ORIGINAL base, so a second improvement to the
        # same hot path could never be seen as an improvement at all.
        #
        # The bug track has no such property (independent loci, one PR each), which is why
        # resetting there was right and resetting here is not the same change.
        #
        # A per-winner branch rebuilt from the remote base would satisfy both goals in
        # principle. Measured: it is not a safe drop-in — two cycles improving the SAME
        # line produce a patch that does not apply to the untouched base, and the rebuild
        # silently yielded a branch containing NEITHER fix. Doing it properly needs a
        # cherry-pick with conflict handling and a decision about what to publish when the
        # replay fails, which is a design change rather than a bug fix.
        #
        # Recorded as a known limitation instead (see the module spec). It is also latent
        # rather than live: the perf track has never kept a measured win on a real
        # repository, so no perf PR has been filed for a second cycle to contaminate.
        # Raised by the GPT review of this branch.
    else:
        # Not reproduced / duplicate / draft error → do NOT advance the branch; the
        # ledger already carries the terminal outcome (the pipeline recorded it).
        # Roll the PROVISIONAL commit back: it exists only so the draft could push a
        # HEAD containing the fix, and a non-win must leave the branch where it was.
        self._reset_provisional(pre_sha)
        self.stats.kept -= 1  # the "keep" did not become a real, reproduced win
        self.log.info("cycle %d: %s NOT filed (%s)", cycle, winner.cand_id, outcome.status)
    return fresh_count


def _apply_bug_winner_owned(
    self, cycle: int, winner: Proposal, bug_res: BugGateResult, publication
) -> None:
    """Accept one bug fix that passed RED ∧ GREEN ∧ STAYGREEN: archive it,
    commit-on-keep locally, draft a DRAFT-only CR with the correctness narrative,
    and record ``filed`` in the shared ledger (05_*.md §4.2; 02_arch §3.2).

    This is the bug-track analogue of :meth:`_apply_verdict`'s keep path, but the
    CR trigger is the boolean RED/GREEN gate (the doubled-RED flake check is the
    reproduction analogue, §4.1) — there is no second A/B and no noise band."""
    if self._publishing_is_halted():
        return
    prepared_sha = None
    if getattr(self.profile, "final_regression_required", False) is True:
        prepared_sha = self._prepare_finalist(winner, self._commit_bug_winner_provisional)
        if prepared_sha is None:
            return
    diff_ref = self.archive.save_candidate(
        cand_id=winner.cand_id,
        diff=winner.diff,
        detail={"proposal": winner, "status": KEPT, "bug_gate": bug_res},
    )
    self.archive.append_row(
        {
            "cycle": cycle,
            "cand_id": winner.cand_id,
            "commit": "-",
            "status": KEPT,
            "tests_pass": True,
            "reps": 0,  # no A/B reps for a bug fix (deterministic boolean gate)
            "primary_delta": "",  # bug track has no measured delta (§6.1)
            "noise_band": "",
            "description": winner.description,
            "diff_ref": diff_ref,
            "metric": f"RED→GREEN→STAYGREEN ({bug_res.reason})",
        }
    )
    # M5 PIPELINE (bug track): the RED/GREEN gate already verified+reproduced (the
    # doubled-RED flake check IS the reproduce analogue, 06_*.md §1.1) — so emit the
    # draft CR with the RED→GREEN correctness narrative (§4.2) via the same pipeline
    # boundary. The pipeline dedups (defense-in-depth), authors the description, files
    # the draft, and records the terminal ledger row.
    # COMMIT first — same reason as the perf track: the recipe pushes this clone's
    # HEAD, and HEAD is a COMMIT pointer, so a merely-staged fix is invisible to the
    # draft. Provisional message; amended once the pipeline returns, reset if nothing
    # was filed.
    pre_sha = prepared_sha or _git(["rev-parse", "HEAD"], self.clone).stdout.strip()
    if prepared_sha is None and not self._commit_bug_winner_provisional(winner):
        self.ledger.record(
            L.LedgerEntry(
                fp=L.fingerprint(
                    kind=winner.candidate.kind,
                    target=winner.candidate.target,
                    signature=winner.candidate.signature or "",
                ),
                kind=winner.candidate.kind,
                target=winner.candidate.target,
                status=L.STATUS_ERROR,
                note="bug fix diff did not apply to the working branch",
            )
        )
        return

    outcome = self.pr_pipeline.emit_bug(
        profile=self.profile,
        winner=winner,
        bug_res=bug_res,
        cycle=cycle,
        diff_ref=diff_ref,
        # `pre_sha`, NOT `head_sha()`: the provisional fix commit above has already
        # advanced HEAD, so `head_sha()` here is the FIX commit. The base anchor is the
        # durable "tested against" provenance a reviewer reads, so recording the fix as its
        # own base is self-referential nonsense. `pre_sha` is the HEAD captured before the
        # commit — the revision the RED→GREEN gate actually ran against. The perf twin
        # already anchors on its own `base_sha` for the same reason. Raised by the GPT review.
        base_anchor=f"{self.branch} @ {pre_sha[:12]}",
    )
    if outcome.filed:
        publication["cr"] = outcome.cr
        if outcome.bookkeeping_error:
            raise RuntimeError(outcome.bookkeeping_error)
    if outcome.repository_retired:
        return
    if outcome.filed or outcome.committed_ready:
        committed = self._finalize_bug_winner_commit(
            winner, bug_res=bug_res, cycle=cycle, diff_ref=diff_ref
        )
        if outcome.committed_ready:
            # F10 direct-commit (bug track): push the verified RED→GREEN fix to the
            # authorized branch; record ``committed`` only on a successful push.
            self.pushed_sha = ""
            pushed = self._direct_push(
                fp=outcome.fp, kind="bug", target=winner.candidate.target, sha=committed
            )
            if pushed is True:
                # See the perf track: record the sha that LANDED, not the pre-rebase one.
                landed = self.pushed_sha or committed
                publication["landed_sha"] = landed
                self.ledger.record(
                    L.LedgerEntry(
                        fp=outcome.fp,
                        kind="bug",
                        target=winner.candidate.target,
                        status=L.STATUS_COMMITTED,
                        cr=landed,
                        note=f"direct-pushed bug fix to {self.branch} ({landed})"[:200],
                    )
                )
                self.stats.kept += 1
                self.stats.filed += 1
                self.log.info(
                    "cycle %d: BUG FIX COMMITTED %s → %s (%s)",
                    cycle,
                    winner.cand_id,
                    self.branch,
                    landed,
                )
            elif pushed is False:
                # Same rollback as the perf twin, and for the same reason: a refused
                # commit left at HEAD is invisible to the NEXT winner's `HEAD~1..HEAD`
                # scan but still published by its push. This branch had no `else` at
                # all — it fell straight through to `return` with the commit intact.
                # Roll back the provisional commit (as the perf twin does) so a refused
                # push leaves nothing at HEAD for the next winner's range to inherit. Do
                # NOT decrement `kept`: unlike the perf path, which increments `kept`
                # EAGERLY on keep (before the push) and so must reverse it on failure, the
                # bug path only increments `kept` inside the SUCCESS arm above (`+= 1` after
                # a landed push). Decrementing here subtracts from a counter this path never
                # added to, driving `stats.kept` negative or undercounted in the `/run`
                # result. Raised by the GPT review.
                self._reset_provisional(pre_sha)
            return  # _apply_bug_winner returns None (no fresh_count in this scope)
        self.stats.kept += 1
        self.stats.filed += 1
        self.log.info(
            "cycle %d: BUG FIX FILED %s cr=%s commit=%s",
            cycle,
            winner.cand_id,
            outcome.cr,
            committed,
        )
        # Announce the filed bug CR (tasks #21/#24) — the backend starts a watcher.
        self._progress(
            cr_filed={
                "fp": outcome.fp,
                "cr": outcome.cr,
                "kind": "bug",
                "target": winner.candidate.target,
                "title": getattr(winner.candidate, "signature", "")
                or getattr(winner, "description", ""),
                "base_ref": getattr(self.profile.isolation, "base_ref", ""),
                "branch": getattr(winner, "branch", ""),
            }
        )
        # Roll back HERE TOO, after a SUCCESSFUL file. A bug cycle can accept several
        # independent fixes and files one draft PR per locus, all from this ONE shared
        # clone — so leaving a filed winner's commit at HEAD makes the NEXT winner's
        # branch start from it, and its PR then carries the earlier, unrelated fix.
        # Measured on a real repo: winner B's `base...HEAD` range contained `FIX_A` as
        # well as `FIX_B`. The provisional commit exists ONLY so the draft push had a
        # HEAD containing this fix; that push has already happened and the work is safe
        # on its own generated branch, so HEAD must return to where this winner found it.
        #
        # Review suggested capping the cycle at ONE bug winner instead. That discards
        # verified, reproduced work for a bookkeeping problem — each fix is on a distinct
        # locus and has passed RED x2 -> GREEN -> STAYGREEN independently. Resetting keeps
        # every winner AND keeps each PR to its own change.
        # Raised by the GPT review of this branch.
        self._reset_provisional(pre_sha)
    else:
        # Roll the PROVISIONAL commit back — it exists only so the draft could push a
        # HEAD containing the fix, and a not-filed candidate must leave HEAD where it
        # was (06_*.md §1.1). flake8 caught this path being unwired.
        self._reset_provisional(pre_sha)
        self.log.info("cycle %d: bug fix %s NOT filed (%s)", cycle, winner.cand_id, outcome.status)


def _publishing_is_halted(self: Driver) -> bool:
    """Whether a failed provisional rollback has made this clone unsafe to publish from.

    A failed rollback leaves HEAD carrying a commit that was refused and never published.
    Anything that publishes afterwards sends that commit as an ANCESTOR while its own scan
    looks at one revision, so the refused content lands through a scanner that never saw it.
    The rollback IS the repair, so a rollback that cannot be trusted to have happened must
    not be followed by work that assumes it did -- and the answer is to stop, not to retry:
    a retry would be one more thing whose failure has to be handled.

    Checked at three places so the halt is UNCONDITIONAL rather than narrowed:

    * here, at the top of each winner-applying method, which is BEFORE the PR pipeline's
      `emit_*` -- that path reaches ``pr_recipe._push_fix_branch``, which pushes ``HEAD``
      and knows nothing about this latch, and it runs before the direct push does;
    * at :meth:`_direct_push`, the publish gate itself;
    * and through ``request_stop``, which the cycle loop reads, so no later cycle starts.

    One cycle can hold SEVERAL bug winners (``for prop, bug_res in bug_winners``), so a
    latch read only at cycle boundaries would let the next winner in the SAME cycle publish.
    Raised by the GPT review of this branch; the unconditional form was the conductor's
    requirement.
    """
    if getattr(self, "_publication_failure", None):
        return True
    if not getattr(self, "_rollback_failed", False):
        return False
    self.log.error(
        "refusing to apply any further winner: a provisional rollback failed, so HEAD"
        " carries a commit that was refused and never published"
    )
    return True


def _redact_commit_message(msg: str) -> str:
    """Strip credentials / exfiltration URLs from a commit message before it becomes
    PERMANENT git metadata. The message is built from agent-authored content (proposal
    signature/description), which is untrusted (CLAUDE.md) — and metadata-leak
    guidance is explicit that git commit messages "stay forever in
    the repository", so a leaked secret there is unwipeable. Applies to BOTH the CR-path
    local commit and the F10 direct-push commit. Best-effort: if the redaction helpers
    are unavailable, the message passes through (the commit still happens)."""
    try:
        # Kiro Crew's core redactor: one call, string return, both the
        # credential and exfiltration-URL passes. (The port originally
        # referenced a vendored module that does not exist here, so this
        # silently no-op'd on every commit — a real leak risk, now fixed.)
        from kiro_crew.security import redact

        msg = redact(msg)
    except Exception:  # noqa: BLE001 — a commit message is permanent, pushed git history
        # FAIL CLOSED (same as backend/commit.py): a message that cannot be scanned must
        # not be committed verbatim, since it becomes unwipeable once pushed. Fall back
        # to a fixed, prose-free subject. Raised by the GPT review of this branch.
        logging.getLogger("auto_improvement.driver").warning(
            "commit-message redaction failed; using a fixed subject"
        )
        return "auto-improvement: apply verified change"
    return msg


def _discard_staged(self: Driver, why: str) -> None:
    """Throw away the applied-but-not-committed diff so nothing inherits it.

    A provisional commit that FAILS (a rejecting hook, gpg/signing trouble) leaves the
    candidate's diff sitting in the index, and the next candidate's ``git commit`` —
    which stages with ``add -A`` — silently absorbs it. Measured on a real repo with a
    rejecting ``pre-commit`` hook: candidate B's commit contained candidate A's
    REJECTED, never-verified diff in ``m.py`` alongside B's own file. Publishing an
    unmeasured change is the one thing this pipeline must not do. Raised by the GPT
    review of this branch.

    ``reset --hard`` alone is not enough: files the patch CREATED were staged by
    ``add -A``, and a reset leaves them untracked on disk where the next ``add -A``
    picks them straight back up. So the added paths are collected first (from the
    index, before the reset) and removed individually — a targeted cleanup rather
    than a blanket ``git clean``, which would also delete unrelated build output.
    """
    added = _git(["diff", "--cached", "--name-only", "--diff-filter=A"], self.clone)
    paths = [p for p in (added.stdout or "").splitlines() if p.strip()]
    reset = _git(["reset", "--hard", "HEAD"], self.clone)
    if reset.returncode != 0:
        # Nothing else is safe to do here, but the operator must see it: the next
        # candidate may inherit this tree.
        self.log.error(
            "could not discard the staged diff after %s: %s",
            why,
            redact_log_via_context(reset.stderr or "")[:200],
        )
    for rel in paths:
        try:
            (self.clone / rel).unlink(missing_ok=True)
        except OSError as exc:  # a directory or a permission problem — log, continue
            self.log.warning("could not remove %s left by %s: %s", rel, why, exc)


def _stage_winner(self: Driver, winner: Proposal) -> bool:
    """Apply the winner's diff to the working branch and stage it. Returns False when
    the diff does not apply (the caller records an error and does not draft).

    SPLIT OUT of :meth:`_commit_winner` so the winner is in the shared clone's tree
    BEFORE the PR pipeline drafts. ``pr_recipe._push_fix_branch`` pushes the clone's
    ``HEAD``, so drafting first meant pushing a branch that did not contain the fix —
    or contained a PREVIOUS cycle's commit. Raised by review of this branch; traced
    through: the queue copy carries ``winner.diff`` (correct), ``gated_commit_sha``
    feeds the reproduce MEASUREMENT rather than the draft, and ``gate_res.commit_sha``
    is the throwaway WORKTREE's head — none of them put the fix in the shared clone.

    Committing earlier instead would have been wrong: the commit MESSAGE needs
    ``outcome.reproduce``, which only the pipeline produces, and reordering that way
    would silently degrade every kept-commit message to echoing VERIFY (06_*.md
    §3.1/§3.2). Apply-then-draft-then-commit keeps both properties.
    """
    _git(["checkout", normalize_branch(self.branch)], self.clone)
    if not winner.diff.strip():
        return True
    ap = subprocess.run(
        ["git", "-C", str(self.clone), "apply"],
        input=winner.diff,
        capture_output=True,
        # surrogateescape re-encodes the captured payload back to the exact
        # bytes git produced.
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    if ap.returncode != 0:
        self.log.error(
            "winner diff did not apply: %s", redact_log_via_context(ap.stderr or "")[:200]
        )
        return False
    _git(["add", "-A"], self.clone)
    return True


def _commit_winner_provisional(self: Driver, winner: Proposal) -> bool:
    """Apply the winner and commit it with a PLACEHOLDER message. False if it will not
    apply.

    A real commit, not just a staged index: ``git push HEAD:refs/heads/<b>`` sends the
    commit HEAD points at, so a staged-but-uncommitted fix is invisible to the draft.
    The final message needs the pipeline's reproduce measurement, so it is amended by
    :meth:`_finalize_winner_commit` once that exists, and rolled back by
    :meth:`_reset_provisional` when nothing is filed.
    """
    if not self._stage_winner(winner):
        return False
    if not winner.diff.strip():
        return True
    # CHECK the commit return code. `_git` does not raise, so a failed commit (a
    # rejecting hook, gpg failure, or an empty index) would otherwise leave HEAD on the
    # PREVIOUS commit while this returns True — the pipeline then drafts/pushes a commit
    # that does not contain the fix (or a prior cycle's). Fail closed instead. Raised by
    # the GPT review of this branch.
    commit = _git(
        # FIXED message — never `winner.cand_id`. `cand_id` embeds the model-chosen
        # `candidate.target`, and `_short` only restricts to alnum/`_`/`-`, which is
        # exactly the character class of an AWS key id or a `ghp_` token. Measured:
        # `src/m.py::AKIAIOSFODNN7EXAMPLE` produced
        # `c1_wide_m_py_AKIAIOSFODNN7EXAMPLE_d469bc5b`. This message is what the
        # DRAFT PUSH publishes — the redacted amend happens AFTER the push (871 ->
        # 887 -> 903) — so an unscanned cand_id lands in GitHub history, which cannot
        # be edited without rewriting it. The cand_id is still in the run archive and
        # the ledger, where it belongs. Raised by the GPT review of this branch.
        ["commit", "-q", "-m", "wip(auto-improvement): staging a verified candidate"],
        self.clone,
    )
    if commit.returncode != 0:
        self.log.error(
            "provisional commit failed for %s: %s",
            winner.cand_id,
            redact_log_via_context(commit.stderr or "")[:200],
        )
        self._discard_staged(f"a failed provisional commit for {winner.cand_id}")
        return False
    return True


def _reset_provisional(self: Driver, pre_sha: str) -> bool:
    """Roll the AUTHORIZED branch back to ``pre_sha`` after a provisional commit that was
    not filed, so a fluke, duplicate or error never advances HEAD (06_*.md section 1.1).

    Returns whether the branch is back where it belongs. A FAILED rollback is not a log
    line: HEAD keeps the refused commit, the next winner commits on top of it, and that
    winner's single-revision scan cannot see the parent its own push would publish. So a
    failure latches ``_rollback_failed`` and stops the run, at this chokepoint rather than
    at the five call sites, because a caller that forgets to check is the whole defect.

    ONE COMMAND, and that is the whole point. ``git reset --hard`` acts on WHATEVER IS
    CHECKED OUT, and this runs after the pre-push reviewer has had a shell in the clone --
    whose denylist covers only ``push`` and ``remote set-url``, so ``git checkout`` is
    permitted there. Two ways that bites:

    * the reviewer checks out a DIFFERENT branch, and the rollback hard-resets that branch
      to a sha from an unrelated run, destroying its commits;
    * HEAD is DETACHED, and the reset moves the detachment while the branch keeps carrying
      the provisional commit -- a rollback that silently does not roll back.

    An earlier fix read ``rev-parse --abbrev-ref HEAD``, checked the branch out if it
    differed, then reset -- which was the same check-then-use pair one level up: a
    backgrounded ``setsid git checkout victim`` landing between the check and the reset put
    the reset back on the wrong branch. ``git checkout -f -B <branch> <pre_sha>`` is a
    single invocation that NAMES the branch, moves it to *pre_sha* and checks it out, so
    there is no window and no ambiguity about which ref moves. ``-f`` keeps the old
    ``reset --hard`` semantics of discarding the working tree. Fail closed on error: a
    provisional commit left behind is resolved by the next cycle's stage step, while
    touching a ref that was never this run's to touch is not recoverable. Raised across
    three rounds of the GPT review of this branch.
    """
    if not pre_sha:
        return True
    if getattr(self, "_repository_retired", False):
        # The clone was already renamed out from under us; there is nothing to roll back
        # and no path to roll it back on.
        return False
    branch = normalize_branch(self.branch)
    res = _git(["checkout", "-f", "-B", branch, pre_sha], self.clone)
    if res.returncode != 0:
        self.log.error(
            "could not roll back the provisional commit to %s on %s: %s -- halting: HEAD"
            " carries a commit that was refused and never published",
            pre_sha[:10],
            branch,
            redact_log_via_context((res.stderr or "").strip())[:160],
        )
        self._rollback_failed = True
        self.request_stop()
        self._quarantine_unrolled_clone(pre_sha)
        return False
    return True


def _quarantine_unrolled_clone(self: Driver, pre_sha: str) -> None:
    """Rename the clone out of its canonical name after a rollback failure.

    THE GUARD HAS TO OUTLIVE THE PROCESS, because the thing it guards does. The in-memory
    latch stops this run, but the un-rolled-back commit is on DISK and the clone is REUSED:
    the next run starts with a clear latch on a clone that still carries the refused commit,
    commits the next winner on top of it, and publishes an ancestor its single-revision scan
    never looked at. A guard scoped to a process lifetime is checking something shorter-lived
    than the hazard. Raised by the GPT review of this branch; the conductor asked for
    quarantine over a persisted latch, because a persisted latch raises the question of when
    it clears and one that clears on the wrong event is worse than none.

    Retirement is the app's OWN primitive for exactly this, not a new mechanism:
    ``_retire_unsafe_clone`` renames the root directory aside, and its docstring already
    says it "prevents a later run from adopting a rejected provisional commit". Reuse in
    ``_setup_safe_clone`` hinges on the canonical ``<dest>/.git`` being a directory, so a
    retired clone is not reused -- the next run clones fresh. Nothing about how clones are
    located or named changes, and the bytes are preserved for diagnosis.

    Unconditional: unlike :meth:`_retire_if_unsafe`, this does not consult the isolation
    probe first. The trigger is the failed rollback itself, which is already known, so a
    probe verdict could only talk us out of quarantining a clone we know is poisoned.

    RETIREMENT ITSELF CAN FAIL, and the reason it fails is correlated with the reason the
    rollback failed rather than independent of it: a Windows process still holding handles
    inside the tree makes both the force-checkout and the rename fail from that one cause.
    Logging "MUST NOT be reused" is not a guard -- the next run does not read this log --
    so a marker is persisted at a masked crew-home leaf (``_mark_clone_quarantined``) and
    ``_setup_safe_clone`` refuses a marked clone. Reuse there attests git metadata and
    remotes and never inspects the branch tip, so without the marker the poisoned tree
    passes every existing check. The marker deliberately does NOT live beside the clone:
    the scratch tree is one the agent works in, so a marker there could be truncated
    through a pre-placed hardlink or simply deleted.

    THE MARKER IS WRITTEN FIRST, BEFORE THE RENAME IS ATTEMPTED. Ordering it after made
    the durable record depend on a path reached only once two things had already failed,
    and a transient failure of the marker write itself then left the clone reusable with
    nothing recording that it must not be -- fail-open at the exact moment the guard is
    needed. Written first, the record exists before anything is disturbed, and a rename
    that then SUCCEEDS makes it stale rather than wrong: the marker names a directory, so
    it stops reporting once that directory is gone and is pruned on the way past.

    THREE LEVERS, AND THE CANONICAL NAME MUST NOT SURVIVE ALL THREE. Recording a marker
    and renaming the tree aside fail from independent causes -- an unwritable marker root
    against a held file handle -- so either alone can be the one that works. When BOTH
    fail the clone is still sitting at the name the next run looks up with nothing saying
    it must not be, so the last lever is to remove the tree outright: a partially removed
    clone presents no ``.git`` directory, and ``_setup_safe_clone`` refuses a destination
    that exists and is not a git repository. It can fail too, from the same
    held handle that defeated the rename, and then the log is all that is left -- but the
    reuse check has one more independent reason to refuse by then, because a marker root
    that could not take the write cannot pass ``_quarantine_root``'s writability probe
    either, and that makes the guard fail closed for every clone. All three halves raised
    by the GPT review of this branch.
    """
    from kiro_crew.platform_compat import rmtree_force

    from ..backend.clone_setup import _mark_clone_quarantined, _retire_unsafe_clone

    marked = _mark_clone_quarantined(self.clone, f"provisional rollback to {pre_sha[:10]} failed")
    if marked is None:
        self.log.error(
            "could not record a quarantine marker for %s before retiring it; a later run"
            " has nothing to read, so retirement is now the only durable guard",
            self.clone,
        )
    retained = _retire_unsafe_clone(self.clone)
    self._repository_retired = True
    if retained is None:
        self.log.error(
            "could not retire the clone after the failed rollback to %s; it is left unsafe"
            " in place and MUST NOT be reused (quarantine marker: %s)",
            pre_sha[:10],
            marked or "could not be written either",
        )
        if marked is None and not rmtree_force(self.clone):
            self.log.error(
                "could not remove %s either; every guard against reusing it has failed and"
                " it must be deleted by hand before another run starts",
                self.clone,
            )
    else:
        self.log.error(
            "clone retired to %s after the failed rollback to %s, so no later run can adopt"
            " the commit that was refused",
            retained,
            pre_sha[:10],
        )
    self._progress(
        stage="rollback_failed",
        error="provisional rollback failed; clone quarantined so it is not reused",
        retained_clone=str(retained or ""),
    )


def _finalize_winner_commit(
    self: Driver, winner: Proposal, *, verify, reproduce=None, cycle: int, diff_ref: str
) -> str:
    """Apply the winner's diff to the working branch (local commit only), with the
    §2.4 attributable commit message (stage breakdown + guardrails + reproduce + RH
    guards + diff-ref). Every kept commit is independently reviewable (06_*.md §3.1).

    ``reproduce`` is the pipeline's SECOND independent A/B :class:`Measurement` (the one
    the CR description used); the commit message renders its REAL delta on the
    ``reproduce:`` line so commit and CR never disagree (06_*.md §3.1/§3.2). It falls
    back to ``verify`` only if the pipeline did not carry a reproduce measurement (it
    always does on a filed perf win) — never silently re-using VERIFY when the real
    numbers are available."""
    if not winner.diff.strip():
        return _full_head_id(self.clone)
    if True:
        # Use the INDEPENDENT reproduce measurement (carried back from the pipeline) for
        # the ``reproduce:`` line so the kept-commit message states the real second-A/B
        # delta the CR cited, not an echo of VERIFY (06_*.md §3.1/§3.2; CrOutcome.reproduce).
        msg = D.perf_commit_message(
            proposal=winner,
            verify=verify,
            reproduce=reproduce if reproduce is not None else verify,
            cycle=cycle,
            primary_name=self.profile.ruler.primary_name,
            unit=self.profile.ruler.unit,
            diff_ref=diff_ref,
            guardrail_tolerances=self.guardrail_tolerances,
        )
        # AMEND: the provisional commit already carries the winner's tree (that is what
        # the draft pushed); only its message is replaced.
        _git(
            ["commit", "-q", "--amend", "-m", self._redact_commit_message(msg)],
            self.clone,
        )
    return _full_head_id(self.clone)


def _stage_bug_winner(self: Driver, winner: Proposal) -> bool:
    """Apply + stage the bug fix on the working branch. False when it will not apply.

    Split out for the same reason as :meth:`_stage_winner`: the recipe pushes the shared
    clone's HEAD, so the fix has to be in this tree BEFORE the pipeline drafts.
    """
    _git(["checkout", normalize_branch(self.branch)], self.clone)
    if not winner.diff.strip():
        return True

    # Apply with --3way: the diff was authored in a throwaway WORKTREE forked off a
    # base sha that may have drifted from the clone's branch HEAD (a prior candidate
    # landed, a clone-sync moved HEAD, or the agent touched an artifact like uv.lock
    # that already exists here). A plain ``git apply`` fails outright on any context
    # mismatch or "already exists in working directory" — the observed committed=0
    # cause ("bug fix diff did not apply: error: uv.lock: already exists").
    # --3way falls back to a blob-level 3-way merge, which reconciles drift and
    # absorbs an already-present file instead of aborting. We retry plain-apply first
    # (cheapest, no index churn) and only fall back to 3-way so behavior is unchanged
    # when the base matches.
    def _apply(extra: list[str]):
        return subprocess.run(
            ["git", "-C", str(self.clone), "apply", *extra],
            input=winner.diff,
            capture_output=True,
            # Same byte-exact payload round-trip as the plain apply above.
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        )

    ap = _apply([])
    if ap.returncode != 0:
        self.log.info(
            "bug fix plain-apply failed (%s) — retrying with --3way",
            redact_log_via_context((ap.stderr or "").strip())[:120],
        )
        ap = _apply(["--3way"])
    if ap.returncode != 0:
        self.log.error(
            "bug fix diff did not apply (even --3way): %s",
            redact_log_via_context(ap.stderr or "")[:200],
        )
        return False
    _git(["add", "-A"], self.clone)
    return True


def _commit_bug_winner_provisional(self: Driver, winner: Proposal) -> bool:
    """Apply + commit the bug fix with a placeholder message. See
    :meth:`_commit_winner_provisional` for why a commit rather than a staged index."""
    if not self._stage_bug_winner(winner):
        return False
    if not winner.diff.strip():
        return True
    # Same as the perf twin: a failed commit must not report success, or the draft/push
    # publishes a HEAD that lacks the fix. Raised by the GPT review of this branch.
    commit = _git(
        # FIXED message — never `winner.cand_id`. `cand_id` embeds the model-chosen
        # `candidate.target`, and `_short` only restricts to alnum/`_`/`-`, which is
        # exactly the character class of an AWS key id or a `ghp_` token. Measured:
        # `src/m.py::AKIAIOSFODNN7EXAMPLE` produced
        # `c1_wide_m_py_AKIAIOSFODNN7EXAMPLE_d469bc5b`. This message is what the
        # DRAFT PUSH publishes — the redacted amend happens AFTER the push (871 ->
        # 887 -> 903) — so an unscanned cand_id lands in GitHub history, which cannot
        # be edited without rewriting it. The cand_id is still in the run archive and
        # the ledger, where it belongs. Raised by the GPT review of this branch.
        ["commit", "-q", "-m", "wip(auto-improvement): staging a verified candidate"],
        self.clone,
    )
    if commit.returncode != 0:
        self.log.error(
            "provisional bug commit failed for %s: %s",
            winner.cand_id,
            redact_log_via_context(commit.stderr or "")[:200],
        )
        self._discard_staged(f"a failed provisional bug commit for {winner.cand_id}")
        return False
    return True


def _finalize_bug_winner_commit(
    self: Driver, winner: Proposal, *, bug_res: BugGateResult, cycle: int, diff_ref: str
) -> str:
    """Apply the bug fix to the working branch (local commit only). The commit
    message states the defect + the RED→GREEN correctness narrative (not a perf
    metric — 05_*.md §4.2 / 06_*.md §3.1 contrast the bug narrative with the perf A/B)."""
    if winner.diff.strip():
        # AMEND the provisional commit: its tree is what the draft pushed; only the
        # message is replaced (staging + --3way happened in _stage_bug_winner).
        msg = D.bug_commit_message(proposal=winner, bug_res=bug_res, cycle=cycle, diff_ref=diff_ref)
        _git(
            ["commit", "-q", "--amend", "-m", self._redact_commit_message(msg)],
            self.clone,
        )
    return _full_head_id(self.clone)
