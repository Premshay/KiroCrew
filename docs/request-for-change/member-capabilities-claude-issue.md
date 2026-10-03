# Allow editor-managed crew members on Claude

Editor-enrolled crew members cannot start on the Claude backend. Startup rejects
the saved member definition with `capability_harness_unsupported`, and the
Capabilities panel reports "Runtime loading failed".

## Reproduction

1. Select the Claude backend and open a crew member's Capabilities editor.
2. Enroll the member and save an override, creating its private agent definition.
3. Start a fresh member session. Startup rejects the backend before loading it.

## Proposed change

Opt Claude into saved member-definition support only with evidence that its
session consumed the saved projection. Retain the exact parsed definition beside
the projected Model Context Protocol (MCP) servers, compare its digest with the
saved materialization after successful session creation or loading, and clear
that evidence when the process or projection changes. Preserve the existing
saved-revision, project, governance and MCP-readiness checks.

Use existing member essentials for saved prompts, file resources and mapped
skills. Keep runtime status unverified for unsupported hooks, native tool
restrictions, per-tool MCP mounting and approval shortcuts; report those fields
explicitly rather than claiming full native loading.

## Risk and validation

Claude does not natively consume Kiro agent definitions. A successful projection
must not imply enforcement of unsupported restrictions or alter existing Claude
permissions. Focused lifecycle tests cover creation, resume, withheld or changed
projections, unreadable state, stale evidence and partial verification. A live
Claude session still needs a rollout smoke test, including routing of generated
private definition names.
