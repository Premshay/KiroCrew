import { toApiError } from "../../api/apiError";
import type {
  DesignRound,
  DesignRoundInput,
  ProjectContext,
  ReviewRun,
  Scope,
  SlotData,
} from "./types";
import type { KiroCrewAgent } from "../../components/AgentSelector";

export interface AgentList {
  agents: KiroCrewAgent[];
  default_agent: string;
}

interface ResolvedAgentModel {
  model: string;
}

export interface ReviewRunInput {
  slot_key: string;
  agent: string;
  model: string;
  stage: string;
  source: Record<string, unknown>;
  screens: unknown[];
}

export interface ProjectContextInput {
  name: string;
  repository: string;
  context_paths: string[];
  notes: string;
}

// This app's own backend (mounted by the built-in at gateway startup). It does
// the clone / discover / render work server-side so the agent never runs a tool.
const DC = "/api/apps/design-critique";

// These hit the dashboard's own chat endpoints (NOT an app-scoped reverse proxy),
// so they are plain same-origin fetches — the same convention file-explorer's
// api.ts uses. An empty body (e.g. 204 on DELETE) is treated as success.
async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin", ...init });
  if (!r.ok) {
    throw await toApiError(r);
  }
  if (r.status === 204 || r.status === 205) return undefined as T;
  const text = await r.text();
  if (text.trim() === "") return undefined as T;
  return JSON.parse(text) as T;
}

const postJson = <T>(path: string, body: unknown): Promise<T> =>
  jsonFetch<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body != null ? JSON.stringify(body) : undefined,
  });

// A started backend job (discover/render) or, for the trivial discover kinds
// (figma/url/blocked), the finished payload returned inline with no job.
interface JobHandle {
  job: string;
}
const isJobHandle = (x: unknown): x is JobHandle =>
  !!x && typeof (x as JobHandle).job === "string";

// Poll a detached backend job until it is no longer running, then resolve its
// result or throw its error. The scan runs server-side regardless of this loop,
// so a page that navigates away simply stops polling — the job keeps going and a
// later visit reconnects by polling the same id (see pollDiscover / pollRender).
async function pollJob<T>(base: string, jobId: string): Promise<T> {
  let misses = 0;
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    let r: { status: string; result?: T; error?: string };
    try {
      r = await jsonFetch(base + "?job=" + encodeURIComponent(jobId));
    } catch (e) {
      // Tolerate a transient blip (gateway restart) for a few cycles.
      if (++misses >= 8)
        throw e instanceof Error ? e : new Error("lost contact with the scan");
      continue;
    }
    misses = 0;
    if (r.status === "running") continue;
    if (r.status === "error")
      throw new Error(r.error || "that run did not finish");
    return r.result as T;
  }
}

