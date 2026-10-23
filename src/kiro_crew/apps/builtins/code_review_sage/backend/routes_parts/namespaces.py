"""The learning namespaces: their CRUD, the learnings view and the consolidation.

Listing, creating and deleting namespaces, the read-only learnings view, and the
one-shot merge of staged candidates into the ruleset. New learning-namespace
endpoints go here.

Composed onto :mod:`kiro_crew.apps.builtins.code_review_sage.backend.routes`;
see this package's ``__init__``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.code_review_sage.backend.routes import (
        _CONSOLIDATE_STATE,
        _CONSOLIDATING,
        _MERGE_MAX_BYTES,
        _NS_OPS_LOCK,
        _TASKS,
        _load_review_section,
        _write_review_section,
        hooks,
        learning,
        logger,
        require_owner_dashboard_request,
        review_driver,
        review_pool,
        web,
    )


async def _handle_learning_rule_lifecycle(request: web.Request) -> web.Response:
    """Archive or restore one governed rule after an explicit operator action."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    namespace = str(body.get("namespace") or learning.DEFAULT_NAMESPACE)
    record_id = str(body.get("record_id") or "").strip()
    if not record_id:
        return web.json_response(
            {"code": "record_required", "error": "record_id is required"}, status=400
        )
    action = "restore" if request.match_info.get("action") == "restore" else "archive"

    async def _audit(outcome: str) -> None:
        def _emit() -> None:
            from kiro_crew.sel import sel

            sel().log_api_access(
                caller="code-review-sage",
                operation=f"{action}_learning_rule",
                outcome=outcome,
                resources=f"learnings/{namespace}/{record_id}",
            )

        try:
            await asyncio.to_thread(_emit)
        except Exception:
            logger.warning("SEL audit failed for %s learning rule", action, exc_info=True)

    def _change() -> dict:
        # A markdown-only namespace is adopted only because this operator asked
        # to archive a rule. Normal reads and startup remain migration-free.
        if action == "archive" and not learning.load_learning_records(namespace=namespace):
            learning.migrate_legacy_learning_records(namespace=namespace)
        if action == "archive":
            return learning.archive_learning_record(
                record_id, operator="dashboard", namespace=namespace
            )
        return learning.restore_learning_record(
            record_id, operator="dashboard", namespace=namespace
        )

    try:
        result = await asyncio.to_thread(_change)
    except KeyError:
        await _audit("denied")
        return web.json_response(
            {"code": "record_not_found", "error": "learning record not found"}, status=404
        )
    except ValueError as exc:
        await _audit("denied")
        return web.json_response({"code": "invalid_lifecycle", "error": str(exc)}, status=400)
    await _audit("success")
    return web.json_response({"ok": True, "record": result["record"]})


