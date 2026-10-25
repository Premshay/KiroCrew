import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { renderCritiquePresentation } from "./CritiquePresentation";

import { designCritiqueApi, fileUrl } from "./api";
import {
  BLOCKED,
  DEFAULT_AGENT,
  HARD_CAP_MS,
  MAX_SCREENS,
  SAMPLE_REPORT,
  SAMPLE_SCREENS
} from "./constants";
import { useNarrow, useReduceMotion, useToasts } from "./hooks";
import { fmtNumber } from "../../i18n/format";
import { ASK_CONTEXT, ASK_FOLLOW_UP, ASK_PROMPT, IMAGES_PROMPT } from "./prompts";
import type {
  Ask,
  Blocked,
  DesignRound,
  DiscoveryScreen,
  Flow,
  HistoryEntry,
  Job,
  Phase,
  Report,
  ReviewBrief,
  ReviewRun,
  Scope,
  Screen,
  Sel,
  SlotData,
  StagedItem
} from "./types";
import {
  beginPendingCritique,
  clearJob,
  detectKind,
  dropPendingCritique,
  jsonFromMessages,
  lastAssistant,
  loadHistory,
  loadJobs,
  loadLive,
  loadReviewBrief,
  loadSelectedAgent,
  loadSlots,
  looksLikeReport,
  markLive,
  normalizeReport,
  normalizeScope,
  resolveScreens,
  saveHistory,
  saveJob,
  saveReviewBrief,
  saveSelectedAgent,
  saveSlots,
  trackSlot,
  unmarkLive,
  untrackSlot
} from "./utils";

import { i18nT } from "../../i18n/t";
// Errors flagged with why the poll loop gave up — distinguishes navigate-away and
// timeout (both resumable) from a real failure.
type Flagged = Error & { cancelled?: boolean; timeout?: boolean };
const CANCELLED = (): Flagged =>
  Object.assign(new Error("cancelled"), { cancelled: true });
const TIMEOUT = (): Flagged =>
  Object.assign(new Error("still running"), { timeout: true });

export default function DesignCritiquePage() {
  const [queryClient] = useState(() => new QueryClient());
  return (
    <QueryClientProvider client={queryClient}>
      <DesignCritiquePageContent />
    </QueryClientProvider>
  );
}

