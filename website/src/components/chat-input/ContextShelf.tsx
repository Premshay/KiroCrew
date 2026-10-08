import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Bot, FileDiff } from 'lucide-react'
import AppIcon from '../AppIcon'
import ContextBar, { contextTip, contextColor, composeContextReadout, contextPctClamped, fmtTokens } from '../ContextBar'
import ErrorNotice from '../ErrorNotice'
import { Btn, Slider } from '../ui'
import { effortLabel } from '../../lib/effort'
import { fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type { ComposerControl } from '../composerControl'
import type { ChatInputProps } from './props'
import type { useAutoCompactThreshold } from './autoCompact'

/* The context shelf under the composer: its measured width (which collapses
   the chips to icons) and the controls that stand on it -- app session
   controls, the agent chip, the context readout with its popover, and the
   model chip. The shelf row and the project pill are the composer's own. */
export function useShelfMeasure() {
  // Shelf responsiveness: measure the shelf row width and collapse chips to
  // icon-only (agent/project) + drop the model effort label when space is tight.
  // Truncation handles the in-between cases.
  const [shelfWidth, setShelfWidth] = useState(9999)
  // Border-box height of the shelf, handed to `.glass-shelf::before` as
  // `--glass-shelf-h`: the fade under the shelf is positioned against the
  // dock's input-area wrapper (so it spans exactly the dock root, which already
  // stops short of the scrollbar gutter), not against the shelf, so it has to
  // be told how tall the shelf is to start at the pane's bottom edge. 32px is
  // the one-row shelf (pt-1 + h-7) for the first paint and for environments
  // without ResizeObserver.
  const [shelfHeight, setShelfHeight] = useState(32)
  const shelfRoRef = useRef<ResizeObserver | null>(null)
  const shelfRef = useCallback((el: HTMLDivElement | null) => {
    shelfRoRef.current?.disconnect()
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(entries => {
      const w = entries[0]?.contentRect.width
      if (typeof w === 'number') setShelfWidth(w)
      const h = entries[0]?.borderBoxSize?.[0]?.blockSize ?? entries[0]?.target.getBoundingClientRect().height
      if (typeof h === 'number' && h > 0) setShelfHeight(h)
    })
    ro.observe(el)
    shelfRoRef.current = ro
  }, [])
  // The labels no longer fit alongside the context bar, the model chip AND the
  // working-tree badge, so collapse the chips (agent/project) to icon-only. The
  // threshold is a phone width, not 340: a 390px phone leaves the shelf ~364px,
  // which cleared 340 and overflowed -- the badge then painted over the context
  // readout. 420 covers the phone range with the labels shed.
  const shelfCompact = shelfWidth < 420
  // A two-column split can leave under 200px per composer. The effort level on
  // the model chip is the only at-a-glance readout of what a turn runs at, so
  // it survives the compact collapse and drops only when even a short word has
  // no room (the picker the chip opens always shows the level in force).
  const shelfTiny = shelfWidth < 220
  return { shelfRef, shelfHeight, shelfCompact, shelfTiny }
}

/** The context popover: open state and its outside-click dismissal. */
export function useContextPopover() {
  const [ctxPopoverOpen, setCtxPopoverOpen] = useState(false)
  // Where the trigger was when the panel opened. The panel is portalled, so it
  // cannot anchor to its wrapper: an absolutely-positioned child of the shelf
  // is clipped by any scroller around it, and tapping the readout then opened
  // nothing. A viewport rect survives the portal.
  const [ctxPopoverRect, setCtxPopoverRect] = useState<DOMRect | null>(null)
  const ctxWrapRef = useRef<HTMLDivElement>(null)
  // The portalled panel is outside `ctxWrapRef` in the DOM, so the outside-click
  // test has to know about it too -- otherwise a mousedown on the panel's own
  // auto-compact slider reads as a click outside and closes it.
  const ctxPanelRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!ctxPopoverOpen) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      const insideTrigger = !!ctxWrapRef.current?.contains(target)
      const insidePanel = !!ctxPanelRef.current?.contains(target)
      if (!insideTrigger && !insidePanel) setCtxPopoverOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [ctxPopoverOpen])
  return { ctxPopoverOpen, setCtxPopoverOpen, ctxPopoverRect, setCtxPopoverRect, ctxWrapRef, ctxPanelRef }
}

