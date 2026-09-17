---
name: dynamic-workflows
description: Author, run, inspect, or resume multi-phase agent workflows with the workflow tools.
---

# Dynamic workflows

Check `workflow_library_list` for a reusable definition. Use `workflow_run(intent=...)`
to author and launch, or `workflow_author` to inspect the script first. Read
`workflow_result` before claiming success; a failed authoring phase means no workers ran.

Check the live tool schema before using author-selection fields. If they are absent
or the separate author backend is failing, draft the script in this session and call
`workflow_run(source=...)`. This supported path uses the current agent as author and
still runs workers through the workflow engine; it needs no gateway restart.

Choose `author_agent` and `author_model` for drafting; workers choose separately with
`ctx.agent(agent=..., model=...)`. Use the installed agent roster and that backend's
advertised models, honor explicit selections, and omit overrides when discovery is
unavailable. Never infer the worker's model from the parent or author.

Use local crews for bounded extraction, classification, summarization, and routine
tool work when their verified tools, quality, and input-plus-output context budget
fit. Keep local concurrency within lane capacity. Prefer a stronger available model
for ambiguous synthesis, difficult debugging, and consequential review. Check results
and escalate failed acceptance checks explicitly; do not silently substitute models.

Keep phases and worker labels specific, guard missing worker results, and inspect
`workflow_status` / `workflow_result`. Use `workflow_rerun_subtree` to restart only the
affected portion after understanding the failure.

Scripts declare a literal `META` dict and `async def workflow(ctx)`. Do not import
modules or access files directly. Await `ctx.agent(...)` and `ctx.parallel(...)`;
call `ctx.phase(...)` and `ctx.log(...)` without awaiting. Guard each agent result
against `None` and return a JSON-serializable result.
