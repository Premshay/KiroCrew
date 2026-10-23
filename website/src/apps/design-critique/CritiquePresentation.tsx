import type { KiroCrewAgent } from "../../components/AgentSelector";
import type { AgentList } from "./api";
import type { ProjectContext, Toast } from "./types";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Image as ImageIcon,
  Maximize2,
  PencilRuler,
  Plus,
  Sparkle,
  Upload,
  X,
} from "lucide-react";
import { type ReactNode } from "react";

import Clickable from "../../components/Clickable";
import ErrorNotice from "../../components/ErrorNotice";
import AskLayer from "./AskLayer";
import Composer from "./Composer";
import {
  DEFAULT_AGENT,
  KIND_LABEL,
  sevOf
} from "./constants";
import DesignRoundPanel from "./DesignRoundPanel";
import FindingRow from "./FindingRow";
import { Spinner } from "./Motion";
import ScopingPicker from "./ScopingPicker";
import { S } from "./styles";
import type {
  Ask,
  Blocked,
  DesignRound,
  Finding,
  Flow,
  HistoryEntry,
  Phase,
  Report,
  ReviewBrief,
  Scope,
  Screen,
  Sel,
  StagedItem
} from "./types";
import {
  loadHistory,
  readableOn,
  relTime,
  shortLabel
} from "./utils";
import WaitingScreen from "./WaitingScreen";

import { i18nT } from "../../i18n/t";

type StateSetter<T> = React.Dispatch<React.SetStateAction<T>>;

interface FinishedCritique {
  slotKey: string;
  read: string;
  screens: Screen[];
  report: Report;
}

interface BackgroundFailure {
  slotKey: string;
  message: string;
}

interface CritiquePresentationProps {
  report: Report | null;
  isFlow: boolean;
  designRoundData: { rounds: DesignRound[]; } | undefined;
  slotRunId: string;
  screens: Screen[];
  setScreenIdx: StateSetter<number>;
  shown: Screen;
  active: number | null;
  setActive: StateSetter<number | null>;
  jumpTo: (i: number) => void;
  phase: Phase;
  newCritique: () => void;
  busy: boolean;
  setJustFinished: StateSetter<FinishedCritique | null>;
  setPhase: StateSetter<Phase>;
  pendingKind: string | null;
  reduceMotion: boolean;
  justFinished: FinishedCritique | null;
  showReport: (raw: Report, screens: Screen[], entry?: HistoryEntry) => void;
  critiques: HistoryEntry[];
  setMenuOpen: StateSetter<boolean>;
  menuOpen: boolean;
  selectCritique: (e: HistoryEntry) => void;
  asks: Ask[];
  setOpenAskId: StateSetter<string | null>;
  open: Set<number>;
  toggle: (i: number) => void;
  rowRefs: React.MutableRefObject<Record<number, HTMLDivElement | null>>;
  reviewBrief: ReviewBrief;
  designRoundBusy: boolean;
  prepareDesignRound: (input: {
    mode: "generate-design" | "generate-prototype";
    target: string;
    claude_design_url: string;
    handoff_path: string;
  }) => Promise<void>;
  updateDesignRound: (roundId: string, update: Partial<DesignRound>) => Promise<void>;
  sendScreenshots: () => void;
  critiqueRunning: () => void;
  openExample: () => void;
  narrow: boolean;
  backgroundFailures: BackgroundFailure[];
  setBackgroundFailures: StateSetter<BackgroundFailure[]>;
  captureSelection: () => void;
  scope: Scope | null;
  picked: string[];
  refBrief: string;
  dragId: string | null;
  togglePick: (id: string) => void;
  dropPickAt: (id: string, overId: string) => void;
  movePick: (id: string, dir: number) => void;
  useFlow: (f: Flow) => void;
  setDragId: StateSetter<string | null>;
  setRefBrief: StateSetter<string>;
  runScoped: () => Promise<void>;
  elapsed: number;
  writing: boolean;
  cancelRun: () => Promise<void>;
  thumbUrl: string;
  screenIdx: number;
  setZoom: StateSetter<boolean>;
  staged: StagedItem[];
  refText: string;
  dragging: boolean;
  blocked: Blocked | null;
  showAuth: boolean;
  contextBusy: boolean;
  agents: KiroCrewAgent[];
  agentList: AgentList | undefined;
  activeAgent: string;
  setSelectedAgent: StateSetter<string>;
  err: string;
  hint: string;
  inputRef: React.RefObject<HTMLInputElement>;
  projectContexts: ProjectContext[];
  updateReviewBrief: (patch: Partial<ReviewBrief>) => void;
  selectProjectContext: (contextId: string) => void;
  saveProjectContext: () => Promise<void>;
  deleteProjectContext: () => Promise<void>;
  onPick: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onDrop: (e: React.DragEvent) => void;
  onDragOver: (e: React.DragEvent) => void;
  onDragLeave: (e: React.DragEvent) => void;
  pickFile: () => void;
  dropStaged: (i: number) => void;
  moveStaged: (i: number, dir: number) => void;
  clearStaged: () => void;
  start: () => void;
  setRefText: StateSetter<string>;
  setBlocked: StateSetter<Blocked | null>;
  setShowAuth: StateSetter<boolean>;
  runRef: (raw: string) => Promise<void>;
  zoom: boolean;
  rootRef: React.MutableRefObject<HTMLDivElement | null>;
  sel: Sel | null;
  openAskId: string | null;
  askDraft: string;
  threadRef: React.RefObject<HTMLDivElement>;
  setSel: StateSetter<Sel | null>;
  setAskDraft: StateSetter<string>;
  askAbout: (quote: string, question: string) => void;
  askFollowUp: (askId: string, question: string) => void;
  removeAsk: (id: string) => void;
  askPending: boolean;
  toasts: Toast[];
}