async def _handle_candidates_delete(request: web.Request) -> web.Response:
    """DELETE .../learnings/candidates — drop staged candidates without a merge.

    ``candidate_ids`` is a multiset, exactly as ``consolidate`` takes it: a legacy
    candidate file can hold several entries under one id and each element is one
    removal. Nothing here touches the ruleset or the governed sidecar, because
    discarding a staged learning is not a review decision.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    ns = str(body.get("namespace") or learning.DEFAULT_NAMESPACE)
    if not (ns == learning.DEFAULT_NAMESPACE or learning._is_valid_ns_name(ns)):
        return web.json_response(
            {"code": "invalid_namespace", "error": f"invalid namespace {ns!r}"}, status=400
        )
    requested = body.get("candidate_ids")
    if (
        not isinstance(requested, list)
        or not requested
        or any(not isinstance(item, str) or not item for item in requested)
    ):
        return web.json_response(
            {"code": "invalid_candidate_ids", "error": "candidate_ids must be a non-empty list"},
            status=400,
        )

    async def _audit(outcome: str) -> None:
        def _emit() -> None:
            from kiro_crew.sel import sel

            sel().log_api_access(
                caller="code-review-sage",
                operation="delete_learned_candidates",
                outcome=outcome,
                resources=f"learnings/{ns}",
            )

        try:
            await asyncio.to_thread(_emit)
        except Exception:
            logger.warning("SEL audit failed for delete_learned_candidates", exc_info=True)

    async with _NS_OPS_LOCK:
        # Held across the check and the rewrite: a merge that already snapshotted
        # these entries would otherwise apply against a catalog the worker never read.
        if ns in _CONSOLIDATING:
            return web.json_response(
                {
                    "code": "consolidation_in_progress",
                    "error": "a consolidation is already running for this namespace",
                },
                status=409,
            )
        candidates = await asyncio.to_thread(learning.list_candidate, None, ns)
        before = len(candidates)
        available = {str(item["id"]) for item in candidates}
        unknown = sorted({item for item in requested if item not in available})
        if unknown:
            return web.json_response(
                {
                    "code": "invalid_candidate_ids",
                    "error": f"unknown candidate ids: {', '.join(unknown[:5])}",
                },
                status=400,
            )
        await asyncio.to_thread(learning.clear_candidate, None, ns, list(requested))
    remaining = len(await asyncio.to_thread(learning.list_candidate, None, ns))
    await _audit("ok")
    return web.json_response(
        {
            "ok": True,
            "namespace": ns,
            "requested": len(requested),
            "removed": before - remaining,
            "remaining": remaining,
        }
    )


def _preview_namespace(request: web.Request) -> tuple[str | None, web.Response | None]:
    ns = request.query.get("namespace") or learning.DEFAULT_NAMESPACE
    if ns != learning.DEFAULT_NAMESPACE and not learning._is_valid_ns_name(ns):
        return None, web.json_response({"code": "invalid_namespace"}, status=400)
    return ns, None


async def _handle_consolidation_preview_list(request: web.Request) -> web.Response:
    """GET .../consolidation-previews — read-only namespace-local proposal index."""
    ns, error = _preview_namespace(request)
    if error is not None:
        return error
    previews = await asyncio.to_thread(learning.list_consolidation_previews, namespace=ns)
    return web.json_response({"code": "preview_list", "namespace": ns, "previews": previews})


async def _handle_consolidation_preview_get(request: web.Request) -> web.Response:
    """GET .../consolidation-previews/{preview_id} — read-only artifact detail."""
    ns, error = _preview_namespace(request)
    if error is not None:
        return error
    try:
        preview = await asyncio.to_thread(
            learning.get_consolidation_preview, request.match_info["preview_id"], namespace=ns
        )
    except (OSError, ValueError):
        return web.json_response({"code": "preview_not_found"}, status=404)
    return web.json_response({"code": "preview_detail", "preview": preview})


async def _handle_consolidation_preview_apply(request: web.Request) -> web.Response:
    """POST .../consolidation-previews/apply — explicit confirmation is mandatory."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return web.json_response({"code": "invalid_request"}, status=400)
    ns = str(body.get("namespace") or learning.DEFAULT_NAMESPACE)
    preview_id = body.get("preview_id")
    if (ns != learning.DEFAULT_NAMESPACE and not learning._is_valid_ns_name(ns)) or not isinstance(
        preview_id, str
    ):
        return web.json_response({"code": "invalid_request"}, status=400)
    try:
        result = await asyncio.to_thread(
            learning.apply_consolidation_preview,
            preview_id,
            confirmed=body.get("confirm") is True,
            namespace=ns,
        )
    except (OSError, ValueError):
        return web.json_response({"code": "preview_not_found"}, status=404)
    status = 200 if result.get("ok") else 409
    if result.get("code") == "confirmation_required":
        status = 400
    return web.json_response({**result, "code": result["code"]}, status=status)