function DesignCritiquePageContent() {
  const [phase, setPhase] = useState<Phase>("new");
  const [scope, setScope] = useState<Scope | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [refBrief, setRefBrief] = useState("");
  const [slot, setSlot] = useState("");
  // The backend render handle + target for a reference (repo/local/url) run, so
  // step 2 can render without re-cloning and without a chat slot doing the work.
  const [refHandle, setRefHandle] = useState("");
  const [refTarget, setRefTarget] = useState<{
    kind: string;
    value: string;
  } | null>(null);
  // Critique method text, fetched once from the backend and reused.
  const methodRef = useRef("");
  // slotKey is carried so the chip resolves the entry belonging to THIS run:
  // a second critique finishing first takes history index 0, and annotating
  // through the chip would then write onto the wrong critique's entry.
  const [justFinished, setJustFinished] = useState<{
    slotKey: string;
    read: string;
    screens: Screen[];
    report: Report;
  } | null>(null);
  // A BACKGROUND run that failed. The foreground run reports through `err`, but
  // a run the user had already navigated away from used to announce its failure
  // only as a toast — once that faded, the critique had simply vanished from the
  // history with nothing on screen saying why. Kept per run (keyed by slotKey)
  // until each is read and dismissed, so a second failure cannot overwrite the
  // first back into toast-only.
  const [backgroundFailures, setBackgroundFailures] = useState<
    Array<{ slotKey: string; message: string }>
  >([]);
  const [dragId, setDragId] = useState<string | null>(null);
  const [sel, setSel] = useState<Sel | null>(null);
  const [asks, setAsks] = useState<Ask[]>([]);
  const [openAskId, setOpenAskId] = useState<string | null>(null);
  const [askDraft, setAskDraft] = useState("");
  const [blocked, setBlocked] = useState<Blocked | null>(null);
  const [showAuth, setShowAuth] = useState(false);
  const [current, setCurrent] = useState<{
    report: Report | null;
    screens: Screen[];
    entryId?: number | null;
  } | null>(null);
  const [critiques, setCritiques] = useState<HistoryEntry[]>(loadHistory);
  // Two composer messages, kept apart on purpose: `err` is a failure (a caught
  // exception, a run that did not finish) and renders through ErrorNotice;
  // `hint` is a client-side check or a not-failed status ("still working") and
  // must not be dressed as an error. The two are mutually exclusive on screen:
  // setting either clears the other, so a run that times out ("still working")
  // and then fails never shows both verdicts above one composer.
  const [err, setErrRaw] = useState("");
  const [hint, setHintRaw] = useState("");
  const setErr = (m: string) => {
    setErrRaw(m);
    if (m) setHintRaw("");
  };
  const setHint = (m: string) => {
    setHintRaw(m);
    if (m) setErrRaw("");
  };
  const [dragging, setDragging] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [zoom, setZoom] = useState(false);
  const [active, setActive] = useState<number | null>(null);
  const [open, setOpen] = useState<Set<number>>(() => new Set([0]));
  const [screenIdx, setScreenIdx] = useState(0);
  const [refText, setRefText] = useState("");
  const [staged, setStaged] = useState<StagedItem[]>([]);
  const [elapsed, setElapsed] = useState(0);
  const [writing, setWriting] = useState(false);
  const [pendingKind, setPendingKind] = useState<string | null>(null);
  const [selectedAgent, setSelectedAgent] = useState(loadSelectedAgent);
  const [slotRunId, setSlotRunId] = useState("");
  const [reviewBrief, setReviewBrief] = useState<ReviewBrief>(loadReviewBrief);
  const [contextBusy, setContextBusy] = useState(false);
  const [designRoundBusy, setDesignRoundBusy] = useState(false);

  const reduceMotion = useReduceMotion();
  const { toasts, notify } = useToasts();
  const queryClient = useQueryClient();
  const { data: agentList } = useQuery({
    queryKey: ["design-critique-agents"],
    queryFn: designCritiqueApi.listAgents,
    enabled: typeof designCritiqueApi.listAgents === "function",
  });
  const { data: reviewRunData } = useQuery({
    queryKey: ["design-critique-runs"],
    queryFn: designCritiqueApi.listReviewRuns,
    enabled: typeof designCritiqueApi.listReviewRuns === "function",
  });
  const { data: projectContextData } = useQuery({
    queryKey: ["design-critique-project-contexts"],
    queryFn: designCritiqueApi.listProjectContexts,
    enabled: typeof designCritiqueApi.listProjectContexts === "function",
  });
  const { data: designRoundData } = useQuery({
    queryKey: ["design-critique-design-rounds"],
    queryFn: designCritiqueApi.listDesignRounds,
    enabled: typeof designCritiqueApi.listDesignRounds === "function",
  });
  const agents = agentList?.agents || [];
  const projectContexts = projectContextData?.contexts || [];
  const activeAgent =
    agents.length && !agents.some((agent) => agent.name === selectedAgent)
      ? DEFAULT_AGENT
      : selectedAgent;

  useEffect(() => {
    saveSelectedAgent(activeAgent);
  }, [activeAgent]);
  useEffect(() => {
    saveReviewBrief(reviewBrief);
  }, [reviewBrief]);

  const updateReviewBrief = (patch: Partial<ReviewBrief>) => {
    setReviewBrief((current) => ({ ...current, ...patch }));
  };

  const selectProjectContext = (contextId: string) => {
    const context = projectContexts.find((entry) => entry.id === contextId);
    if (!context) {
      updateReviewBrief({ contextId: "" });
      return;
    }
    setReviewBrief((current) => ({
      ...current,
      contextId: context.id,
      projectName: context.name,
      repository: context.repository,
      contextPaths: context.context_paths.join("\n"),
      notes: context.notes,
    }));
  };

  const saveProjectContext = async () => {
    if (contextBusy) return;
    const input = {
      name: reviewBrief.projectName.trim(),
      repository: reviewBrief.repository.trim(),
      context_paths: reviewBrief.contextPaths
        .split("\n")
        .map((path) => path.trim())
        .filter(Boolean),
      notes: reviewBrief.notes.trim(),
    };
    setContextBusy(true);
    try {
      const response = reviewBrief.contextId
        ? await designCritiqueApi.updateProjectContext(
            reviewBrief.contextId,
            input,
          )
        : await designCritiqueApi.createProjectContext(input);
      await queryClient.invalidateQueries({
        queryKey: ["design-critique-project-contexts"],
      });
      updateReviewBrief({ contextId: response.context.id });
      setErr("");
    } catch (error) {
      setErr(
        error instanceof Error
          ? error.message
          : i18nT(
              "apps.designCritique.scopeBuilder.project_context_request_failed",
            ),
      );
    } finally {
      setContextBusy(false);
    }
  };

  const deleteProjectContext = async () => {
    if (!reviewBrief.contextId || contextBusy) return;
    setContextBusy(true);
    try {
      await designCritiqueApi.deleteProjectContext(reviewBrief.contextId);
      await queryClient.invalidateQueries({
        queryKey: ["design-critique-project-contexts"],
      });
      updateReviewBrief({ contextId: "" });
      setErr("");
    } catch (error) {
      setErr(
        error instanceof Error
          ? error.message
          : i18nT(
              "apps.designCritique.scopeBuilder.project_context_request_failed",
            ),
      );
    } finally {
      setContextBusy(false);
    }
  };

  const briefForRun = (scopeNote = ""): ReviewBrief => ({
    ...reviewBrief,
    notes: [reviewBrief.notes, scopeNote].filter(Boolean).join("\n"),
  });

  const prepareDesignRound = async (input: {
    mode: "generate-design" | "generate-prototype";
    target: string;
    claude_design_url: string;
    handoff_path: string;
  }) => {
    if (!current?.report || designRoundBusy) return;
    setDesignRoundBusy(true);
    try {
      await designCritiqueApi.createDesignRound({
        ...input,
        intent: reviewBrief.intent,
        project_name: reviewBrief.projectName,
        repository: reviewBrief.repository,
        context_paths: reviewBrief.contextPaths,
        notes: reviewBrief.notes,
        review_run_id: slotRunId,
        report: current.report,
      });
      await queryClient.invalidateQueries({
        queryKey: ["design-critique-design-rounds"],
      });
      setErr("");
    } catch (error) {
      setErr(
        error instanceof Error
          ? error.message
          : i18nT("designSync.prepare_failed"),
      );
    } finally {
      setDesignRoundBusy(false);
    }
  };

  const updateDesignRound = async (
    roundId: string,
    update: Partial<DesignRound>,
  ) => {
    if (designRoundBusy) return;
    setDesignRoundBusy(true);
    try {
      await designCritiqueApi.updateDesignRound(roundId, update);
      await queryClient.invalidateQueries({
        queryKey: ["design-critique-design-rounds"],
      });
      setErr("");
    } catch (error) {
      setErr(
        error instanceof Error
          ? error.message
          : i18nT("designSync.update_failed"),
      );
    } finally {
      setDesignRoundBusy(false);
    }
  };

  useEffect(() => {
    const restored = (reviewRunData?.runs || []).filter(
      (run) => run.status === "completed" && run.report,
    );
    if (!restored.length) return;
    const current = loadHistory();
    const next = restored.reduce<HistoryEntry[]>((entries, run) => {
      if (entries.some((entry) => entry.slotKey === run.slot_key))
        return entries;
      const screens = Array.isArray(run.screens)
        ? run.screens.filter(
            (screen): screen is Screen =>
              !!screen &&
              typeof screen.url === "string" &&
              typeof screen.label === "string" &&
              typeof screen.step === "number",
          )
        : [];
      const report = normalizeReport(run.report) || run.report!;
      return [
        {
          id: run.created_at,
          ts: run.created_at,
          slotKey: run.slot_key,
          runId: run.id,
          screens,
          thumbUrl: screens[0]?.url || "",
          read: report.overallRead || "",
          report,
        },
        ...entries,
      ];
    }, current);
    if (next.length !== current.length) {
      saveHistory(next);
      setCritiques(next);
    }
  }, [reviewRunData]);

  const inputRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const rowRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const startedAtRef = useRef(0);
  const aliveRef = useRef(true);
  const phaseRef = useRef<Phase>("new");
  const activeSlotRef = useRef("");
  /**
   * Slots whose run the user cancelled. Keyed per slot because more than one
   * critique can be in flight: a single boolean would trip for every poller, so
   * cancelling one run would exit the others and discard their results.
   */
  const cancelledRef = useRef<Set<string>>(new Set());
  /**
   * Bumped whenever the user starts or resets a run, so an async step that began
   * earlier can tell it no longer owns the screen. An upload has no slot yet —
   * the slot is created after it completes — so `activeSlotRef` cannot identify
   * it and `isWatching()` has nothing to compare; this counter is what protects
   * the pre-slot window.
   */
  const runSeqRef = useRef(0);
  /** Slots that already have a live poller, so a run is never polled twice. */
  const pollingRef = useRef<Set<string>>(new Set());
  const isCancelled = (slotKey: string): boolean =>
    cancelledRef.current.has(slotKey);
  const askSlotRef = useRef("");
  const askAgentRef = useRef(DEFAULT_AGENT);
  /**
   * Follow-ups deliberately share ONE chat slot so the thread keeps its context.
   * That makes them strictly sequential: `/api/chat` queues a second turn on the
   * same slot and `pollForText` only ever reads the latest assistant message, so
   * two turns in flight both resolve to the second answer and the first question
   * is recorded with a reply that does not belong to it.
   */
  const [askPending, setAskPending] = useState(false)
  const threadRef = useRef<HTMLDivElement>(null)
  /** The open thread, read when a turn fails after an await. The composer
   *  draft is shared by every thread, so a failed question goes back into it
   *  only while its own thread is still the one on screen. */
  const openAskIdRef = useRef(openAskId)
  openAskIdRef.current = openAskId

  const narrow = useNarrow(rootRef);

  // Must re-arm on setup, not only clear on teardown: StrictMode mounts,
  // unmounts and remounts effects, so a teardown-only version would leave
  // aliveRef false forever and every poll would cancel itself.
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);
  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);

  // Real elapsed time while a run is in flight. Derived from a START TIMESTAMP, not
  // by counting ticks — counting is wrong if two intervals ever overlap.
  useEffect(() => {
    if (phase !== "uploading" && phase !== "analyzing" && phase !== "scanning")
      return;
    const tick = () => {
      const t0 = startedAtRef.current;
      setElapsed(t0 ? Math.max(0, Math.floor((Date.now() - t0) / 1000)) : 0);
    };
    tick();
    const id = setInterval(tick, 500);
    return () => clearInterval(id);
  }, [phase]);

  useEffect(() => {
    if (!zoom) return;
    const k = (e: KeyboardEvent) => {
      if (e.key === "Escape") setZoom(false);
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [zoom]);

  // ── poll loops ──────────────────────────────────────────────────────────
  // Ends on EVIDENCE, not a clock; the runaway backstop is measured against
  // Date.now(), never a loop count. Bails the moment this view goes away.
  const pollForReport = async <T,>(slotKey: string): Promise<T> => {
    const began = Date.now();
    let misses = 0;
    while (Date.now() - began < HARD_CAP_MS) {
      await new Promise((r) => setTimeout(r, 1500));
      if (!aliveRef.current || isCancelled(slotKey)) throw CANCELLED();
      let d;
      try {
        d = await designCritiqueApi.getSlot(slotKey);
      } catch {
        if (++misses >= 8)
          throw new Error(
            "That run is no longer available — start a new critique.",
          );
        continue;
      }
      misses = 0;
      if (!aliveRef.current || isCancelled(slotKey)) throw CANCELLED();
      const c = lastAssistant(d && d.messages);
      if (c && c.trim() && activeSlotRef.current === slotKey) setWriting(true);
      if (d && !d.running && c) {
        const p = jsonFromMessages<T>(d.messages, looksLikeReport);
        if (p) return p;
        throw new Error("The critic replied but not in a readable format.");
      }
    }
    throw TIMEOUT();
  };

  /** How many rows the slot holds right now -- the baseline a follow-up's poll
   *  measures new rows against. Read BEFORE the send goes out. */
  const slotLength = async (slotKey: string): Promise<number> => {
    const d = await designCritiqueApi.getSlot(slotKey)
    return Array.isArray(d && d.messages) ? (d.messages as unknown[]).length : 0
  }

  // Follow-up answers are prose, not JSON — so this waits for text, not a schema.
  // Only an assistant row appended AFTER `seen` (the slot's length before the
  // send) counts as the answer: the thread shares one slot, so its previous
  // answer is always the latest assistant row, and a send whose POST never
  // landed (the wire reports a rejected fetch as indeterminate, not failed)
  // would otherwise be "answered" by it -- the previous reply persisted under
  // the new question. With no new row the loop runs to its timeout instead.
  const pollForText = async (slotKey: string, seen: number): Promise<string> => {
    const began = Date.now()
    let misses = 0
    while (Date.now() - began < 3 * 60 * 1000) {
      await new Promise(r => setTimeout(r, 1200))
      if (!aliveRef.current) throw CANCELLED()
      let d
      try { d = await designCritiqueApi.getSlot(slotKey) }
      catch { if (++misses >= 8) throw new Error('lost contact'); continue }
      misses = 0
      const rows: SlotData['messages'] = (d && Array.isArray(d.messages)) ? d.messages : []
      const c = lastAssistant((rows ?? []).slice(seen))
      if (d && !d.running && c && c.trim()) return c.trim()
    }
    throw TIMEOUT();
  };

  const startClock = (fromMs?: number) => {
    startedAtRef.current = fromMs || Date.now();
    setElapsed(0);
  };

  const openSlot = async (agent: string): Promise<string> => {
    const s = await designCritiqueApi.openSlot(agent);
    trackSlot(s.key);
    markLive(s.key);
    return s.key;
  };
  const send = (slotKey: string, agent: string, message: string) =>
    designCritiqueApi.send(slotKey, agent, message);
  const dropSlot = (slotKey: string) => {
    if (!slotKey) return;
    untrackSlot(slotKey);
    unmarkLive(slotKey);
    designCritiqueApi.deleteSlot(slotKey);
  };

  const createReviewRun = async (
    slotKey: string,
    agent: string,
    stage: string,
    source: Record<string, unknown>,
    screens: Screen[],
  ): Promise<string> => {
    if (typeof designCritiqueApi.createReviewRun !== "function") return "";
    const model =
      typeof designCritiqueApi.resolveAgentModel === "function"
        ? await designCritiqueApi
            .resolveAgentModel(agent)
            .then((result) => result.model)
            .catch(() => "")
        : "";
    const { run } = await designCritiqueApi.createReviewRun({
      slot_key: slotKey,
      agent,
      model,
      stage,
      source,
      screens,
    });
    return run.id;
  };

  const updateReviewRun = (
    runId: string | undefined,
    update: Partial<ReviewRun>,
  ): Promise<void> =>
    runId && typeof designCritiqueApi.updateReviewRun === "function"
      ? designCritiqueApi.updateReviewRun(runId, update).then(() => undefined)
      : Promise.resolve();

  // Fetch the critique method once and cache it; on failure the critique still
  // runs on the persona alone rather than blocking.
  const loadMethod = async (): Promise<string> => {
    if (methodRef.current) return methodRef.current;
    try {
      const m = await designCritiqueApi.method();
      // Inline only the checklist rubric — NOT the full skill, whose shell/fs_read
      // steps would contradict this tool-free critique path.
      methodRef.current = (m && m.checklist) || "";
    } catch {
      methodRef.current = "";
    }
    return methodRef.current;
  };

  const showReport = (raw: Report, screens: Screen[], entry?: HistoryEntry) => {
    // Also normalise on the way IN, not just on the way out of a run: entries
    // written to dc-history-v1 before this guard existed can still hold a
    // wrong-shaped field, and reopening one would otherwise crash on every visit.
    const report = normalizeReport(raw) || raw;
    setCurrent({
      report,
      screens: screens || [],
      entryId: entry ? entry.id : null,
    });
    const saved = (entry && Array.isArray(entry.asks) ? entry.asks : []).map(
      (a: Ask & { q?: string; a?: string; failed?: boolean }) =>
        a.turns
          ? a
          : {
              id: a.id,
              quote: a.quote,
              turns: [
                {
                  t: 0,
                  q: a.q || "",
                  a: a.a || "",
                  pending: false,
                  failed: !!a.failed,
                },
              ],
            },
    );
    setAsks(saved);
    setOpenAskId(null);
    setSel(null);
    setAskDraft("");
    if (askSlotRef.current) {
      dropSlot(askSlotRef.current);
      askSlotRef.current = "";
      askAgentRef.current = DEFAULT_AGENT;
    }
    setOpen(new Set([0]));
    setActive(null);
    setZoom(false);
    setScreenIdx(0);
    setPhase("report");
  };

  /**
   * Clear the persisted job ONLY if it is this run's.
   *
   * One job record is persisted at a time, so an older run reaching its end must
   * not wipe a newer one's. Records are keyed by slot, so this removes exactly
   * this run's entry and leaves any other in-flight run resumable.
   */
  /**
   * End one run completely: forget its resume pointer, release its server slot,
   * and remove any pending row. Releasing the slot WITHOUT forgetting the job
   * leaves a reload able to resume a run whose slot no longer exists, which then
   * dies on an availability error. Every terminal path goes through here so the
   * two cannot drift apart.
   */
  const endRun = (slotKey: string) => {
    // An empty key would reach clearJob()'s clear-all branch and delete every
    // persisted run, so a failure before the slot exists is a no-op.
    if (!slotKey) return;
    clearJob(slotKey);
    dropSlot(slotKey);
    setCritiques(dropPendingCritique(slotKey));
  };

  /**
   * Is the user currently watching this run? Both the run slot AND a waiting
   * phase must match: more than one critique can be in flight, so a run that
   * has been backgrounded by `+ New` must never write to the foreground.
   */
  const isWatching = (slotKey: string): boolean =>
    activeSlotRef.current === slotKey &&
    (phaseRef.current === "analyzing" ||
      phaseRef.current === "uploading" ||
      phaseRef.current === "scanning");

  const finishReport = async (
    slotKey: string,
    uploaded: Screen[],
    raw: Report,
    runId?: string,
  ) => {
    // Normalise the model's JSON once, here, before it reaches history or render.
    // Every caller funnels through this, so no render site can be handed a
    // non-array for keep / couldNotSee / findings / steps / rules.
    const rep = normalizeReport(raw) || raw;
    const screens = resolveScreens(rep, uploaded);
    await updateReviewRun(runId, {
      status: "completed",
      stage: "report",
      screens,
      report: rep,
      error: null,
    });
    const cur = loadHistory();
    let next = cur;
    const at = cur.findIndex((e) => e.slotKey === slotKey);
    const thumbUrl = screens[0] ? screens[0].url : "";
    const read = rep.overallRead || "";
    if (at < 0) {
      next = [
        {
          id: Date.now(),
          ts: Date.now(),
          slotKey,
          runId,
          screens,
          thumbUrl,
          read,
          report: rep,
        },
        ...cur,
      ];
      saveHistory(next);
    } else if (cur[at].pending) {
      // Backgrounded run finishing: fill the placeholder rather than adding a
      // second row, and keep its id and position so the list does not reshuffle.
      next = cur.slice();
      next[at] = {
        ...cur[at],
        runId,
        screens,
        thumbUrl,
        read,
        report: rep,
        pending: false,
      };
      saveHistory(next);
    }
    endRun(slotKey);
    setCritiques(next);
    // Take over the screen only if the user is waiting for THIS run. Testing the
    // phase alone is not enough: starting a second critique puts the phase back
    // to 'analyzing', so the first run would read that as "still waiting for me"
    // and replace the second run's in-progress view with its own report. Any run
    // the user is no longer watching announces itself with the ready chip instead.
    const waiting = isWatching(slotKey);
    if (waiting)
      showReport(
        rep,
        screens,
        next.find((e) => e.slotKey === slotKey) || next[0],
      );
    else
      setJustFinished({
        slotKey,
        read: rep.overallRead || i18nT("apps.designCritique.designCritiquePage.critique_ready"),
        screens,
        report: rep,
      });
  };

  const failWith = async (e: unknown, slotKey: string, runId?: string) => {
    const flag = e as Flagged;
    if (flag && flag.cancelled) return;
    // Same rule as finishReport: only the run on screen may change the screen.
    // A background run that fails or times out reports through the notification,
    // never by dropping the foreground run into an error state.
    const watching = isWatching(slotKey);
    if (flag && flag.timeout) {
      if (watching) {
        setHint(
          i18nT(
            "apps.designCritique.designCritiquePage.still_working_on_this_one_it_s_kept_running_come",
          ),
        );
        setPhase("error");
      }
      return;
    }
    await updateReviewRun(runId, {
      status: "failed",
      error: { code: e instanceof Error ? e.message : "unknown_failure" },
    }).catch(() => undefined);
    endRun(slotKey);
    setCritiques(dropPendingCritique(slotKey));
    const message =
      e instanceof Error
        ? e.message
        : i18nT("apps.designCritique.designCritiquePage.something_went_wrong");
    if (watching) {
      setErr(message);
      setPhase("error");
    }
    // The toast is transient feedback; the failed state itself is rendered
    // in-page (the rail notice) so it is not lost when the toast fades.
    else
      setBackgroundFailures((prev) => [
        ...prev.filter((f) => f.slotKey !== slotKey),
        { slotKey, message },
      ]);
    notify(i18nT("designSync.critique_failed", { error: e instanceof Error ? e.message : String(e) }), {
      type: "error",
    });
  };

  const ask = async (prompt: string, uploaded: Screen[], foreground = true) => {
    let slotKey = "";
    let runId = "";
    const agent = activeAgent;
    try {
      slotKey = await openSlot(agent);
      runId = await createReviewRun(
        slotKey,
        agent,
        "analyzing",
        { kind: "screenshots", brief: briefForRun() },
        uploaded,
      );
      // Claiming activeSlotRef is what makes a run the foreground one, so a
      // superseded upload must not claim it — it would redirect every guard that
      // keys off the active slot to a run the user is no longer looking at.
      if (foreground) {
        activeSlotRef.current = slotKey;
        setSlotRunId(runId);
      } else setCritiques(beginPendingCritique(slotKey, uploaded || []));
      saveJob({
        stage: "analyzing",
        slotKey,
        runId,
        agent,
        screens: uploaded || [],
        ts: Date.now(),
      });
      await send(slotKey, agent, prompt);
      const rep = await pollForReport<Report>(slotKey);
      await finishReport(slotKey, uploaded, rep, runId);
    } catch (e) {
      await failWith(e, slotKey, runId);
    }
  };

  // One or many screenshots. Order is the order you gave them.
  const runImages = async (fileList: File[]) => {
    const files = Array.from(fileList || []).filter((f) =>
      /^image\//.test(f.type || ""),
    );
    if (!files.length) {
      setHint(
        i18nT(
          "apps.designCritique.designCritiquePage.those_weren_t_image_files",
        ),
      );
      setPhase("error");
      return;
    }
    if (files.length > 20) {
      setHint(
        i18nT(
          "apps.designCritique.designCritiquePage.that_s_more_than_20_screens_send_fewer",
        ),
      );
      setPhase("error");
      return;
    }
    const seq = ++runSeqRef.current;
    setErr("");
    setHint("");
    setBlocked(null);
    setShowAuth(false);
    setMenuOpen(false);
    startClock();
    setWriting(false);
    setPendingKind(null);
    setPhase("uploading");
    try {
      const { paths } = await designCritiqueApi.uploadFiles(files);
      if (!paths || !paths.length) throw new Error("no file paths returned");
      const uploaded = paths.map((p, i) => ({
        step: i + 1,
        label: i18nT("designSync.screen_number", { value: fmtNumber(i + 1) }),
        url: fileUrl(p),
      }));
      // Do the slow method fetch BEFORE deciding ownership, then re-check the run
      // sequence: if `+ New` ran while the upload/method-fetch was in flight, a
      // later run owns the screen — keep critiquing in the background rather than
      // stealing foreground ownership back (which would strand the current run).
      const method = await loadMethod();
      const mine = runSeqRef.current === seq;
      if (mine) {
        setCurrent({ report: null, screens: uploaded });
        setScreenIdx(0);
        setPhase("analyzing");
      }
      await ask(IMAGES_PROMPT(paths, briefForRun(), method), uploaded, mine);
    } catch (e) {
      // No slot exists yet at this point; ask() owns cleanup for the one it creates.
      if (runSeqRef.current === seq) {
        setErr(
          e instanceof Error
            ? e.message
            : i18nT(
                "apps.designCritique.designCritiquePage.something_went_wrong",
              ),
        );
        setPhase("error");
      }
    }
  };

  // Shared post-discovery handling: turn a discovery payload into the scoping
  // picker, or into a blocked / empty-result screen. Used by a fresh scan and by
  // a resumed one (reconnecting to a stored backend job).
  const applyDiscovery = (
    info: Scope & { handle?: string },
    det: { kind: string; value: string },
    jobKey: string,
  ) => {
    if (info.blocked && info.blocked.reason) {
      const d = info.blocked.detail;
      setBlocked({
        ...(BLOCKED[info.blocked.reason] || BLOCKED.other),
        detail: typeof d === "string" ? d : "",
      });
      clearJob(jobKey);
      setPhase("error");
      return;
    }
    const list = Array.isArray(info.screens)
      ? info.screens.filter((s) => s && s.id)
      : [];
    if (!list.length) {
      clearJob(jobKey);
      setErr(
        discoveryNote(info) ||
          i18nT(
            "apps.designCritique.designCritiquePage.i_got_in_but_there_s_nothing_in_there_i_can",
          ),
      );
      setPhase("error");
      return;
    }
    const norm = normalizeScope({ ...info, screens: list }) || {
      ...info,
      screens: list,
      flows: [],
    };
    setScope(norm);
    const first = norm.flows[0];
    const preset =
      first && first.screenIds && first.screenIds.length
        ? first.screenIds.filter((id) => list.some((s) => s.id === id))
        : list.filter((s) => s.canSee !== false).map((s) => s.id);
    setPicked(preset);
    setSlot(jobKey);
    setRefHandle(info.handle || "");
    saveJob({
      stage: "scoping",
      slotKey: jobKey,
      kind: det.kind,
      value: det.value,
      ts: Date.now(),
      scope: norm,
      picked: preset,
      handle: info.handle,
    });
    setPhase("scoping");
  };

  // A Figma link, a repo, or a local package. Step 1: the backend finds what's in
  // there — as a DETACHED server-side job, so navigating away no longer cancels
  // the scan. The backend job id is persisted the moment it exists, so a return
  // visit reconnects by polling it instead of starting a second scan.
  const runRef = async (raw: string) => {
    const det = detectKind(raw);
    if (!det) return;
    if (det.kind === "unknown") {
      setHint(
        i18nT(
          "apps.designCritique.designCritiquePage.couldn_t_tell_what_that_is_give_me_a_figma_link",
        ),
      );
      setPhase("error");
      return;
    }
    const seq = ++runSeqRef.current;
    const jobKey = "ref-" + Date.now();
    setErr("");
    setHint("");
    setBlocked(null);
    setShowAuth(false);
    setMenuOpen(false);
    startClock();
    setWriting(false);
    setPendingKind(det.kind);
    setCurrent({ report: null, screens: [] });
    setScreenIdx(0);
    setScope(null);
    setPicked([]);
    setRefHandle("");
    setRefTarget({ kind: det.kind, value: det.value });
    setSlot(jobKey);
    // No chat slot drives discovery any more, so nothing may claim the screen as
    // its foreground run while the backend works.
    activeSlotRef.current = "";
    setPhase("scanning");
    try {
      const info = await designCritiqueApi.discover(
        det.kind,
        det.value,
        (jobId) => {
          // Persist the moment the backend job exists, so a navigate-away resumes by
          // polling this id rather than re-POSTing (which would double-scan).
          saveJob({
            stage: "scanning",
            slotKey: jobKey,
            kind: det.kind,
            value: det.value,
            ts: Date.now(),
            discoverJob: jobId,
          });
        },
      );
      if (runSeqRef.current !== seq) return;
      applyDiscovery(info, det, jobKey);
    } catch (e) {
      if (runSeqRef.current !== seq) return;
      const flag = e as Flagged;
      if (flag && flag.cancelled) return;
      clearJob(jobKey);
      setErr(
        e instanceof Error
          ? e.message
          : i18nT(
              "apps.designCritique.designCritiquePage.that_scan_didn_t_finish",
            ),
      );
      setPhase("error");
    }
  };

  // Shared post-render handling: turn rendered PNGs into a tool-free critique on a
  // fresh chat slot. Used by a fresh scoped run and by a resumed one (reconnecting
  // to a stored backend render job). `seq` is undefined on resume, where the run
  // is always the foreground one.
  const critiqueRendered = async (
    out: {
      screens: Array<{ step: number; label: string; path: string }>;
      couldNotSee: string[];
    },
    jobKey: string,
    seq: number | undefined,
    brief: string,
  ) => {
    const rendered = Array.isArray(out.screens)
      ? out.screens.filter((s) => s && s.path)
      : [];
    if (!rendered.length) {
      if (jobKey) {
        clearJob(jobKey);
        dropSlot(jobKey);
      }
      setErr(
        i18nT(
          "apps.designCritique.designCritiquePage.that_critique_didn_t_finish",
        ),
      );
      setPhase("error");
      return;
    }
    // The scoping/render job is finished; the critique below opens its own slot + job.
    if (jobKey) {
      clearJob(jobKey);
      dropSlot(jobKey);
    }
    setSlot("");
    const paths = rendered.map((s) => s.path);
    const uploaded = rendered.map((s, i) => ({
      step: i + 1,
      label: s.label,
      url: fileUrl(s.path),
    }));
    const missed = Array.isArray(out.couldNotSee) ? out.couldNotSee : [];
    // Load the method BEFORE claiming ownership, then re-check the run sequence:
    // if `+ New` ran while scoping/method-fetch was in flight, a later run owns
    // the screen — keep critiquing in the background rather than stealing the
    // foreground back (which would strand the current run).
    const method = await loadMethod();
    const mine = seq === undefined || runSeqRef.current === seq;
    if (mine) {
      setCurrent({ report: null, screens: uploaded });
      setScreenIdx(0);
    }
    await ask(IMAGES_PROMPT(paths, brief, method, missed), uploaded, mine);
  };

  // Step 2: the backend renders the picked screens to PNGs, then the agent
  // critiques those finished images with no tools — the same tool-free path a
  // screenshot upload uses, so it can never stall on a tool-approval prompt. The
  // render is a DETACHED server-side job; its id is persisted so a navigate-away
  // reconnects by polling it rather than re-rendering.
  const runScoped = async () => {
    if (!scope || !picked.length || !refTarget) return;
    const byId = new Map(scope.screens.map((s) => [s.id, s]));
    const picks = picked
      .map((id) => byId.get(id))
      .filter(Boolean) as DiscoveryScreen[];
    const jobKey = slot;
    const seq = ++runSeqRef.current;
    const brief = refBrief;
    startClock();
    setWriting(false);
    setPhase("analyzing");
    try {
      const out = await designCritiqueApi.render(
        {
          kind: refTarget.kind,
          value: refTarget.value,
          handle: refHandle,
          picks: picks.map((p) => ({ id: p.id, label: p.label, ref: p.ref })),
        },
        (jobId) => {
          // Persist the render job so a navigate-away resumes by polling it.
          saveJob({
            stage: "rendering",
            slotKey: jobKey,
            kind: refTarget.kind,
            value: refTarget.value,
            ts: Date.now(),
            renderJob: jobId,
            scope,
            picked,
            refBrief: brief,
            handle: refHandle,
          });
        },
      );
      if (runSeqRef.current !== seq) return;
      await critiqueRendered(out, jobKey, seq, brief);
    } catch (e) {
      if (runSeqRef.current !== seq) return;
      const flag = e as Flagged;
      if (flag && flag.cancelled) return;
      if (jobKey) clearJob(jobKey);
      setErr(
        e instanceof Error
          ? e.message
          : i18nT(
              "apps.designCritique.designCritiquePage.something_went_wrong",
            ),
      );
      setPhase("error");
    }
  };

  // Reap slots we created and never cleaned up. Runs before resume so the live job is spared.
  useEffect(() => {
    // Spare every persisted job AND every run still in flight. Sparing only the
    // newest job record would delete a background critique's slot the moment a
    // second one started.
    const keep = new Set<string>(loadLive());
    for (const j of loadJobs()) if (j.slotKey) keep.add(j.slotKey);
    const strays = loadSlots().filter((k) => k && !keep.has(k));
    if (!strays.length) return;
    saveSlots([...keep]);
    for (const k of strays) designCritiqueApi.deleteSlot(k);
  }, []);

  // Resume whatever was in flight when the page went away.
  useEffect(() => {
    const all = loadJobs();
    const job = all[0];
    // Every OTHER persisted analyzing run gets a background poller so its result
    // is collected and written to history instead of being lost.
    for (const other of all.slice(1)) {
      if (other.stage === "analyzing") resumeAnalyzing(other);
    }
    if (!job || !job.slotKey) return;
    if (job.stage === "analyzing") pollingRef.current.add(job.slotKey);

    if (job.stage === "scoping" && job.scope) {
      setSlot(job.slotKey);
      setSlotRunId(job.runId || "");
      // A job persisted by an earlier build can hold an un-normalised scope, so
      // repair on read rather than trusting what localStorage happens to contain.
      setScope(normalizeScope(job.scope) || job.scope);
      setPicked(Array.isArray(job.picked) ? job.picked : []);
      setRefBrief(job.refBrief || "");
      setPendingKind(job.kind || null);
      setRefHandle(job.handle || "");
      if (job.kind && job.value)
        setRefTarget({ kind: job.kind, value: job.value });
      setCurrent({ report: null, screens: [] });
      setPhase("scoping");
      return;
    }

    if (job.stage === "scanning") {
      // Discovery runs as a detached backend job now. If the job id was persisted,
      // reconnect by POLLING it — the scan kept running server-side while the page
      // was away — rather than re-POSTing (which would start a second scan). A scan
      // persisted by an older build has no job id and cannot be resumed; drop it.
      if (job.discoverJob) {
        setSlot(job.slotKey);
        setPendingKind(job.kind || null);
        if (job.kind && job.value)
          setRefTarget({ kind: job.kind, value: job.value });
        setCurrent({ report: null, screens: [] });
        startClock(job.ts);
        setWriting(false);
        activeSlotRef.current = "";
        setPhase("scanning");
        (async () => {
          try {
            const info = await designCritiqueApi.pollDiscover(
              job.discoverJob as string,
            );
            applyDiscovery(
              info,
              { kind: job.kind || "", value: job.value || "" },
              job.slotKey,
            );
          } catch (e) {
            const flag = e as Flagged;
            if (flag && flag.cancelled) return;
            clearJob(job.slotKey);
            setErr(
              e instanceof Error
                ? e.message
                : i18nT(
                    "apps.designCritique.designCritiquePage.that_scan_didn_t_finish",
                  ),
            );
            setPhase("error");
          }
        })();
        return;
      }
      clearJob(job.slotKey);
      dropSlot(job.slotKey);
      return;
    }

    if (job.stage === "rendering" && job.renderJob) {
      // The render runs as a detached backend job; reconnect by polling it and
      // then critique the finished PNGs, exactly as a fresh scoped run does.
      setSlot(job.slotKey);
      if (job.scope) setScope(normalizeScope(job.scope) || job.scope);
      setPicked(Array.isArray(job.picked) ? job.picked : []);
      setRefBrief(job.refBrief || "");
      setPendingKind(job.kind || null);
      setRefHandle(job.handle || "");
      if (job.kind && job.value)
        setRefTarget({ kind: job.kind, value: job.value });
      setCurrent({ report: null, screens: [] });
      startClock(job.ts);
      setWriting(false);
      activeSlotRef.current = "";
      setPhase("analyzing");
      (async () => {
        try {
          const out = await designCritiqueApi.pollRender(
            job.renderJob as string,
          );
          await critiqueRendered(
            out,
            job.slotKey,
            undefined,
            job.refBrief || "",
          );
        } catch (e) {
          const flag = e as Flagged;
          if (flag && flag.cancelled) return;
          clearJob(job.slotKey);
          setErr(
            e instanceof Error
              ? e.message
              : i18nT(
                  "apps.designCritique.designCritiquePage.that_critique_didn_t_finish",
                ),
          );
          setPhase("error");
        }
      })();
      return;
    }

    const uploaded = Array.isArray(job.screens) ? job.screens : [];
    // Claim the slot: this branch shows the waiting screen for this run, so
    // isWatching() has to agree, or finishReport routes the result to the ready
    // chip and the foreground sits on "analyzing" for ever.
    activeSlotRef.current = job.slotKey;
    setSlot(job.slotKey);
    setSlotRunId(job.runId || "");
    setCurrent({ report: null, screens: uploaded });
    startClock(job.ts);
    setWriting(false);
    setPhase("analyzing");
    (async () => {
      try {
        const rep = await pollForReport<Report>(job.slotKey);
        await finishReport(job.slotKey, uploaded, rep, job.runId);
      } catch (e) {
        pollingRef.current.delete(job.slotKey);
        const flag = e as Flagged;
        if (flag && flag.cancelled) return;
        if (flag && flag.timeout) {
          setHint(
            i18nT(
              "apps.designCritique.designCritiquePage.still_working_on_this_one_it_s_kept_running_come_2",
            ),
          );
          setPhase("error");
          return;
        }
        await updateReviewRun(job.runId, {
          status: "failed",
          error: { code: e instanceof Error ? e.message : "unknown_failure" },
        }).catch(() => undefined);
        endRun(job.slotKey);
        setErr(
          e instanceof Error
            ? e.message
            : i18nT(
                "apps.designCritique.designCritiquePage.that_critique_didn_t_finish",
              ),
        );
        setPhase("error");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Follow the conversation: pin the thread to the newest turn as it arrives.
  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [asks, openAskId]);

  // Keep the persisted pick in sync while you're deciding, so a reorder isn't lost.
  useEffect(() => {
    if (phase !== "scoping" || !scope || !slot) return;
    saveJob({
      stage: "scoping",
      slotKey: slot,
      kind: pendingKind,
      value: refTarget?.value,
      ts: Date.now(),
      scope,
      picked,
      refBrief,
      handle: refHandle,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [picked, refBrief, phase]);

  const busy =
    phase === "uploading" || phase === "analyzing" || phase === "scanning";

  const report = current?.report ?? null;
  const screens = (current && current.screens) || [];
  const isFlow = screens.length > 1;
  const shown =
    screens[Math.min(screenIdx, Math.max(0, screens.length - 1))] || null;
  const thumbUrl = shown ? shown.url : "";

  // ── staging ────────────────────────────────────────────────────────────
  const addFiles = (fileList: FileList | File[] | null) => {
    const imgs = Array.from(fileList || []).filter((f) =>
      /^image\//.test(f.type || ""),
    );
    if (!imgs.length) {
      setHint(
        i18nT(
          "apps.designCritique.designCritiquePage.those_weren_t_image_files",
        ),
      );
      return;
    }
    setErr("");
    setHint("");
    setStaged((prev) => {
      const room = MAX_SCREENS - prev.length;
      if (room <= 0) {
        setHint(
          i18nT(
            "apps.designCritique.designCritiquePage.that_s_the_limit_of_max_screens",
            { max: MAX_SCREENS },
          ),
        );
        return prev;
      }
      if (imgs.length > room)
        setHint(
          i18nT(
            "apps.designCritique.designCritiquePage.only_added_the_first_room_the_limit_is_max_screens",
            { room, max: MAX_SCREENS },
          ),
        );
      return prev.concat(
        imgs.slice(0, room).map((f) => ({
          id:
            f.name +
            ":" +
            f.size +
            ":" +
            Math.random().toString(36).slice(2, 7),
          file: f,
          url: URL.createObjectURL(f),
        })),
      );
    });
    if (phase === "error") setPhase("new");
  };
  const dropStaged = (i: number) =>
    setStaged((prev) => {
      const next = prev.slice();
      const [gone] = next.splice(i, 1);
      if (gone) {
        try {
          URL.revokeObjectURL(gone.url);
        } catch {
          /* ignore */
        }
      }
      return next;
    });
  const moveStaged = (i: number, dir: number) =>
    setStaged((prev) => {
      const j = i + dir;
      if (j < 0 || j >= prev.length) return prev;
      const next = prev.slice();
      const t = next[i];
      next[i] = next[j];
      next[j] = t;
      return next;
    });
  /**
   * Attach a poller to a run that is persisted but unattended. Resume attaches a
   * poller for EVERY analyzing job, not just the newest record, so a critique
   * backgrounded with `+ New` is collected after a page revisit instead of
   * showing a spinner nothing is driving.
   */
  const resumeAnalyzing = (job: Job): void => {
    if (!job.slotKey || pollingRef.current.has(job.slotKey)) return;
    pollingRef.current.add(job.slotKey);
    void (async () => {
      const uploaded = Array.isArray(job.screens) ? job.screens : [];
      try {
        const rep = await pollForReport<Report>(job.slotKey);
        await finishReport(job.slotKey, uploaded, rep, job.runId);
      } catch (e) {
        await failWith(e, job.slotKey, job.runId);
      } finally {
        pollingRef.current.delete(job.slotKey);
      }
    })();
  };

  /**
   * The "nothing renderable in there" copy, read through normalizeScope so the
   * model's `cannotSee` / `note` cannot put an object into an error string.
   */
  const discoveryNote = (info: Partial<Scope>): string => {
    const sc = normalizeScope(info);
    if (!sc) return "";
    return (
      (sc.cannotSee && sc.cannotSee.length ? sc.cannotSee.join(" ") : "") ||
      sc.note ||
      ""
    );
  };

  const clearStaged = () =>
    setStaged((prev) => {
      prev.forEach((s) => {
        try {
          URL.revokeObjectURL(s.url);
        } catch {
          /* ignore */
        }
      });
      return [];
    });

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (!busy) addFiles(e.dataTransfer && e.dataTransfer.files);
  };
  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    if (!dragging) setDragging(true);
  };
  const onDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
  };
  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    addFiles(e.target.files);
    e.target.value = "";
  };
  const pickFile = () => {
    if (inputRef.current) inputRef.current.click();
  };

  const canStart = !busy && (staged.length > 0 || !!refText.trim());
  const start = () => {
    if (!canStart) return;
    if (staged.length) {
      const files = staged.map((s) => s.file);
      clearStaged();
      runImages(files);
    } else runRef(refText);
  };

  // ── highlight & ask ──────────────────────────────────────────────────────
  const captureSelection = () => {
    const w = window.getSelection && window.getSelection();
    if (!w || w.isCollapsed || !w.rangeCount) {
      setSel(null);
      return;
    }
    const quote = String(w.toString()).replace(/\s+/g, " ").trim();
    if (quote.length < 4 || quote.length > 320) {
      setSel(null);
      return;
    }
    const r = w.getRangeAt(0).getBoundingClientRect();
    if (!r || (!r.width && !r.height)) {
      setSel(null);
      return;
    }
    setSel({ quote, top: r.top, left: r.left + r.width / 2 });
  };

  const persistAsks = (list: Ask[]) => {
    const key = current && current.entryId;
    if (!key) return;
    const next = loadHistory().map((e) =>
      e.id === key ? { ...e, asks: list } : e,
    );
    saveHistory(next);
    setCritiques(next);
  };

  const runTurn = async (
    askId: string,
    question: string,
    seedWith: string | null,
  ) => {
    if (askPending) return;
    setAskPending(true);
    const stamp = Date.now();
    setAsks((prev) =>
      prev.map((a) =>
        a.id === askId
          ? {
              ...a,
              turns: a.turns.concat([
                { t: stamp, q: question || "", a: "", pending: true },
              ]),
            }
          : a,
      ),
    );
    try {
      if (!askSlotRef.current) {
        const agent = activeAgent;
        const k = await openSlot(agent);
        askSlotRef.current = k;
        askAgentRef.current = agent;
        await send(k, agent, ASK_CONTEXT(report as Report, screens));
        await pollForText(k, 0).catch(() => {});
      }
      const seen = await slotLength(askSlotRef.current)
      await send(
        askSlotRef.current,
        askAgentRef.current,
        seedWith
          ? ASK_PROMPT(seedWith, question)
          : ASK_FOLLOW_UP(question),
      );
      const text = await pollForText(askSlotRef.current, seen);
      setAsks((prev) => {
        const next = prev.map((a) =>
          a.id === askId
            ? {
                ...a,
                turns: a.turns.map((t) =>
                  t.t === stamp ? { ...t, a: text, pending: false } : t,
                ),
              }
            : a,
        );
        persistAsks(next);
        return next;
      });
    } catch (e) {
      const flag = e as Flagged
      if (flag && flag.cancelled) return
      const msg = flag && flag.timeout ? i18nT("designSync.ask_timeout")
        : (e instanceof Error) ? e.message : i18nT("apps.designCritique.designCritiquePage.something_went_wrong")
      setAsks(prev => prev.map(a => a.id === askId
        ? { ...a, turns: a.turns.map(t => (t.t === stamp ? { ...t, a: msg, pending: false, failed: true } : t)) }
        : a))
      // Hand the question back so a retry is one Enter, not a retype. Never
      // over a draft the user has already started.
      if (openAskIdRef.current === askId) setAskDraft(d => d || question)
    } finally {
      // Every exit path, including the early cancelled return above, or the next
      // follow-up would be refused for the rest of the session.
      setAskPending(false);
    }
  };

  const askAbout = (quote: string, question: string) => {
    const id = "a" + Date.now();
    setAsks((prev) => prev.concat([{ id, quote, turns: [] }]));
    setOpenAskId(id);
    setSel(null);
    setAskDraft("");
    runTurn(id, question, quote);
  };
  const askFollowUp = (askId: string, question: string) => {
    if (!question || !question.trim()) return;
    setAskDraft("");
    runTurn(askId, question.trim(), null);
  };
  const removeAsk = (id: string) => {
    const next = asks.filter((a) => a.id !== id);
    setAsks(next);
    persistAsks(next);
    if (openAskId === id) setOpenAskId(null);
  };

  const sendScreenshots = () => {
    setPhase("new");
    setCurrent(null);
    setScope(null);
    setPicked([]);
    setErr("");
    setHint("");
    setRefText("");
    setTimeout(() => {
      if (inputRef.current) inputRef.current.click();
    }, 0);
  };
  const critiqueRunning = () => {
    setPhase("new");
    setCurrent(null);
    setScope(null);
    setPicked([]);
    setErr("");
    setHint("");
    setRefText("http://localhost:");
  };

  const cancelRun = async () => {
    // Supersede any in-flight run: the scan/render polls live inside the api call
    // and have no abort hook, so bumping the sequence is what makes their (later)
    // result be discarded when it resolves. The backend job keeps running detached
    // and is simply ignored — nothing here can cancel it, which is the point.
    runSeqRef.current++;
    if (askSlotRef.current) {
      dropSlot(askSlotRef.current);
      askSlotRef.current = "";
      askAgentRef.current = DEFAULT_AGENT;
    }
    const k = activeSlotRef.current || slot;
    // Cancel THIS run only, and clear only its job record: a bare clearJob()
    // removes every persisted run, which would discard a concurrent critique.
    if (k) {
      void updateReviewRun(slotRunId, {
        status: "interrupted",
        error: { code: "cancelled" },
      }).catch(() => undefined);
      cancelledRef.current.add(k);
      endRun(k);
    }
    activeSlotRef.current = "";
    setSlot("");
    setSlotRunId("");
    setScope(null);
    setPicked([]);
    setJustFinished(null);
    setPhase("new");
    setCurrent(null);
    setErr("");
    setHint("");
    setWriting(false);
    setPendingKind(null);
    startedAtRef.current = 0;
    setElapsed(0);
    // Release this slot's flag once its poller has certainly observed it. Keyed
    // per slot so the timer cannot un-cancel a different run.
    if (k)
      setTimeout(() => {
        cancelledRef.current.delete(k);
      }, 2500);
  };

  const newCritique = () => {
    // 'scanning' is deliberately NOT here. A scan ends at a scope decision the
    // user has to make, so it cannot finish in the background: leaving it running
    // would let its completion write setScope/setPhase over a newer run, and the
    // pending row it earned could never resolve on its own. Treating it as
    // not-running routes it into the cleanup below, which ends it.
    const running = phase === "analyzing" || phase === "uploading";
    // Supersede anything mid-upload / mid-scan / mid-render: the scan and render
    // polls have no abort hook (the backend job runs detached), so this counter is
    // how a superseded run learns it no longer owns the screen and discards its
    // eventual result.
    runSeqRef.current++;
    clearStaged();
    if (running) {
      const k = activeSlotRef.current;
      if (k) {
        // A real critique slot is in flight: keep it and show a pending row that
        // its own poller will fill in when it finishes.
        setCritiques(
          beginPendingCritique(k, (current && current.screens) || []),
        );
      } else if (slot) {
        // Ref discovery/render phase: the synthetic scoping key has no poller, so a
        // pending row could never resolve (a permanent-pending run). The sequence
        // bump above already orphaned the backend poll — just drop the scoping state.
        clearJob(slot);
        setSlot("");
        setScope(null);
        setPicked([]);
        setRefBrief("");
      }
    }
    // Clear the job record too. Dropping only the slot would leave the run
    // persisted, so a reload would resume a critique the user had explicitly
    // started over from.
    // Marking it cancelled makes an in-flight scan's poller exit on its next
    // check instead of grinding through eight failed polls against a dead slot.
    if (!running && slot) {
      cancelledRef.current.add(slot);
      setTimeout(() => {
        cancelledRef.current.delete(slot);
      }, 2500);
      endRun(slot);
    }
    if (!running) {
      setSlot("");
      setScope(null);
      setPicked([]);
      setRefBrief("");
    }
    setPhase("new");
    setCurrent(null);
    setMenuOpen(false);
    setErr("");
    setHint("");
    setBlocked(null);
    setRefText("");
    startedAtRef.current = 0;
    setElapsed(0);
    setWriting(false);
    setPendingKind(null);
  };
  const openExample = () => {
    setMenuOpen(false);
    showReport(SAMPLE_REPORT, SAMPLE_SCREENS);
  };
  /** Re-attach the view to a run that is still going. */
  const watchRun = (e: HistoryEntry) => {
    setMenuOpen(false);
    activeSlotRef.current = e.slotKey;
    setSlot(e.slotKey);
    setCurrent({ report: null, screens: e.screens || [] });
    setJustFinished(null);
    setErr("");
    setHint("");
    setBlocked(null);
    const job = loadJobs().find((j) => j.slotKey === e.slotKey);
    startClock(job && job.ts ? job.ts : e.ts);
    setPhase(e.screens && e.screens.length ? "analyzing" : "scanning");
    // Selecting a pending row only changes UI state, so a run with no poller
    // would sit at "analyzing" forever. Attach one if nothing is driving it.
    if (job && job.stage === "analyzing") resumeAnalyzing(job);
    else if (!job)
      resumeAnalyzing({
        stage: "analyzing",
        slotKey: e.slotKey,
        screens: e.screens || [],
        ts: e.ts,
      });
  };

  const selectCritique = (e: HistoryEntry) => {
    if (e.pending) {
      watchRun(e);
      return;
    }
    if (!e.report) return;
    setMenuOpen(false);
    setSlotRunId(e.runId || "");
    const scr =
      e.screens && e.screens.length
        ? e.screens
        : e.thumbUrl
          ? [{ step: 1, label: i18nT("designSync.screen_number", { value: fmtNumber(1) }), url: e.thumbUrl }]
          : [];
    showReport(e.report, scr, e);
  };
  const toggle = (i: number) =>
    setOpen((prev) => {
      const n = new Set(prev);
      if (n.has(i)) n.delete(i);
      else n.add(i);
      return n;
    });
  const jumpTo = (i: number) => {
    setActive(i);
    setOpen((prev) => new Set(prev).add(i));
    const el = rowRefs.current[i];
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  // scoping picker handlers
  const togglePick = (id: string) =>
    setPicked((p) =>
      p.includes(id) ? p.filter((x) => x !== id) : p.concat([id]),
    );
  const dropPickAt = (id: string, overId: string) =>
    setPicked((p) => {
      const from = p.indexOf(id),
        to = p.indexOf(overId);
      if (from < 0 || to < 0 || from === to) return p;
      const n = p.slice();
      n.splice(from, 1);
      n.splice(to, 0, id);
      return n;
    });
  const movePick = (id: string, dir: number) =>
    setPicked((p) => {
      const i = p.indexOf(id);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= p.length) return p;
      const n = p.slice();
      const t = n[i];
      n[i] = n[j];
      n[j] = t;
      return n;
    });
  const useFlow = (f: Flow) =>
    setPicked(
      (f.screenIds || []).filter((id) =>
        scope ? scope.screens.some((s) => s.id === id) : false,
      ),
    );

  return renderCritiquePresentation({
    report,
    isFlow,
    designRoundData,
    slotRunId,
    screens,
    setScreenIdx,
    shown,
    active,
    setActive,
    jumpTo,
    phase,
    newCritique,
    busy,
    setJustFinished,
    setPhase,
    pendingKind,
    reduceMotion,
    justFinished,
    showReport,
    critiques,
    setMenuOpen,
    menuOpen,
    selectCritique,
    asks,
    setOpenAskId,
    open,
    toggle,
    rowRefs,
    reviewBrief,
    designRoundBusy,
    prepareDesignRound,
    updateDesignRound,
    sendScreenshots,
    critiqueRunning,
    openExample,
    narrow,
    backgroundFailures,
    setBackgroundFailures,
    captureSelection,
    scope,
    picked,
    refBrief,
    dragId,
    togglePick,
    dropPickAt,
    movePick,
    useFlow,
    setDragId,
    setRefBrief,
    runScoped,
    elapsed,
    writing,
    cancelRun,
    thumbUrl,
    screenIdx,
    setZoom,
    staged,
    refText,
    dragging,
    blocked,
    showAuth,
    contextBusy,
    agents,
    agentList,
    activeAgent,
    setSelectedAgent,
    err,
    hint,
    inputRef,
    projectContexts,
    updateReviewBrief,
    selectProjectContext,
    saveProjectContext,
    deleteProjectContext,
    onPick,
    onDrop,
    onDragOver,
    onDragLeave,
    pickFile,
    dropStaged,
    moveStaged,
    clearStaged,
    start,
    setRefText,
    setBlocked,
    setShowAuth,
    runRef,
    zoom,
    rootRef,
    sel,
    openAskId,
    askDraft,
    threadRef,
    setSel,
    setAskDraft,
    askAbout,
    askFollowUp,
    removeAsk,
    askPending,
    toasts,
  });
}