/** App-contributed session controls (`contributes.sessionControls`). */
export function SessionControlChips({ sessionControls, shelfCompact, onSessionControlClick }: {
  sessionControls: NonNullable<ChatInputProps['sessionControls']>
  shelfCompact: boolean
  onSessionControlClick: ChatInputProps['onSessionControlClick']
}) {
  return (
    <div className="flex items-center gap-2 min-w-0 shrink-0 pr-2 border-r border-border">
  {(sessionControls || []).map(sc => {
    /* State must not be carried by colour alone: `ok` and `warn` differ
       only by tint, which a colourblind user cannot separate and a
       screen reader never sees at all. Fold it into the accessible name,
       and APPEND the app's own tooltip rather than replacing the label —
       the label is what identifies the control, so it has to survive
       whatever the app reports about it. */
    const stateWord =
      sc.state === 'warn'
        ? i18nT('components.chatInput.session_control_needs_attention')
        : sc.state === 'ok'
          ? i18nT('components.chatInput.session_control_ready')
          : ''
    // The app's `detail` follows the manifest label on the chip itself, so
    // the label still names the control whatever the app reports.
    const shown = sc.detail
      ? i18nT('components.chatInput.session_control_chip_label', {
          label: sc.label,
          detail: sc.detail,
        })
      : sc.label
    const detail = sc.statusTooltip || stateWord
    const chipName = detail
      ? i18nT('components.chatInput.session_control_chip_label', {
          label: shown,
          detail,
        })
      : shown
    return (
    <button
      key={sc.key}
      /* No `font-mono`: same reasoning as the agent chip below — a
         control label is a label, not code, and pinning `var(--mono)`
         would make the shelf ignore the user's Font Family setting. */
      className={`inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer ${
        /* Open wins, so the chip you are pointing at always reads as
           the active one; otherwise the app's own state colours it. */
        sc.active
          ? 'text-accent'
          : sc.state === 'ok'
            ? 'text-ok'
            : sc.state === 'warn'
              ? 'text-warn'
              : 'text-muted hover:text-text'
      }`}
      onClick={e => onSessionControlClick?.(sc.key, e.currentTarget.getBoundingClientRect(), e.currentTarget)}
      // Marks the chip as part of its own popover for dismissal
      // purposes: mousedown fires before click, so without this the
      // host's outside-click closes the popover and the chip's toggle
      // then re-opens it — a flicker instead of a dismissal.
      data-session-control-chip=""
      title={chipName}
      aria-label={chipName}
    >
      <AppIcon icon={sc.icon} size={13} />
      {!shelfCompact && <span className="truncate max-w-[140px]">{shown}</span>}
    </button>
    )
  })}
    </div>
  )
}

/** The agent chip. Chrome type: an agent name is a label, not code. `font-mono`
 *  would pin `var(--mono)`, which Settings → Display → Font Family writes only
 *  for OpenDyslexic, so for every other family it would make the shelf ignore
 *  the user's typeface. */
export function AgentChip({ agentName, agentLabel, agentIsInheritedDefault, agentSource, isRunning, shelfCompact, onAgentClick }: {
  agentName: string
  agentLabel?: string
  agentIsInheritedDefault?: boolean
  agentSource?: string
  isRunning: boolean
  shelfCompact: boolean
  onAgentClick: NonNullable<ChatInputProps['onAgentClick']>
}) {
  return (
    <button
      className={`inline-flex items-center gap-1.5 h-7 text-[12px] px-2.5 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer disabled:cursor-not-allowed disabled:hover:bg-transparent ${agentSource === 'package' ? 'text-[var(--aim)] hover:text-[var(--aim)]' : 'text-muted hover:text-text disabled:hover:text-muted'}`}
      onClick={e => onAgentClick(e.currentTarget.getBoundingClientRect(), e.currentTarget)}
      disabled={isRunning}
      // Inherited default: explain what the ` . default` marker means, on
      // hover (title) AND keyboard focus / screen readers (aria-label),
      // because the marker alone reads as opaque. No glyph, no
      // layout change -- text on demand. A pinned chip keeps the plain
      // switch hint; it has nothing to explain.
      title={isRunning
        ? i18nT('components.chatInput.stop_the_current_response_to_switch_agents')
        : agentIsInheritedDefault
          ? i18nT('components.chatInput.agent_inherited_default', { name: agentName })
          : i18nT('components.chatInput.agent', { name: agentName })}
      aria-label={isRunning
        ? i18nT('components.chatInput.stop_the_current_response_to_switch_agents')
        : agentIsInheritedDefault
          ? i18nT('components.chatInput.agent_inherited_default', { name: agentName })
          : i18nT('components.chatInput.agent', { name: agentName })}
    >
      <Bot size={13} className="shrink-0 opacity-70" />
      {!shelfCompact && <span className="truncate max-w-[160px]">{agentLabel ?? agentName}</span>}
    </button>
  )
}