async def _handle_namespaces(request: web.Request) -> web.Response:
    # fmt: off
    """GET    -> {namespaces:[{name, patterns, candidate, active}], active:[...]}
    POST   body {name}            -> create a namespace
    DELETE body {name}            -> delete a (non-default) namespace."""
    if request.method == "GET":
        # list_namespaces() walks a dir and the per-namespace loop does a
        # synchronous read_text()+parse for EACH namespace — unbounded sync IO.
        # Offload the whole build to a thread so it can't freeze the event loop.
        def _build_ns_response() -> dict:
            names = learning.list_namespaces()
            active = set(learning.get_active_namespaces())
            out = []
            for n in names:
                out.append(
                    {
                        "name": n,
                        "patterns": len(learning.list_patterns(namespace=n)),
                        "candidate": learning.candidate_count(namespace=n),
                        "active": n in active,
                    }
                )
            return {"namespaces": out, "active": sorted(active)}

        try:
            return web.json_response(await asyncio.to_thread(_build_ns_response))
        except Exception as exc:
            logger.warning("namespace list failed: %s", exc, exc_info=True)
            return web.json_response({"namespaces": [], "active": ["default"]})

    owner_denied = await require_owner_dashboard_request(
        request, "code_review_sage.namespaces_write"
    )
    if owner_denied is not None:
        return owner_denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str((body or {}).get("name", "")).strip()
    if not name:
        return web.json_response(
            {"code": "name_required", "ok": False, "error": "name required"}, status=400
        )

    # Audit namespace create/delete (filesystem ops that change the learning
    # scope). log_api_access does synchronous file IO (appends to the audit
    # ledger), so it is offloaded off the event loop. Audit failure is swallowed
    # so it can never break the response.
    async def _audit(operation: str, ok: bool) -> None:
        def _emit() -> None:
            from kiro_crew.sel import sel  # circular import: sel->config->apps cycle

            sel().log_api_access(
                caller="code-review-sage",
                operation=operation,
                outcome="success" if ok else "denied",
                resources=f"learnings/namespaces/{name}",
            )

        try:
            await asyncio.to_thread(_emit)
        except Exception as exc:
            logger.warning("SEL audit failed for %s: %s", operation, exc)

    if request.method == "POST":
        res = await asyncio.to_thread(learning.create_namespace, name)
        await _audit("create_namespace", bool(res.get("ok")))
        return web.json_response(res, status=200 if res.get("ok") else 400)
    if request.method == "DELETE":
        # Deleting a namespace and starting a consolidation for it are mutually
        # exclusive, and BOTH sides take `_NS_OPS_LOCK` so there is no interleaving
        # to reason about. Two earlier shapes of this guard were wrong in ways worth
        # recording: a single check before the prune left a window during the
        # prune's await, and adding a SECOND check after the prune closed that
        # window but could reject the delete with the namespace already pruned from
        # the active list -- refusing the request while silently deactivating the
        # namespace it refused to delete. Holding the lock across check + prune +
        # rmtree removes the window instead of splitting it.
        async with _NS_OPS_LOCK:
            if name in _CONSOLIDATING:
                return web.json_response(
                    {
                        "code": "consolidation_in_progress",
                        "error": "a consolidation is running for this namespace — "
                        "wait for it to finish, then delete",
                    },
                    status=409,
                )

            # Delete FIRST, prune only on success -- one offloaded helper, since both
            # halves are synchronous file IO that must never run on the event loop.
            # Order matters: `delete_namespace` refuses the default namespace, an
            # invalid name, an out-of-tree path and a missing directory, and none of
            # those refusals depend on the active list. Pruning first would therefore
            # deactivate a namespace the API then reports it could not delete --
            # reviews quietly stop using it while the caller is told nothing changed.
            def _delete_then_prune() -> dict:
                res = learning.delete_namespace(name)
                if not res.get("ok"):
                    return res
                try:
                    sec = _load_review_section()
                    patch: dict[str, Any] = {}
                    if name in (sec.get("active_namespaces") or []):
                        remaining = [n for n in sec["active_namespaces"] if n != name]
                        patch["active_namespaces"] = remaining or ["default"]
                    bindings = dict(sec.get("namespace_bindings") or {})
                    if name in bindings:
                        bindings.pop(name)
                        patch["namespace_bindings"] = bindings
                    if patch:
                        _write_review_section(patch)
                except Exception:
                    logger.debug(
                        "could not prune deleted namespace from review settings", exc_info=True
                    )
                return res

            res = await asyncio.to_thread(_delete_then_prune)
        await _audit("delete_namespace", bool(res.get("ok")))  # destructive rmtree
        return web.json_response(res, status=200 if res.get("ok") else 400)
    return web.json_response(
        {"code": "method_not_allowed", "error": "method not allowed"}, status=405
    )