/** Render the critique workspace; run lifecycle and persistence stay in the page. */
export function renderCritiquePresentation({
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
  toasts
}: CritiquePresentationProps) {
  // ── derived finding structures ────────────────────────────────────────────
  const bySev = (a: Finding, b: Finding) =>
    sevOf(a.severity).rank - sevOf(b.severity).rank;
  const all: Finding[] =
    report && Array.isArray(report.findings) ? report.findings : [];
  const stepOf = (f: Finding) =>
    Array.isArray(f.steps) && f.steps.length ? f.steps[0] : 1;
  const isFlowFinding = (f: Finding) =>
    f.scope === "flow" || (Array.isArray(f.steps) && f.steps.length > 1);
  const flowFindings = isFlow ? all.filter(isFlowFinding).sort(bySev) : [];
  const screenFindings = all
    .filter((f) => !isFlow || !isFlowFinding(f))
    .sort(bySev);
  const designRounds = (designRoundData?.rounds || []).filter(
    (designRound) => !!slotRunId && designRound.review_run_id === slotRunId,
  );

  const idxOf = new Map<Finding, number>();
  flowFindings.forEach((f, i) => idxOf.set(f, i));
  screenFindings.forEach((f, i) => idxOf.set(f, flowFindings.length + i));
  const pinNo = new Map<Finding, number>();
  screens.forEach((s) => {
    let n = 0;
    screenFindings
      .filter((f) => stepOf(f) === s.step)
      .forEach((f) => {
        n += 1;
        pinNo.set(f, n);
      });
  });
  if (!isFlow) screenFindings.forEach((f, i) => pinNo.set(f, i + 1));

  const stepRange = (f: Finding) => {
    // `steps` arrives from jsonFromMessages<Report>, which is an unchecked cast
    // over model output — a reply with "steps":"1" would otherwise reach .sort()
    // on a string and take the whole report down. Trust the shape only when it
    // really is an array.
    const st = Array.isArray(f.steps)
      ? f.steps.slice().sort((a, b) => a - b)
      : [];
    if (!st.length) return "flow";
    if (st.length === 1) return String(st[0]);
    if (st.length === 2 && st[1] === st[0] + 1) return st[0] + "→" + st[1];
    return st[0] + "–" + st[st.length - 1];
  };

  const showStepOf = (f: Finding) => {
    if (!isFlow) return;
    const i = screens.findIndex((s) => s.step === stepOf(f));
    if (i >= 0) setScreenIdx(i);
  };

  // Pins for the screen currently on the canvas only. Location pins, never rectangles.
  const buildMarkers = (interactive: boolean): ReactNode[] =>
    screenFindings
      .filter((f) => !isFlow || (shown && stepOf(f) === shown.step))
      .map((f) => {
        const b = f.box;
        if (!b || typeof b.x !== "number") return null;
        const i = idxOf.get(f)!;
        const s = sevOf(f.severity);
        const on = active === i;
        const cx = (b.x + (b.w || 0) / 2) * 100,
          cy = (b.y + (b.h || 0) / 2) * 100;
        return (
          // Role, tab stop, click and keydown are gated on the SAME `interactive`
          // flag: a pin on the report canvas is a complete button widget, and the
          // copy overlaid on the lightbox image carries hover styling only. The rule
          // cannot evaluate the ternary role, so it reads the widget as a bare span.
          // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- role="button" + tabIndex 0 + Enter/Space ship together on the branch that has the click
          <span
            key={"mk" + i}
            title={pinNo.get(f) + ". " + f.title}
            role={interactive ? "button" : undefined}
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- the tab stop exists only on the `interactive` branch, which is also the one that gets role="button"
            tabIndex={interactive ? 0 : undefined}
            aria-label={interactive ? pinNo.get(f) + ". " + f.title : undefined}
            onMouseEnter={() => setActive(i)}
            onMouseLeave={() => setActive(null)}
            onClick={
              interactive
                ? (e) => {
                    e.stopPropagation();
                    jumpTo(i);
                  }
                : undefined
            }
            onKeyDown={
              interactive
                ? (e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      e.stopPropagation();
                      jumpTo(i);
                    }
                  }
                : undefined
            }
            style={{
              ...S.pinMarker,
              left: cx + "%",
              top: cy + "%",
              background: s.color,
              color: readableOn(s.color),
              cursor: interactive ? "pointer" : "default",
              opacity: on ? 1 : 0.92,
              transform: "translate(-50%, -50%) scale(" + (on ? 1.25 : 1) + ")",
              boxShadow: on
                ? "0 0 0 3px rgba(0,0,0,.5), 0 0 0 11px color-mix(in srgb, " +
                  s.color +
                  " 22%, transparent)"
                : "0 0 0 2px rgba(0,0,0,.5)",
              zIndex: on ? 3 : 2,
            }}
          >
            {String(pinNo.get(f))}
          </span>
        );
      })
      .filter(Boolean);

  // ── rail header ──
  const t = report ? report.tally || {} : {};
  let chips: ReactNode[] = (
    ["catastrophe", "major", "minor", "cosmetic"] as const
  )
    .filter((k) => (t[k] || 0) > 0)
    .map((k) => {
      const s = sevOf(k);
      const Icon = s.icon;
      return (
        <span key={k} style={{ ...S.chip, color: s.color }}>
          <Icon size={12} />
          {(t[k] || 0) + " " + s.label.toLowerCase()}
        </span>
      );
    });
  if (report && !chips.length)
    chips = [
      <span key="ok" style={{ ...S.chip, color: "#3fae6b" }}>
        <Check size={12} />
        {i18nT("apps.designCritique.designCritiquePage.nothing_major")}
      </span>,
    ];

  const entryMeta = (e: HistoryEntry) => {
    const n = (e.screens && e.screens.length) || (e.thumbUrl ? 1 : 0);
    return (
      (n > 1 ? n + " screens · " : n === 1 ? "1 screen · " : "") + relTime(e.ts)
    );
  };

  const railHead = (
    <div style={S.railHead}>
      <div style={S.title}>
        <PencilRuler size={19} style={{ color: "var(--accent)" }} />
        {i18nT("apps.designCritique.designCritiquePage.design_critique")}
      </div>
      <div style={S.railCtrls}>
        {phase !== "new" ? (
          <button
            style={S.railBtn}
            onClick={newCritique}
            title={i18nT(
              "apps.designCritique.designCritiquePage.start_a_new_critique_anything_already_running_ke",
            )}
          >
            <Plus size={13} />
            {i18nT("apps.designCritique.designCritiquePage.new")}
          </button>
        ) : null}
        {busy ? (
          <button
            style={S.runChip}
            onClick={() => {
              setJustFinished(null);
              setPhase(
                pendingKind && !screens.length ? "scanning" : "analyzing",
              );
            }}
            title={i18nT(
              "apps.designCritique.designCritiquePage.a_critique_is_still_running_click_to_watch_it",
            )}
          >
            <Spinner size={12} reduceMotion={reduceMotion} />
            {i18nT("apps.designCritique.designCritiquePage.running")}
          </button>
        ) : null}
        {!busy && justFinished ? (
          <button
            style={{ ...S.runChip, ...S.readyChip }}
            onClick={() => {
              const h = loadHistory();
              const mine = h.find((e) => e.slotKey === justFinished.slotKey);
              if (mine)
                showReport(justFinished.report, justFinished.screens, mine);
              setJustFinished(null);
            }}
            title={justFinished.read}
          >
            <Check size={12} />
            {i18nT("apps.designCritique.designCritiquePage.critique_ready")}
          </button>
        ) : null}
        {phase !== "new" && critiques.length ? (
          <div style={{ position: "relative" }}>
            <button
              style={S.railBtn}
              onClick={() => setMenuOpen((o) => !o)}
              title={i18nT(
                "apps.designCritique.designCritiquePage.switch_critique",
              )}
            >
              {"History (" + critiques.length + ")"}
              <ChevronDown size={12} />
            </button>
            {menuOpen ? (
              <div style={S.menu}>
                {critiques.map((e) => (
                  <Clickable
                    key={e.id}
                    style={S.menuItem}
                    onClick={() => selectCritique(e)}
                    title={
                      e.pending
                        ? i18nT(
                            "apps.designCritique.designCritiquePage.a_critique_is_still_running_click_to_watch_it",
                          )
                        : undefined
                    }
                    aria-busy={e.pending || undefined}
                  >
                    {e.thumbUrl ? (
                      <img src={e.thumbUrl} style={S.menuThumb} alt="" />
                    ) : null}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div
                        style={{
                          ...S.menuRead,
                          ...(e.pending ? S.pendingRead : {}),
                        }}
                      >
                        {e.pending
                          ? i18nT(
                              "apps.designCritique.designCritiquePage.running",
                            )
                          : e.read || "Critique"}
                      </div>
                      <div style={S.menuTime}>{entryMeta(e)}</div>
                    </div>
                    {e.pending ? (
                      <Spinner size={12} reduceMotion={reduceMotion} />
                    ) : null}
                  </Clickable>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );

  // Splits a text run so previously-asked quotes render as clickable annotations.
  const withMarks = (text: string): ReactNode => {
    const str = String(text || "");
    if (!asks.length || !str) return str;
    const low = str.toLowerCase();
    const hits = asks
      .map((a) => ({ a, i: low.indexOf(a.quote.toLowerCase()) }))
      .filter((x) => x.i >= 0)
      .sort((x, y) => x.i - y.i);
    if (!hits.length) return str;
    const out: ReactNode[] = [];
    let at = 0,
      n = 0;
    for (const hit of hits) {
      if (hit.i < at) continue;
      if (hit.i > at) out.push(str.slice(at, hit.i));
      out.push(
        <span
          key={"mk" + n++}
          style={S.mark}
          role="button"
          tabIndex={0}
          title={i18nT(
            "apps.designCritique.designCritiquePage.your_question_click_to_see_the_answer",
          )}
          onClick={(e) => {
            e.stopPropagation();
            setOpenAskId(hit.a.id);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              e.stopPropagation();
              setOpenAskId(hit.a.id);
            }
          }}
        >
          {str.substr(hit.i, hit.a.quote.length)}
        </span>,
      );
      at = hit.i + hit.a.quote.length;
    }
    if (at < str.length) out.push(str.slice(at));
    return out;
  };

  const renderRow = (f: Finding) => {
    const i = idxOf.get(f)!;
    const flowScoped = isFlow && isFlowFinding(f);
    return (
      <FindingRow
        key={i}
        finding={f}
        index={i}
        isOpen={open.has(i)}
        isActive={active === i}
        flowScoped={flowScoped}
        pinNum={pinNo.get(f)}
        stepRange={stepRange(f)}
        withMarks={withMarks}
        onEnter={() => setActive(i)}
        onLeave={() => setActive(null)}
        onClick={() => {
          toggle(i);
          if (!flowScoped) showStepOf(f);
        }}
        rowRef={(el) => {
          rowRefs.current[i] = el;
        }}
      />
    );
  };

  // ── rail body ──
  let railBody: ReactNode;
  if (phase === "report" && report) {
    const tightenBlock: ReactNode[] = isFlow
      ? [
          flowFindings.length ? (
            <div key="flow">
              <div style={S.sectionH}>
                {i18nT(
                  "apps.designCritique.designCritiquePage.across_the_flow",
                )}
                <span style={{ fontWeight: 600, textTransform: "none" }}>
                  {"· " + flowFindings.length}
                </span>
              </div>
              {flowFindings.map(renderRow)}
            </div>
          ) : null,
          ...screens.map((sc) => {
            const mine = screenFindings.filter((f) => stepOf(f) === sc.step);
            const onScreen = shown && shown.step === sc.step;
            return (
              <div key={"st" + sc.step}>
                <Clickable
                  style={S.stepH}
                  onClick={() => setScreenIdx(screens.indexOf(sc))}
                  title={i18nT(
                    "apps.designCritique.designCritiquePage.show_this_screen",
                  )}
                >
                  <span>{"Step " + sc.step + " · "}</span>
                  <span style={S.stepName} title={sc.label}>
                    {shortLabel(sc.label)}
                  </span>
                  {onScreen ? (
                    <span style={{ ...S.stepCount, color: "var(--accent)" }}>
                      {i18nT(
                        "apps.designCritique.designCritiquePage.on_screen",
                      )}
                    </span>
                  ) : (
                    <span style={{ ...S.stepCount, color: "var(--muted)" }}>
                      {mine.length
                        ? mine.length +
                          (mine.length === 1 ? " finding" : " findings")
                        : "nothing flagged"}
                    </span>
                  )}
                </Clickable>
                {mine.map(renderRow)}
              </div>
            );
          }),
        ]
      : screenFindings.length
        ? [
            <div key="wf">
              <div style={S.sectionH}>
                {i18nT(
                  "apps.designCritique.designCritiquePage.what_i_d_tighten",
                )}
              </div>
              {screenFindings.map(renderRow)}
            </div>,
          ]
        : [];

    railBody = (
      <>
        <p key="read" style={S.readLine}>
          {withMarks(report.overallRead || "Design critique")}
        </p>
        {report.health || isFlow ? (
          <div key="hh" style={S.health}>
            {[report.health, isFlow ? " · " + screens.length + " screens" : ""]
              .filter(Boolean)
              .join("")}
          </div>
        ) : null}
        <div key="chips" style={S.chips}>
          {chips}
        </div>
        {Array.isArray(report.keep) && report.keep.length ? (
          <div key="keep">
            <div style={S.sectionH}>
              {i18nT("apps.designCritique.designCritiquePage.what_s_working")}
            </div>
            {report.keep.map((k, i) => (
              <div key={i} style={S.keepItem}>
                <Check
                  size={15}
                  style={{ color: "#3fae6b", flexShrink: 0, marginTop: "2px" }}
                />
                <span>{k}</span>
              </div>
            ))}
          </div>
        ) : null}
        {tightenBlock}
        <DesignRoundPanel
          key="design-round"
          brief={reviewBrief}
          report={report}
          reviewRunId={slotRunId}
          rounds={designRounds}
          busy={designRoundBusy}
          onPrepare={prepareDesignRound}
          onUpdate={updateDesignRound}
        />
        {Array.isArray(report.couldNotSee) && report.couldNotSee.length ? (
          <div key="cns">
            <div style={S.sectionH}>
              {i18nT("apps.designCritique.designCritiquePage.couldn_t_see")}
            </div>
            {report.couldNotSee.map((c, i) => (
              <div key={i} style={S.seeItem}>
                {"· " + c}
              </div>
            ))}
            <div
              style={{
                display: "flex",
                gap: "8px",
                marginTop: "10px",
                flexWrap: "wrap",
              }}
            >
              <button style={S.linkBtn} onClick={sendScreenshots}>
                <Upload size={13} />
                {i18nT(
                  "apps.designCritique.designCritiquePage.send_screenshots",
                )}
              </button>
              <button style={S.linkBtn} onClick={critiqueRunning}>
                <ImageIcon size={13} />
                {i18nT(
                  "apps.designCritique.designCritiquePage.point_me_at_a_running_url",
                )}
              </button>
            </div>
          </div>
        ) : null}
      </>
    );
  } else if (busy) {
    railBody = (
      <>
        <p key="w" style={S.readLine}>
          {phase === "uploading" ? "Uploading…" : "Reading your design…"}
        </p>
        <div key="sub" style={S.health}>
          {screens.length > 1
            ? "A flow of " +
              screens.length +
              " screens — I’ll also check the jumps between them."
            : screens.length
              ? "One screen."
              : pendingKind
                ? "A " +
                  (KIND_LABEL[pendingKind] || "design") +
                  " — rendering it before judging it."
                : "Getting the screens ready."}
        </div>
        {screens.length ? (
          <div key="l">
            <div style={S.sectionH}>
              {screens.length > 1 ? "Screens in order" : "Screen"}
            </div>
            {screens.map((sc, i) => (
              <div
                key={i}
                style={{ ...S.seeItem, display: "flex", gap: "8px" }}
              >
                <span
                  style={{
                    color: "var(--muted)",
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {i + 1 + "."}
                </span>
                <span
                  style={{
                    flex: 1,
                    minWidth: 0,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {sc.label || "Screen " + (i + 1)}
                </span>
              </div>
            ))}
          </div>
        ) : null}
        <div key="nx">
          <div style={S.sectionH}>
            {i18nT("apps.designCritique.designCritiquePage.what_you_ll_get")}
          </div>
          {[
            "An overall read of how it’s doing",
            "What’s working, so it survives the next pass",
            "The top things to tighten, with pins on the screen",
          ].map((tx, i) => (
            <div key={i} style={S.seeItem}>
              {"· " + tx}
            </div>
          ))}
        </div>
      </>
    );
  } else {
    railBody = (
      <>
        <p key="sub" style={S.sub}>
          {i18nT(
            "apps.designCritique.designCritiquePage.point_me_at_a_screen_or_a_whole_flow_and_i_ll_re",
          )}
        </p>
        <button key="ex" style={S.linkBtn} onClick={openExample}>
          <Sparkle size={13} />
          {i18nT("apps.designCritique.designCritiquePage.see_an_example")}
        </button>
        <div key="h" style={S.sectionH}>
          {i18nT("apps.designCritique.designCritiquePage.your_critiques")}
        </div>
        {critiques.length ? (
          <div key="list">
            {critiques.map((e) => (
              <Clickable
                key={e.id}
                style={S.listItem}
                onClick={() => selectCritique(e)}
                title={
                  e.pending
                    ? i18nT(
                        "apps.designCritique.designCritiquePage.a_critique_is_still_running_click_to_watch_it",
                      )
                    : undefined
                }
                aria-busy={e.pending || undefined}
              >
                {e.thumbUrl ? (
                  <img src={e.thumbUrl} style={S.listThumb} alt="" />
                ) : null}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      ...S.listRead,
                      ...(e.pending ? S.pendingRead : {}),
                    }}
                  >
                    {e.pending
                      ? i18nT("apps.designCritique.designCritiquePage.running")
                      : e.read || "Critique"}
                  </div>
                  <div style={S.listTime}>{entryMeta(e)}</div>
                </div>
                {e.pending ? (
                  <Spinner size={13} reduceMotion={reduceMotion} />
                ) : (
                  <ChevronRight
                    size={15}
                    style={{ color: "var(--muted)", flexShrink: 0 }}
                  />
                )}
              </Clickable>
            ))}
          </div>
        ) : (
          <div key="empty" style={S.emptyList}>
            {"No critiques yet." +
              (narrow
                ? " Drop a screenshot below."
                : " Drop a screenshot in the panel →")}
          </div>
        )}
      </>
    );
  }

  // Failed background runs, on their own row under the rail head — not inside
  // its button row, where the notice's hand-off would join New / History /
  // Running as a third action. The failed run is gone from history and its
  // screens are on disk, so the hand-off has nothing on this rail to lose.
  const railFailures = backgroundFailures.length ? (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "6px",
        marginBottom: "12px",
      }}
    >
      {backgroundFailures.map((f) => (
        <ErrorNotice
          key={f.slotKey}
          message={f.message}
          title={i18nT(
            "apps.designCritique.designCritiquePage.that_critique_didn_t_finish",
          )}
          askAgent
          onDismiss={() =>
            setBackgroundFailures((prev) =>
              prev.filter((x) => x.slotKey !== f.slotKey),
            )
          }
        />
      ))}
    </div>
  ) : null;

  const rail = (
    <div
      style={{ ...S.rail, ...(narrow ? S.railNarrow : {}) }}
      onMouseUp={phase === "report" ? captureSelection : undefined}
    >
      {railHead}
      {railFailures}
      {railBody}
    </div>
  );

  // ── canvas ──
  let canvasInner: ReactNode;
  if (phase === "scoping" && scope) {
    canvasInner = (
      <ScopingPicker
        scope={scope}
        picked={picked}
        refBrief={refBrief}
        dragId={dragId}
        togglePick={togglePick}
        dropPickAt={dropPickAt}
        movePick={movePick}
        useFlow={useFlow}
        setDragId={setDragId}
        setRefBrief={setRefBrief}
        runScoped={runScoped}
        onStartOver={newCritique}
      />
    );
  } else if (
    phase === "uploading" ||
    phase === "analyzing" ||
    phase === "scanning"
  ) {
    canvasInner = (
      <WaitingScreen
        phase={phase}
        elapsed={elapsed}
        writing={writing}
        reduceMotion={reduceMotion}
        screens={screens}
        pendingKind={pendingKind}
        onCancel={cancelRun}
      />
    );
  } else if (phase === "report" && report && thumbUrl) {
    const markers = buildMarkers(true);
    const filmstrip = isFlow ? (
      <div style={S.strip}>
        {screens.map((sc, i) => {
          const mine = screenFindings.filter((f) => stepOf(f) === sc.step);
          const worst = mine.slice().sort(bySev)[0];
          const on = i === screenIdx;
          return (
            <button
              key={"th" + i}
              style={S.thumb}
              onClick={() => {
                setScreenIdx(i);
                setActive(null);
              }}
              title={"Step " + sc.step + " · " + sc.label}
              aria-current={on ? "true" : "false"}
            >
              <div style={{ ...S.thumbBox, ...(on ? S.thumbBoxOn : {}) }}>
                <img src={sc.url} style={S.thumbImg} alt="" />
                {worst ? (
                  <span
                    style={{
                      ...S.thumbCount,
                      color: sevOf(worst.severity).color,
                    }}
                  >
                    <span
                      style={{
                        width: "6px",
                        height: "6px",
                        borderRadius: "999px",
                        background: sevOf(worst.severity).color,
                        display: "inline-block",
                      }}
                    />
                    {String(mine.length)}
                  </span>
                ) : (
                  <span style={{ ...S.thumbCount, color: "#3fae6b" }}>
                    <Check size={11} />
                  </span>
                )}
              </div>
              <div
                style={{
                  ...S.thumbCap,
                  ...(on ? { color: "var(--text)" } : {}),
                }}
              >
                {sc.step + " · " + shortLabel(sc.label)}
              </div>
            </button>
          );
        })}
      </div>
    ) : null;
    canvasInner = (
      <>
        {filmstrip}
        <div style={S.previewWrap}>
          <Clickable
            style={S.preview}
            onClick={() => setZoom(true)}
            title={i18nT("apps.designCritique.designCritiquePage.enlarge")}
          >
            <img
              src={thumbUrl}
              style={S.previewImg}
              alt={shown ? shown.label : "the design"}
            />
            {markers}
          </Clickable>
          <div style={S.pvbar}>
            <Maximize2 size={12} />
            {"Click to enlarge" +
              (markers.length
                ? " · numbered pins mark issues on this screen"
                : "") +
              (isFlow ? " · pick a step above" : "")}
          </div>
        </div>
      </>
    );
  } else if (phase === "report" && report) {
    canvasInner = (
      <div style={S.previewWrap}>
        <div style={{ ...S.muted, maxWidth: "420px", textAlign: "center" }}>
          {i18nT(
            "apps.designCritique.designCritiquePage.no_screens_to_show_the_critic_couldn_t_get_pixel",
          )}
        </div>
      </div>
    );
  } else {
    canvasInner = (
      <Composer
        staged={staged}
        refText={refText}
        dragging={dragging}
        blocked={blocked}
        showAuth={showAuth}
        busy={busy || contextBusy}
        agents={agents}
        defaultAgent={agentList?.default_agent || DEFAULT_AGENT}
        selectedAgent={activeAgent}
        setSelectedAgent={setSelectedAgent}
        err={err}
        hint={hint}
        inputRef={inputRef}
        contexts={projectContexts}
        brief={reviewBrief}
        setBrief={updateReviewBrief}
        selectContext={selectProjectContext}
        saveContext={saveProjectContext}
        deleteContext={deleteProjectContext}
        onPick={onPick}
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        pickFile={pickFile}
        dropStaged={dropStaged}
        moveStaged={moveStaged}
        clearStaged={clearStaged}
        start={start}
        setRefText={setRefText}
        setBlocked={setBlocked}
        setShowAuth={setShowAuth}
        onTryAgain={() => runRef(refText)}
      />
    );
  }
  const canvas = <div style={S.canvas}>{canvasInner}</div>;

  const lightbox =
    zoom && thumbUrl ? (
      <div
        style={S.lbOverlay}
        role="presentation"
        onClick={(e) => {
          if (e.target === e.currentTarget) setZoom(false);
        }}
      >
        <div
          style={S.lbInner}
          role="dialog"
          aria-modal="true"
          aria-label={i18nT(
            "apps.designCritique.designCritiquePage.full_size_view",
          )}
        >
          <img
            src={thumbUrl}
            style={S.lbImg}
            alt={i18nT("apps.designCritique.designCritiquePage.full_size")}
          />
          {buildMarkers(false)}
        </div>
        <button
          style={S.lbClose}
          onClick={() => setZoom(false)}
          title={i18nT("apps.designCritique.designCritiquePage.close_esc")}
          aria-label={i18nT("apps.designCritique.askLayer.close")}
        >
          <X size={18} />
        </button>
      </div>
    ) : null;

  return (
    <div
      ref={rootRef}
      style={{ ...S.shell, ...(narrow ? { flexDirection: "column" } : {}) }}
    >
      {rail}
      {canvas}
      {lightbox}
      {phase === "report" ? (
        <AskLayer
          sel={sel}
          asks={asks}
          openAskId={openAskId}
          askDraft={askDraft}
          reduceMotion={reduceMotion}
          threadRef={threadRef}
          setOpenAskId={setOpenAskId}
          setSel={setSel}
          setAskDraft={setAskDraft}
          askAbout={askAbout}
          askFollowUp={askFollowUp}
          removeAsk={removeAsk}
          pending={askPending}
        />
      ) : null}
      {toasts.length ? (
        <div style={S.toastWrap}>
          {toasts.map((t2) => (
            <div
              key={t2.id}
              style={{
                ...S.toast,
                ...(t2.type === "error"
                  ? {
                      borderColor: "var(--error, #e5484d)",
                      color: "var(--error, #e5484d)",
                    }
                  : {}),
              }}
            >
              {t2.msg}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