export const designCritiqueApi = {
  // Open a throwaway worker slot. memory_mode 'temporary' keeps it out of memory
  // snapshots; mode 'design-critique' keeps it OUT of the chat sidebar (the chat
  // list only renders '' and 'orchestrator').
  listAgents: () => jsonFetch<AgentList>("/api/agents"),

  resolveAgentModel: (agent: string) =>
    jsonFetch<ResolvedAgentModel>(
      "/api/agents/resolved-model?agent=" + encodeURIComponent(agent),
    ),

  listReviewRuns: () =>
    jsonFetch<{ runs: ReviewRun[] }>("/api/apps/design-critique/runs"),

  createReviewRun: (input: ReviewRunInput) =>
    postJson<{ run: ReviewRun }>("/api/apps/design-critique/runs", input),

  updateReviewRun: (runId: string, input: Partial<ReviewRun>) =>
    jsonFetch<{ run: ReviewRun }>(
      "/api/apps/design-critique/runs/" + encodeURIComponent(runId),
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      },
    ),

  listProjectContexts: () =>
    jsonFetch<{ contexts: ProjectContext[] }>(
      "/api/apps/design-critique/contexts",
    ),

  createProjectContext: (input: ProjectContextInput) =>
    postJson<{ context: ProjectContext }>(
      "/api/apps/design-critique/contexts",
      input,
    ),

  updateProjectContext: (
    contextId: string,
    input: Partial<ProjectContextInput>,
  ) =>
    jsonFetch<{ context: ProjectContext }>(
      "/api/apps/design-critique/contexts/" + encodeURIComponent(contextId),
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      },
    ),

  deleteProjectContext: (contextId: string) =>
    jsonFetch<void>(
      "/api/apps/design-critique/contexts/" + encodeURIComponent(contextId),
      {
        method: "DELETE",
      },
    ),

  listDesignRounds: () =>
    jsonFetch<{ rounds: DesignRound[] }>(
      "/api/apps/design-critique/design-rounds",
    ),

  createDesignRound: (input: DesignRoundInput) =>
    postJson<{ round: DesignRound }>(
      "/api/apps/design-critique/design-rounds",
      input,
    ),

  updateDesignRound: (roundId: string, input: Partial<DesignRound>) =>
    jsonFetch<{ round: DesignRound }>(
      "/api/apps/design-critique/design-rounds/" + encodeURIComponent(roundId),
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      },
    ),

  openSlot: (agent: string) =>
    postJson<{ key: string }>("/api/chat/slots", {
      name: "dc-" + Date.now(),
      agent,
      memory_mode: "temporary",
      mode: "design-critique",
    }),

  getSlot: (slotKey: string) =>
    jsonFetch<SlotData>("/api/chat/slots/" + encodeURIComponent(slotKey)),

  // Fire a message at a slot. The response body is not JSON we care about, so a
  // parse error is swallowed — only a real HTTP/network error propagates.
  send: (slotKey: string, agent: string, message: string): Promise<void> =>
    jsonFetch<void>("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // memory_mode AND mode must be repeated here, not only at slot creation.
      // POST /api/chat auto-creates a missing slot, and with neither in the body
      // it falls back to the persistent default with surface '' — so if the
      // gateway restarts mid-run (the slot is in memory, not on disk) the next
      // send would silently recreate this critique slot with memory reads and
      // writes ENABLED and visible in the chat sidebar (whose allowlist admits
      // surface ''). Passing them is also safe when the slot exists:
      // get_or_create_slot only raises on a memory_mode mismatch and ignores
      // mode for existing slots, and both match what openSlot() asked for.
      body: JSON.stringify({
        message,
        slot: slotKey,
        agent,
        memory_mode: "temporary",
        mode: "design-critique",
      }),
    }).catch((e: unknown) => {
      if (e instanceof SyntaxError) return;
      throw e;
    }),

  deleteSlot: (slotKey: string): Promise<void> =>
    jsonFetch<void>("/api/chat/slots/" + encodeURIComponent(slotKey), {
      method: "DELETE",
    }).catch(() => {}),

  uploadFiles: async (files: File[]): Promise<{ paths: string[] }> => {
    const fd = new FormData();
    files.forEach((f) => fd.append("file", f));
    const up = await fetch("/api/upload/file", {
      method: "POST",
      body: fd,
      credentials: "same-origin",
    });
    if (!up.ok) throw await toApiError(up);
    return up.json();
  },

  // STEP 1 — the backend clones (if needed), lists candidate screens, and probes
  // which ones actually render. Heavy kinds (repo/local) run as a detached backend
  // job: the POST returns {job} and we poll it, so navigating away no longer
  // cancels the scan. Trivial kinds (figma/url/blocked) come back inline with no
  // job. `onJob` fires with the job id the moment it exists, so the caller can
  // persist it and reconnect after a navigation.
  discover: async (
    kind: string,
    value: string,
    onJob?: (jobId: string) => void,
  ): Promise<Scope & { handle?: string }> => {
    const started = await postJson<JobHandle | (Scope & { handle?: string })>(
      DC + "/discover",
      { kind, value },
    );
    if (isJobHandle(started)) {
      onJob?.(started.job);
      return pollJob<Scope & { handle?: string }>(
        DC + "/discover",
        started.job,
      );
    }
    return started;
  },

  // Reconnect to an in-flight discover job by id (resume path — never re-POSTs).
  pollDiscover: (jobId: string): Promise<Scope & { handle?: string }> =>
    pollJob<Scope & { handle?: string }>(DC + "/discover", jobId),

  // STEP 2 — the backend renders the picked screens to PNGs and returns their
  // absolute paths. Always a detached job: the POST returns {job}, then we poll.
  render: (
    body: {
      kind: string;
      value: string;
      handle: string;
      picks: Array<{ id: string; label: string; ref?: string }>;
    },
    onJob?: (jobId: string) => void,
  ): Promise<{
    screens: Array<{ step: number; label: string; path: string }>;
    couldNotSee: string[];
  }> =>
    postJson<JobHandle>(DC + "/render", body).then((started) => {
      onJob?.(started.job);
      return pollJob(DC + "/render", started.job);
    }),

  // Reconnect to an in-flight render job by id (resume path — never re-POSTs).
  pollRender: (
    jobId: string,
  ): Promise<{
    screens: Array<{ step: number; label: string; path: string }>;
    couldNotSee: string[];
  }> => pollJob(DC + "/render", jobId),

  // The critique method text, inlined into the prompt so the agent does not have
  // to read it with a tool.
  method: (): Promise<{ checklist: string }> => jsonFetch(DC + "/method"),
};

export const fileUrl = (p: string): string =>
  "/api/file-raw?path=" + encodeURIComponent(p);