async def _handle_learnings(request: web.Request) -> web.Response:
    """GET /api/apps/code-review-sage/learnings?namespace=<ns>

    Read-only view of a namespace's self-learning state so the dashboard can show
    what the reviewer has actually learned:
      - ``patterns``   — the consolidated heuristics reviews load (learned-patterns.md)
      - ``candidate``  — pending learnings staged during reviews, awaiting the
                         human-triggered ``learn-from-sage`` consolidation
    Both come from on-disk markdown; consolidation itself is an AI merge run via
    the skill (never a blind REST overwrite), so this endpoint deliberately does
    not mutate anything. ``?namespace=`` defaults to 'default'."""
    ns = request.query.get("namespace") or learning.DEFAULT_NAMESPACE

    def _build() -> dict:
        try:
            patterns = learning.list_rule_entries(namespace=ns)
        except Exception:
            patterns = []
        try:
            candidate = learning.list_candidate(namespace=ns)
        except Exception:
            candidate = []
        state = dict(_CONSOLIDATE_STATE.get(ns) or {})
        return {
            "namespace": ns,
            "patterns": patterns,
            "candidate": candidate,
            "consolidating": bool(state.get("running")),
            "consolidate_error": state.get("error"),
            # The machine-readable cause behind the prose: "malformed_worker_output"
            # covers both a bad reason_code and a promotion the sidecar refused.
            "consolidate_error_code": state.get("error_code"),
        }

    try:
        # Namespace-scoped file reads + markdown parse are synchronous IO — keep
        # them off the shared gateway event loop.
        return web.json_response(await asyncio.to_thread(_build))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("learnings view failed for ns=%s: %s", ns, exc, exc_info=True)
        return web.json_response(
            {
                "namespace": ns,
                "patterns": [],
                "candidate": [],
                "consolidating": False,
                "consolidate_error": None,
                "consolidate_error_code": None,
            }
        )