/** The context-window readout and its popover (usage, model, the per-session
 *  auto-compact threshold). */
export function ContextUsageControl({ contextPct, contextUsedTokens, contextWindowTokens, showContextPct, showContextTokens, shelfCompact, modelName, ctxPopoverOpen, setCtxPopoverOpen, ctxPopoverRect, setCtxPopoverRect, ctxWrapRef, ctxPanelRef, autoCompactThreshold }: {
  contextPct: number
  contextUsedTokens?: number
  contextWindowTokens?: number
  showContextPct?: boolean
  showContextTokens?: boolean
  shelfCompact: boolean
  modelName?: string
  ctxPopoverOpen: boolean
  setCtxPopoverOpen: (update: (open: boolean) => boolean) => void
  ctxPopoverRect: DOMRect | null
  setCtxPopoverRect: (rect: DOMRect | null) => void
  ctxWrapRef: React.RefObject<HTMLDivElement>
  ctxPanelRef: React.RefObject<HTMLDivElement>
  autoCompactThreshold: ReturnType<typeof useAutoCompactThreshold>
}) {
  const { autoCompactQuery, autoCompact, autoCompactError, setAutoCompactError, pushAutoCompact } = autoCompactThreshold
  const pct = Math.round(contextPct)
  const win = contextWindowTokens || 0
  const used = contextUsedTokens != null ? contextUsedTokens : (win ? Math.round((pct / 100) * win) : 0)
  const remaining = win ? Math.max(win - used, 0) : 0
  const approx = contextUsedTokens == null
  const hasKnownWindow = win > 0
  const hasMeasuredUsed = contextUsedTokens != null && contextUsedTokens > 0
  // With no window there is no percentage to colour or plot; the readout
  // degrades to a bare token count rather than asserting a 0% that was never
  // measured.
  const pctColor = hasKnownWindow ? contextColor(contextPct) : 'var(--text)'
  const showPct = !!showContextPct && hasKnownWindow
  const showTokens = !!showContextTokens && (hasKnownWindow || hasMeasuredUsed)
  // Graceful degrade: on a narrow shelf, collapse to the percentage
  // alone (or tokens, if that's the only segment enabled) so the
  // readout never crowds out the agent/model controls.
  const readout = shelfCompact
    ? composeContextReadout(contextPct, used, win, { approx, showPct, showTokens: showTokens && !showPct })
    : composeContextReadout(contextPct, used, win, { approx, showPct, showTokens })
  return (
  <div ref={ctxWrapRef} className="relative flex items-center">
    <button
      className={`inline-flex items-center h-7 px-2.5 rounded-md transition-colors border-none cursor-pointer ${ctxPopoverOpen ? 'bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))]' : 'bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))]'}`}
      onClick={e => {
        setCtxPopoverRect(e.currentTarget.getBoundingClientRect())
        setCtxPopoverOpen(o => !o)
      }}
      title={hasKnownWindow ? contextTip(contextPct) : i18nT('components.chatInput.context_usage')}
      aria-label={i18nT('components.chatInput.context_usage')}
    >
      <ContextBar pct={hasKnownWindow ? contextPct : 0} width={40} height={3} decorative={!hasKnownWindow} />
      {!!readout && <span className="text-[11px] ml-1.5 tabular-nums whitespace-nowrap" style={{ color: pctColor }}>{readout}</span>}
    </button>
    {ctxPopoverOpen && ctxPopoverRect && createPortal(
      <div
        ref={ctxPanelRef}
        className="fixed z-[60] w-52 rounded-xl border border-border bg-bg-elevated shadow-xl p-3 animate-slide-up"
        style={{
          left: Math.max(8, Math.min(ctxPopoverRect.right - 208, window.innerWidth - 208 - 8)),
          bottom: window.innerHeight - ctxPopoverRect.top + 4,
        }}
      >
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[11px] font-semibold text-text">{hasKnownWindow ? i18nT('components.chatInput.context_window') : i18nT('components.chatInput.context_usage')}</span>
                  <span className="text-[12px] font-mono font-bold" style={{ color: pctColor }}>{hasKnownWindow ? fmtPercent(contextPctClamped(contextPct) / 100) : fmtTokens(used)}</span>
                </div>
                <div className="flex flex-col gap-1 text-[11px] font-mono">
                  <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.used')}</span><span className="text-text">{approx ? '~' : ''}{fmtTokens(used)}</span></div>
                  {hasKnownWindow && <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.remaining')}</span><span className="text-text">{approx ? '~' : ''}{fmtTokens(remaining)}</span></div>}
                  {hasKnownWindow && <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.total')}</span><span className="text-text">{fmtTokens(win)}</span></div>}
                </div>
                {modelName && (
                  <div className="mt-2 pt-2 border-t border-border flex justify-between text-[11px] font-mono">
                    <span className="text-muted">{i18nT('components.chatInput.model')}</span><span className="text-text truncate max-w-[120px]" title={modelName}>{modelName}</span>
                  </div>
                )}
                {autoCompactQuery.isLoading && (
                  <div className="mt-2 pt-2 border-t border-border" aria-hidden="true">
                    <div className="h-4 mb-1 rounded bg-bg-hover animate-pulse" />
                    <div className="h-5 rounded bg-bg-hover animate-pulse" />
                  </div>
                )}
                {autoCompactQuery.isError && !autoCompact && (
                  <div className="mt-2 pt-2 border-t border-border">
                    {/* No hand-off: the composer draft below is unsaved. */}
                    <ErrorNotice
                      variant="inline"
                      testId="auto-compact-load-error"
                      message={i18nT('components.chatInput.auto_compact_load_failed')}
                    />
                  </div>
                )}
                {autoCompactError && (
                  <div className="mt-2 pt-2 border-t border-border">
                    {/* No hand-off: same composer draft. The shared notice toast is
                        transient; the write that did not persist is reported HERE,
                        next to the slider whose value snapped back. */}
                    <ErrorNotice
                      variant="inline"
                      testId="auto-compact-write-error"
                      message={autoCompactError}
                      onDismiss={() => setAutoCompactError('')}
                    />
                  </div>
                )}
                {autoCompact && (
                  <div className="mt-2 pt-2 border-t border-border">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[11px] text-muted">{i18nT('components.chatInput.auto_compact_at')}</span>
                      <span className="text-[12px] font-mono font-bold text-accent">{fmtPercent(Math.round(autoCompact.pct ?? autoCompact.global_pct) / 100)}</span>
                    </div>
                    <Slider
                      value={autoCompact.pct ?? autoCompact.global_pct}
                      onChange={v => pushAutoCompact(v)}
                      min={autoCompact.min}
                      max={autoCompact.max}
                      step={1}
                      formatValue={v => fmtPercent(v / 100)}
                      aria-label={i18nT('components.chatInput.auto_compact_threshold')}
                    />
                    {autoCompact.pct != null ? (
                      <Btn
                        className="mt-1 px-0 py-0 border-none text-[10px] text-muted underline hover:text-text hover:bg-transparent"
                        onClick={() => pushAutoCompact(null)}
                      >
                        {i18nT('components.chatInput.reset_to_global', { pct: Math.round(autoCompact.global_pct) })}
                      </Btn>
                    ) : (
                      <div className="mt-1 text-[10px] text-muted">{i18nT('components.chatInput.following_global', { pct: Math.round(autoCompact.global_pct) })}</div>
                    )}
                  </div>
                )}
        </div>,
      document.body,
    )}
  </div>
  )
}

/** Tooltip for the working-tree badge. Reuses the Git panel's catalog entry so
 *  the badge adds no i18n keys; the arrow segments are glyph+number only
 *  (script-neutral, plain concatenation -- a template literal here reads as an
 *  untranslated string to the i18n gate). Empty when the tree is clean and in
 *  sync, which is also what hides the badge. */
export function useGitBadgeTitle(dirty?: number, truncated?: boolean, ahead?: number, behind?: number) {
  return useMemo(() => {
    const parts: string[] = []
    if (dirty) {
      // A capped listing makes the count a floor, so it is read as "500+" here
      // and in the badge -- the same claim the Git panel's pill makes.
      parts.push(i18nT(truncated ? 'components.gitPanel.uncommitted_capped' : 'components.gitPanel.uncommitted', { count: dirty }))
    }
    if (ahead) parts.push('\u2191' + String(ahead))
    if (behind) parts.push('\u2193' + String(behind))
    return parts.join(' \u00b7 ')
  }, [dirty, truncated, ahead, behind])
}

/** Working-tree badge: dirty count (warn pill) plus ahead/behind arrows, the Git
 *  panel's own vocabulary. Renders only when there is signal, so a clean in-sync
 *  tree keeps the footer as it was. A passive READOUT, not a button: the shelf
 *  row already carries three actions (agent, picker, copy) and
 *  max-two-buttons-per-row forbids growing a 3+ row, exactly like the context
 *  readout. The Git panel stays one click away in the sidebar. NOT gated on
 *  shelfCompact: icon+digits have no text label to shed, and hiding the badge at
 *  narrow widths would remove the only tree-state signal. */
export function GitTreeBadge({ title, dirty, truncated, ahead, behind }: {
  title: string
  dirty?: number
  truncated?: boolean
  ahead?: number
  behind?: number
}) {
  return (
    <span className="inline-flex items-center gap-1 h-7 shrink-0 text-[11px] font-mono px-1.5 text-muted" role="status" title={title} aria-label={title}>
      {!!dirty && (
        /* The icon makes the count read as "changed files" on a cold look -- a
           bare warn number beside a branch name could be anything. */
        <span className="inline-flex items-center gap-0.5 px-1 py-px rounded bg-warn/15 text-warn">
          <FileDiff size={11} className="shrink-0" />
          {truncated ? `${dirty}+` : dirty}
        </span>
      )}
      {(!!ahead || !!behind) && (
        <span>
          {!!ahead && <>&#x2191;{ahead}</>}
          {!!behind && <>{ahead ? ' ' : ''}&#x2193;{behind}</>}
        </span>
      )}
    </span>
  )
}

/** The model chip. It names the level in force beside the model; the level is
 *  CHANGED inside the model picker the chip opens (model and effort are one
 *  control, docs/decisions/2026-06-14-chat-composer-model-and-effort-are-one-control.md). */
export function ModelChip({ modelName, modelIsJevRouted, modelIsInheritedDefault, modelIsAutoChosen, reasoningEffort, effortIsDefault, hasEffort, isRunning, shelfCompact, shelfTiny, composerControl, modelChipPressedFromComposerRef, onModelClick }: {
  modelName: string
  modelIsJevRouted?: boolean
  modelIsInheritedDefault?: boolean
  modelIsAutoChosen?: boolean
  reasoningEffort?: string
  effortIsDefault: boolean
  hasEffort?: boolean
  isRunning: boolean
  shelfCompact: boolean
  shelfTiny: boolean
  composerControl: () => ComposerControl | null
  /** Owned by the composer, so a press outlives a remount of the chip exactly as it did. */
  modelChipPressedFromComposerRef: React.MutableRefObject<boolean>
  onModelClick: NonNullable<ChatInputProps['onModelClick']>
}) {
  // The chip shows the level in every state (running, routed, pinned
  // or inherited), so its title / accessible name carries it in every
  // state too -- one suffix, appended to each branch.
  // A default and an override show the same level on the chip; the
  // name says which one it is (the picker's own "Default · High").
  const effortShown = effortIsDefault
    ? i18nT('components.reasoningEffortDropdown.default_with_level', { level: effortLabel(reasoningEffort || '') })
    : effortLabel(reasoningEffort || '')
  const effortSuffix = hasEffort
    ? ` · ${i18nT('components.reasoningEffortDropdown.reasoning_effort')}: ${effortShown}`
    : ''
  const modelChipLabel = `${isRunning
    ? i18nT('components.chatInput.stop_the_current_response_to_switch_model')
    : modelIsJevRouted
      ? i18nT('pages.chatPage.model_auto_jev_description')
      : modelIsInheritedDefault
        ? i18nT('components.chatInput.model_inherited_default', { name: modelName })
        : modelIsAutoChosen
          ? `${i18nT('components.chatInput.model_2', { name: modelName })} · ${i18nT('components.jobForm.auto')}`
          : i18nT('components.chatInput.model_2', { name: modelName })}${effortSuffix}`
  return (
  <button
    className="inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] text-muted hover:text-text px-2 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-muted"
    onMouseDown={() => {
      const editor = composerControl()?.getRootElement()
      modelChipPressedFromComposerRef.current = !!editor && editor.contains(document.activeElement)
    }}
    onClick={e => {
      const composerHadFocus = modelChipPressedFromComposerRef.current
      modelChipPressedFromComposerRef.current = false
      onModelClick(e.currentTarget.getBoundingClientRect(), e.currentTarget, composerHadFocus)
    }}
    disabled={isRunning}
    data-testid="composer-model-chip"
    // Inherited default: mirror the agent chip -- ` · default` marker on
    // the label, and the explanation on hover (title) AND keyboard
    // focus / screen readers (aria-label), because a bare served id
    // reads exactly like a pin. A pinned chip keeps the plain hint.
    // The effort level rides along on both: `aria-label` REPLACES the
    // chip's content in the accessible name, so without it a screen
    // reader never hears the level the chip shows, and the tooltip is
    // the only readout left when the shelf is too narrow to show it.
    title={modelChipLabel}
    aria-label={modelChipLabel}
  >
    {/* Fork: the cap follows the shelf, so a long model id cannot squeeze the
        chips beside it on a phone. */}
    <span className={`truncate ${shelfCompact ? 'max-w-[96px]' : 'max-w-[180px]'}`}>
      {modelIsJevRouted ? i18nT('components.modelDropdownList.auto_jev') : modelName}
    </span>
    {/* Outside the truncating span: a long provider-prefixed id must
        ellipsize its own tail, never the marker beside it. A routed chip
        takes NO marker -- its label is already the policy, and a second
        word next to it would be a marker on a name that is not a model.
        So the two unpinned states differ by KIND (a policy vs an id with
        a marker), not by two adjectives a reader has to tell apart. */}
    {!modelIsJevRouted && (modelIsInheritedDefault || modelIsAutoChosen) && (
      <>
        <span className="opacity-30 select-none shrink-0" aria-hidden="true">·</span>
        <span className="opacity-60 shrink-0">{modelIsInheritedDefault
          ? i18nT('components.agentSelector.default')
          : i18nT('components.jobForm.auto')}</span>
      </>
    )}
    {/* A default and an override show the same level, and a glance
        at "High" alone could not tell which one set it. So the chip
        says so where there is room -- the picker's own "Default ·
        High" -- and keeps the bare level only in a compact shelf,
        where the hover / accessible name above still carries it.
        An inherited-default MODEL already put one "Default" on the
        chip; a second, meaning the effort, right after it would be
        the same word twice for two unrelated facts, so that chip
        keeps the bare level as well (the name above still says
        "Reasoning effort: Default · High"). */}
    {hasEffort && !shelfTiny && (
      <>
        <span className="opacity-30 select-none shrink-0" aria-hidden="true">·</span>
        <span className="opacity-60 shrink-0">{shelfCompact || modelIsInheritedDefault ? effortLabel(reasoningEffort || '') : effortShown}</span>
      </>
    )}
  </button>
  )
}