async def _consolidate_bg(
    ns: str, candidate_ids: list[str] | None = None, snapshot: list[dict] | None = None
) -> None:
    """Ask a worker for a proposal, then persist it without touching live learning state."""
    out_path = ""
    input_path = ""
    try:
        if candidate_ids is None or snapshot is None:
            candidates = await asyncio.to_thread(learning.list_candidate, None, ns)
            candidate_ids = sorted({str(item["id"]) for item in candidates})
            snapshot = [learning._candidate_snapshot_entry(item) for item in candidates]
        ns_dir = await asyncio.to_thread(learning._namespace_dir, ns, None)
        live = await asyncio.to_thread(learning.common_file, None, ns)
        out_path = str(Path(ns_dir) / "consolidation-proposal.json")
        input_path = str(Path(ns_dir) / "consolidation-candidates.md")
        selected = [item["pattern"] for item in snapshot if item["id"] in candidate_ids]
        await asyncio.to_thread(
            learning._atomic_write,
            Path(input_path),
            learning._CANDIDATE_HEADER
            + "\n".join(learning.render_pattern(item) for item in selected),
        )
        for scratch in (out_path,):
            try:
                Path(scratch).unlink()
            except OSError:
                pass
        loop = asyncio.get_running_loop()
        pool = review_pool.get_pool()
        dispatch = review_pool.make_sync_dispatch(loop, pool)
        await pool.begin_batch()
        selection = [
            {"id": str(item["id"]), "title": str(item["pattern"].get("title") or "")}
            for item in snapshot
            if str(item["id"]) in candidate_ids
        ]
        try:
            blocked_promotions = await asyncio.to_thread(
                learning.promotion_ineligible_candidate_ids,
                snapshot,
                candidate_ids,
                namespace=ns,
            )
        except Exception:
            # An eligibility hint the worker can do without: the preview still
            # refuses an ineligible promotion on its own.
            blocked_promotions = []
            logger.debug("promotion eligibility probe failed for ns=%s", ns, exc_info=True)
        try:
            spawn = await asyncio.to_thread(
                dispatch,
                review_driver.build_consolidation_task(
                    ns,
                    str(live),
                    input_path,
                    out_path,
                    selection=selection,
                    blocked_promotions=blocked_promotions,
                ),
            )
        finally:
            await pool.end_batch()
        raw = None
        try:
            raw = await asyncio.to_thread(
                hooks.safe_read_file_bytes_nolink, out_path, str(ns_dir), max_bytes=_MERGE_MAX_BYTES
            )
        except Exception:
            logger.debug("consolidation proposal read rejected", exc_info=True)
        if not spawn.get("ok") or raw is None:
            worker_error = str(spawn.get("error") or "worker_failed")
            logger.warning("consolidation worker failed for ns=%s: %s", ns, worker_error)
            _CONSOLIDATE_STATE[ns] = {
                "running": False,
                "code": "worker_failed",
                "error_code": "worker_failed",
                "error": "the consolidation worker could not complete; try again",
            }
            return
        try:
            proposal = json.loads(raw.decode("utf-8", errors="strict"))
            preview = await asyncio.to_thread(
                learning.create_consolidation_preview,
                proposal,
                candidate_ids=candidate_ids,
                candidate_snapshot=snapshot,
                namespace=ns,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            # The worker finished and its proposal was refused, so this line is the
            # only account of why: without it a rejected merge is indistinguishable
            # from one that never ran.
            logger.warning(
                "consolidation proposal rejected for ns=%s selected=%d: %s: %s",
                ns,
                len(candidate_ids or []),
                type(exc).__name__,
                exc,
            )
            _CONSOLIDATE_STATE[ns] = {
                "running": False,
                "code": "malformed_worker_output",
                "error_code": str(exc),
                "error": "malformed_worker_output",
            }
            return
        _CONSOLIDATE_STATE[ns] = {
            "running": False,
            "code": "preview_ready",
            "preview_id": preview["preview_id"],
            "error": None,
        }
    except Exception as exc:
        logger.exception("consolidation preview failed for ns=%s", ns)
        _CONSOLIDATE_STATE[ns] = {
            "running": False,
            "code": "preview_failed",
            "error_code": type(exc).__name__,
            "error": str(exc),
        }
    finally:
        _CONSOLIDATING.discard(ns)
        for scratch in (out_path, input_path):
            if scratch:
                try:
                    await asyncio.to_thread(Path(scratch).unlink, True)
                except OSError:
                    pass


async def _handle_consolidate(request: web.Request) -> web.Response:
    """POST .../learnings/consolidate — queue a durable, human-confirmed proposal."""
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.consolidate")
    if owner_denied is not None:
        return owner_denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    ns = str(body.get("namespace") or learning.DEFAULT_NAMESPACE)
    if not (ns == learning.DEFAULT_NAMESPACE or learning._is_valid_ns_name(ns)):
        return web.json_response(
            {"code": "invalid_namespace", "error": f"invalid namespace {ns!r}"}, status=400
        )
    # Claim the namespace BEFORE the first await. Checking membership and then
    # awaiting the staged count left a window where two concurrent requests for
    # one namespace both passed the guard, then both dispatched a merge against
    # the same scratch path — the second overwriting or unlinking the first's
    # output. The check-and-add below has no await between its two halves, so on
    # the single event loop it is atomic; the claim is given back on every early
    # return.
    async with _NS_OPS_LOCK:
        if ns in _CONSOLIDATING:
            return web.json_response(
                {
                    "code": "consolidation_in_progress",
                    "error": "a consolidation is already running for this namespace",
                },
                status=409,
            )
        # Taken under the lock so an in-flight DELETE cannot land between this
        # check and the claim; the delete handler holds the same lock across its
        # whole check + prune + rmtree.
        _CONSOLIDATING.add(ns)
    try:
        candidates = await asyncio.to_thread(learning.list_candidate, None, ns)
    except Exception:
        _CONSOLIDATING.discard(ns)
        raise
    if not candidates:
        _CONSOLIDATING.discard(ns)
        return web.json_response(
            {
                "code": "nothing_to_consolidate",
                "error": "nothing to consolidate — no learnings are staged",
            },
            status=409,
        )

    requested_ids = body.get("candidate_ids")
    if requested_ids is None:
        candidate_ids = sorted({str(item["id"]) for item in candidates})
    elif (
        not isinstance(requested_ids, list)
        or len(set(requested_ids)) != len(requested_ids)
        or any(not isinstance(item, str) for item in requested_ids)
    ):
        _CONSOLIDATING.discard(ns)
        return web.json_response({"code": "invalid_candidate_ids"}, status=400)
    else:
        candidate_ids = requested_ids
    available_ids = {str(item["id"]) for item in candidates}
    if not candidate_ids or any(item not in available_ids for item in candidate_ids):
        _CONSOLIDATING.discard(ns)
        return web.json_response({"code": "invalid_candidate_ids"}, status=400)
    snapshot = [learning._candidate_snapshot_entry(item) for item in candidates]
    _CONSOLIDATE_STATE[ns] = {"running": True, "code": "preview_running", "error": None}
    # Strong ref, as with the review and post paths: a collected merge task would
    # leave `_CONSOLIDATING` claimed with nothing to release it, locking this
    # namespace out of consolidation until a restart.
    task = asyncio.create_task(_consolidate_bg(ns, candidate_ids, snapshot))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return web.json_response(
        {
            "ok": True,
            "code": "preview_queued",
            "namespace": ns,
            "candidate_ids": candidate_ids,
            "running": True,
        }
    )
