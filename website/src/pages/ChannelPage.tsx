import { useQuery } from '@tanstack/react-query'
import { AnimatePresence } from 'framer-motion'
import {
  Loader2,
  MessageSquare,
  RotateCcw,
  Users,
  Wrench,
  X,
  Zap
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import { FALLBACK_PRESETS, channelTaskPrompt } from '../prompts/channelPresets'
import { ApiError } from '../api/apiError'
import { useConfirm } from '../components/ConfirmDialog'
import { fmtList } from '../i18n/format'
import AgentSelector from '../components/AgentSelector'
import DetailPanel from '../components/DetailPanel'
import ErrorNotice from '../components/ErrorNotice'
import Modal from '../components/Modal'
import SimpleSelect from '../components/SimpleSelect'
import { Badge, Btn, EmptyState, Input, PageHeader } from '../components/ui'
import { useAgents } from '../hooks/useAgents'
import { useImeGuard } from '../hooks/useImeGuard'
import { useAppDispatch } from '../store'
import { triggerRefresh } from '../store/dashboardSlice'

import ListDetailBack from '../components/ListDetailBack'
import { useAutoGrowTextarea } from '../hooks/useAutoGrowTextarea'
import { useListDetailView } from '../hooks/useListDetailView'
import { i18nT } from '../i18n/t'
import {
  AgentBadge,
  AgentControlRow,
  attachableSlotLabel,
  isAttachableSlot,
  mapAttachRequest,
  mapChannel,
  mapMsg,
  MessageBubble,
  STATE_BADGE,
} from './ChannelActors'
import type { AttachRequest, Channel, ChannelAgent, ChannelPageError, DashboardSlot } from './ChannelActors'
export { approvalCardProps, approvalToolTitle, MessageBubble } from './ChannelActors'
export type { ApprovalMeta } from './ChannelActors'
type ClearScope = 'all' | 'agent'

// ── Team Presets ──

// Preset labels localize by stable API id on both healthy and fallback paths.
// Roles and task prompts remain gateway identifiers and model instructions.
/** Catalog KEY for each built-in preset's display name, by stable preset id. */
const PRESET_LABEL_KEY: Record<string, string> = {
  incident: 'pages.channelPage.preset_incident_response',
  custom: 'pages.channelPage.preset_custom_empty',
}

type Preset = {
  id: string
  label?: string
  agents: { role: string; is_orchestrator?: boolean; task?: string }[]
}

/**
 * Localised display name for a preset.
 *
 * `hasOwnProperty`, not `in`: the ids come from the API, so a backend that
 * reported `toString` or `constructor` would otherwise resolve to an inherited
 * Object.prototype member and hand a function to i18next. A preset this build
 * has no key for (a newer backend, a user-defined preset) falls back to the
 * server's own label, then to the id — never to fabricated copy.
 */
function presetLabel(p: Preset): string {
  return Object.prototype.hasOwnProperty.call(PRESET_LABEL_KEY, p.id)
    ? i18nT(PRESET_LABEL_KEY[p.id])
    : p.label || p.id
}

// ── New Channel Dialog (Step 2) ──

/**
 * A form's save that keeps the form open until the request succeeds. `save`
 * resolves to the error text when the request failed and to `null` once it
 * landed (then `onSaved` closes the form). A failure keeps every typed value
 * and shows beside the form, where pressing the same button again retries. A
 * failure that lands after the form was closed goes to `onClosedError`, so it
 * is still reported. While `saving`, the form locks its fields: a success
 * closes it, so an edit made mid-save would be lost unsaved.
 */
function useKeptFormSave(onSaved: () => void, onClosedError: (message: string) => void) {
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  // A ref, not `saving`: two clicks inside one render would both read `false`.
  const inFlight = useRef(false)
  const run = async (save: () => Promise<string | null>) => {
    if (inFlight.current) return
    inFlight.current = true
    setSaving(true)
    setError(null)
    const failed = await save()
    inFlight.current = false
    if (!mounted.current) {
      if (failed) onClosedError(failed)
      return
    }
    setSaving(false)
    setError(failed)
    // Only the form that started the save closes: a form closed and reopened
    // while this save was in flight is a new draft and is left alone.
    if (!failed) onSaved()
  }
  return { saving, error, clearError: () => setError(null), run }
}

/** The notice's bold lead, dropped when the backend sent no text and the message already is it. */
const keptErrorTitle = (titleKey: string, message: string | null) => {
  const title = i18nT(titleKey)
  return message && message !== title ? title : undefined
}

function NewChannelDialog({ onClose, onCreate, onClosedError, presets }: {
  onClose: () => void
  /** Resolves to the error text on failure, `null` on success. */
  onCreate: (topic: string, presetId: string) => Promise<string | null>
  onClosedError: (message: string) => void
  presets: Preset[]
}) {
  const [topic, setTopic] = useState('')
  const [preset, setPreset] = useState(
    () =>
      presets.find((candidate) => candidate.agents.length === 0)?.id || presets[0]?.id || 'custom',
  )
  const topicRef = useRef<HTMLInputElement>(null)
  const save = useKeptFormSave(onClose, onClosedError)

  // Initial focus belongs on the Topic field — the one input this dialog
  // exists to collect. Modal's shared focus trap focuses the dialog's FIRST
  // focusable on mount, which is the header's X button; this effect flushes
  // after the child Modal's (child effects run before the parent's), so it
  // wins. A plain `autoFocus` attribute cannot: React applies it during
  // commit, before the trap's mount effect runs.
  useEffect(() => {
    topicRef.current?.focus({ preventScroll: true })
  }, [])

  const handleCreate = () => {
    if (!topic.trim()) return
    void save.run(() => onCreate(topic.trim(), preset))
  }

  return (
    // The shared Modal owns the backdrop, Escape dismissal, keyboard isolation
    // (global chords stopped at the dialog panel, header X included; Escape
    // excepted; an IME-owned Escape claimed — see Modal.tsx), scroll lock, and
    // the focus trap/restore the hand-rolled overlay lacked. `open` is constant
    // because the call site conditionally mounts this component — that is what
    // resets topic/preset on every open (an always-mounted dialog would compute
    // the default preset once, before the presets fetch resolves).
    // `ariaLabel` keeps the dialog's established accessible name ("New channel"),
    // which predates this conversion and differs from the rendered title only in
    // case. The eslint disable below covers the label/Input association
    // (label-has-for cannot see through the custom <Input>).
    <Modal
      open
      onClose={onClose}
      title={i18nT('pages.channelPage.new_channel_2')}
      ariaLabel={i18nT('pages.channelPage.new_channel')}
      maxWidth={384}
      footer={
        <>
          <Btn onClick={onClose}>{i18nT('pages.channelPage.cancel')}</Btn>
          {/* `aria-disabled` while saving, not `disabled`: the click that started the
              save holds focus here, and a disabled button would drop it to <body>. */}
          <Btn onClick={handleCreate} disabled={!topic.trim()} aria-disabled={save.saving} aria-busy={save.saving}
            className="aria-disabled:opacity-30 aria-disabled:cursor-not-allowed" primary>{i18nT('pages.channelPage.create')}</Btn>
        </>
      }
    >
      {/* No hand-off: the unsaved topic and preset in this dialog */}
      <ErrorNotice
        title={keptErrorTitle('pages.channelPage.failed_to_create_channel', save.error)}
        message={save.error}
        onDismiss={save.clearError}
        className="mb-3"
        testId="new-channel-error"
      />
      <label htmlFor="new-channel-topic" className="block text-[13px] font-medium text-muted mb-1">{i18nT('pages.channelPage.topic')}</label>
      {/* No composition tracking here: the IME-owned-Escape claim moved into
        * Modal with the keyboard boundary, and Modal's document-tracked latch
        * hears this input's composition events natively — a local latch would
        * have no reader. */}
      <Input ref={topicRef} id="new-channel-topic" aria-label={i18nT('pages.channelPage.topic')} value={topic} readOnly={save.saving} onChange={e => { if (!save.saving) setTopic(e.target.value) }}
        className="w-full mb-4"
        placeholder={i18nT('pages.channelPage.e_g_investigate_gamma_deployment_failure')}
      />
      <span id="new-channel-preset-label" className="block text-[13px] font-medium text-muted mb-1">
        {i18nT('pages.channelPage.team_preset')}
      </span>
      <div role="radiogroup" aria-labelledby="new-channel-preset-label" className="space-y-1.5">
        {presets.map(p => (
          <Btn key={p.id} onClick={() => { if (!save.saving) setPreset(p.id) }} aria-disabled={save.saving}
            className={`w-full text-left px-3 py-2 !rounded-lg text-sm ${preset === p.id ? '!border-accent bg-accent/10 text-text-strong' : '!border-border text-muted hover:bg-bg-hover'}`}>
            <span className="font-medium">{presetLabel(p)}</span>
            {p.agents.length > 0 && (
              <span className="text-[13px] text-muted ml-2">
                ({p.agents.map((a) => a.role).join(', ')})
              </span>
            )}
          </Btn>
        ))}
      </div>
    </Modal>
  )
}

// ── @Mention Input (Step 4) ──

function MentionInput({
  agents,
  value,
  onChange,
  onSend,
}: {
  agents: ChannelAgent[]
  value: string
  onChange: (v: string) => void
  onSend: () => void
}) {
  const [show, setShow] = useState(false)
  const [filter, setFilter] = useState('')
  const [sel, setSel] = useState(0)
  const ref = useRef<HTMLTextAreaElement>(null)
  const ime = useImeGuard()

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value
    onChange(v)
    const m = v.match(/@([A-Za-z][\w ]*)?$/)
    if (m) {
      setFilter((m[1] || '').trim().toLowerCase())
      setShow(true)
      setSel(0)
    } else setShow(false)
  }

  // The shared hook, which now carries the hidden-mount guard and the
  // visibility re-measure, rather than a second spelling of both here.
  useAutoGrowTextarea(ref, value, Math.round(window.innerHeight * 0.3))

  const pick = (a: ChannelAgent) => {
    onChange(value.replace(/@[\w ]*$/, `@${a.role} `))
    setShow(false)
    ref.current?.focus()
  }

  const active = agents.filter(
    (a) => a.state !== 'done' && a.state !== 'failed' && a.role.toLowerCase().includes(filter),
  )

  return (
    <div className="relative flex-1 min-w-0">
      {show && active.length > 0 && (
        <div
          role="listbox"
          aria-label={i18nT('pages.channelPage.mention_suggestions')}
          className="absolute bottom-full left-0 mb-1 w-60 bg-bg-elevated border border-border rounded-lg shadow-lg z-10 py-1"
        >
          {active.map((a, i) => (
            <Btn
              key={a.id}
              onClick={() => pick(a)}
              className={`w-full text-left px-3 py-1.5 text-sm !border-none flex items-center gap-2 ${i === sel ? '!bg-accent !text-accent-fg' : 'hover:bg-bg-hover'}`}
            >
              <AgentBadge agent={a} index={i} />{' '}
              <Badge variant={STATE_BADGE[a.state]?.variant || 'warn'}>
                {STATE_BADGE[a.state]?.label || a.state}
              </Badge>
            </Btn>
          ))}
        </div>
      )}
      <textarea
        ref={ref}
        value={value}
        onChange={handleChange}
        rows={1}
        aria-label={i18nT('pages.channelPage.message_the_channel')}
        className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-hidden flex-1 transition-colors focus-ring resize-none"
        placeholder={i18nT('pages.channelPage.message_the_channel_type_to_mention')}
        {...ime.bindComposition()}
        onKeyDown={(e) => {
          if (show && active.length > 0) {
            if (e.key === 'ArrowDown') {
              e.preventDefault()
              setSel((s) => (s + 1) % active.length)
            } else if (e.key === 'ArrowUp') {
              e.preventDefault()
              setSel((s) => (s - 1 + active.length) % active.length)
            } else if (e.key === 'Enter') {
              if (ime.claimEnter(e)) pick(active[sel])
            } else if (e.key === 'Escape') {
              ime.reset()
              setShow(false)
            }
          } else if (e.key === 'Enter' && !e.shiftKey) {
            if (ime.claimEnter(e)) onSend()
          }
        }}
      />
    </div>
  )
}

// ── Add Agent Form ──

function AddAgentForm({ onAdd, onCancel, onClosedError }: {
  /** Resolves to the error text on failure, `null` on success. */
  onAdd: (role: string, task: string, agent: string) => Promise<string | null>
  onCancel: () => void
  onClosedError: (message: string) => void
}) {
  const ime = useImeGuard()
  const save = useKeptFormSave(onCancel, onClosedError)
  const [role, setRole] = useState('')
  const [task, setTask] = useState('')
  const {
    agents,
    defaultAgent,
    error: rosterError,
    reload: reloadRoster,
    reloading: rosterReloading,
  } = useAgents(0)
  const dispatch = useAppDispatch()
  // Recover every roster consumer, not just this form — see SchedulePage's note.
  const recoverRoster = useCallback(() => {
    reloadRoster()
    dispatch(triggerRefresh())
  }, [reloadRoster, dispatch])
  const rosterFailure = rosterError
    ? { reloading: rosterReloading, onReload: recoverRoster }
    : undefined
  const [agent, setAgent] = useState('')
  const submit = () => {
    if (role.trim()) void save.run(() => onAdd(role.trim(), task.trim(), agent || defaultAgent))
  }
  return (
    <div className="p-2 space-y-2 border-t border-border">
      {/* No hand-off: the unsaved role, task and agent in this form */}
      <ErrorNotice
        title={keptErrorTitle('pages.channelPage.failed_to_add_agent', save.error)}
        message={save.error}
        onDismiss={save.clearError}
        testId="add-agent-error"
      />
      <div>
        <AgentSelector agents={agents} defaultAgent={defaultAgent} value={agent || defaultAgent} onChange={v => { if (!save.saving) setAgent(v) }} rosterFailure={rosterFailure} />
      </div>
      <Input value={role} readOnly={save.saving} onChange={e => { if (!save.saving) setRole(e.target.value) }} placeholder={i18nT('pages.channelPage.role_e_g_logs_agent')} aria-label={i18nT('pages.channelPage.role')} autoFocus
        className="w-full text-[13px]" />
      <Input value={task} readOnly={save.saving} onChange={e => { if (!save.saving) setTask(e.target.value) }} placeholder={i18nT('pages.channelPage.task_e_g_search_cloudwatch_logs')} aria-label={i18nT('pages.channelPage.task')}
        className="w-full text-[13px]"
        {...ime.bindComposition()}
        onKeyDown={(e) => {
          if (e.key !== 'Enter') return
          // Rule 1: single-line input — the guard alone is enough; claiming would
          // suppress an implicit form submit where one is wanted.
          if (ime.isComposing(e)) return
          submit()
        }} />
      <div className="flex gap-1">
        {/* `aria-disabled` while saving, not `disabled`, so focus stays on the button. */}
        <Btn onClick={submit} disabled={!role.trim()} aria-disabled={save.saving} aria-busy={save.saving}
          primary className="flex-1 aria-disabled:opacity-30 aria-disabled:cursor-not-allowed">{i18nT('pages.channelPage.add')}</Btn>
        <Btn onClick={onCancel}>{i18nT('pages.channelPage.cancel')}</Btn>
      </div>
    </div>
  )
}

function AttachSessionForm({
  attachedSessionKeys,
  onAttach,
  onCancel,
}: {
  attachedSessionKeys: Set<string>
  onAttach: (slot: string) => void
  onCancel: () => void
}) {
  const { data: slots = [] } = useQuery<DashboardSlot[]>({
    queryKey: ['channel-attachable-slots'],
    queryFn: () => api.chatSlots(),
    // The app default is `staleTime: Infinity` — freshness comes from
    // WebSocket pushes invalidating a key. Nothing invalidates THIS key, so
    // the inherited default froze the list at the first fetch of the page
    // load: a session opened afterwards could never be attached, and one
    // closed since kept a row. This form mounts only when the operator opens
    // the picker, so refetching per mount is one request on a deliberate
    // action rather than polling.
    staleTime: 0,
  })
  const attachable = slots.filter(
    (slot) =>
      slot.key && isAttachableSlot(slot) && !attachedSessionKeys.has(`dashboard:${slot.key}`),
  )
  const [selected, setSelected] = useState('')

  useEffect(() => {
    if (!selected && attachable[0]) setSelected(attachable[0].key)
  }, [attachable, selected])

  return (
    <div className="p-2 space-y-2 border-t border-border">
      <label htmlFor="channel-session-picker" className="text-[11px] text-muted font-medium block">
        {i18nT('pages.chatSidebar.sessions')}
      </label>
      <SimpleSelect
        id="channel-session-picker"
        aria-label={i18nT('pages.chatSidebar.sessions')}
        options={attachable.map((slot) => slot.key)}
        optionLabels={attachable.map(attachableSlotLabel)}
        value={selected}
        onChange={setSelected}
        className="w-full"
      />
      <div className="flex gap-1">
        <Btn
          onClick={() => {
            if (selected) onAttach(selected)
          }}
          disabled={!selected}
          primary
          className="flex-1"
        >
          {i18nT('pages.channelPage.add')}
        </Btn>
        <Btn onClick={onCancel}>{i18nT('pages.channelPage.cancel')}</Btn>
      </div>
    </div>
  )
}

// ── Sidebar ──

/** One "Approve all" per coordinator with two or more pending requests (a changeover). */
function AttachRequestBatches({
  requests,
  roleOf,
  onApproveAll,
}: {
  requests: AttachRequest[]
  roleOf: (memberId: string) => string
  onApproveAll: (batch: AttachRequest[]) => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const batches = new Map<string, AttachRequest[]>()
  for (const req of requests) {
    batches.set(req.requestedBy, [...(batches.get(req.requestedBy) ?? []), req])
  }
  const multi = [...batches].filter(([, batch]) => batch.length > 1)
  if (multi.length === 0) return null
  return (
    <div className="mb-2 flex flex-wrap gap-2">
      {multi.map(([requester, batch]) => (
        <Btn
          key={requester}
          primary
          disabled={busy}
          onClick={async () => {
            setBusy(true)
            try {
              await onApproveAll(batch)
            } finally {
              setBusy(false)
            }
          }}
        >
          {i18nT('pages.channelPage.attach_request_approve_all', {
            coordinator: roleOf(requester),
            count: batch.length,
          })}
        </Btn>
      ))}
    </div>
  )
}

/** A coordinator's request to attach a session; the user's click is the grant. */
function AttachRequestCard({
  request,
  coordinator,
  onResolve,
}: {
  request: AttachRequest
  coordinator: string
  onResolve: (action: 'approve' | 'decline') => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const resolve = async (action: 'approve' | 'decline') => {
    setBusy(true)
    try {
      await onResolve(action)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div
      className="mb-2 rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm text-text"
      data-testid="channel-attach-request"
    >
      <div className="font-medium">
        {i18nT('pages.channelPage.attach_request_title', {
          coordinator: coordinator || request.requestedBy,
          session: request.title,
          role: request.role,
        })}
      </div>
      {request.reason && <div className="mt-1 text-muted">{request.reason}</div>}
      <div className="mt-2 flex gap-2">
        <Btn primary disabled={busy} onClick={() => void resolve('approve')}>
          {i18nT('pages.channelPage.attach_request_approve')}
        </Btn>
        <Btn disabled={busy} onClick={() => void resolve('decline')}>
          {i18nT('pages.channelPage.attach_request_decline')}
        </Btn>
      </div>
    </div>
  )
}

function ChannelListItem({
  ch,
  active,
  onClick,
}: {
  ch: Channel
  active: boolean
  onClick: () => void
}) {
  const working = ch.agents.filter(
    (a) => a.state === 'working' || a.state === 'tool_running',
  ).length
  return (
    <Btn
      onClick={onClick}
      className={`w-full text-left px-3 py-2.5 !rounded-lg !border-none ${active ? 'bg-accent/15 text-text-strong' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
    >
      <div className="text-sm font-medium truncate">{ch.topic}</div>
      <div className="flex items-center gap-2 mt-1 text-[13px] text-muted">
        <span>{i18nT('pages.channelPage.agent_2', { count: ch.agents.length })}</span>
        {working > 0 && (
          <Badge variant="ok">
            <span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--ok)]" /> {working}{' '}
            {i18nT('pages.channelPage.active')}
          </Badge>
        )}
      </div>
    </Btn>
  )
}

// ── Main Page ──

const apiError = (err: unknown, fallback: string) => {
  const message = err instanceof Error ? err.message : ''
  try {
    return JSON.parse(message)?.error || fallback
  } catch {
    return message || fallback
  }
}

export default function ChannelPage() {
  const [channels, setChannels] = useState<Channel[]>([])
  const [presets, setPresets] = useState<Preset[]>(FALLBACK_PRESETS)
  const [activeId, setActiveId] = useState<string | null>(null)
  // The channel actually ON SCREEN, which is not always `activeId`: when `activeId` names a
  // channel that is gone, the render falls back to `channels[0]`. Compared against the
  // RENDERED id because that is what decides whether a notice would be visible.
  const renderedIdRef = useRef<string | null>(null)
  const isOffscreenClearResult = useCallback(
    (channelId: string) => channelId !== renderedIdRef.current,
    [],
  )
  const [input, setInput] = useState('')
  const [showNew, setShowNew] = useState(false)
  const [showAgents, setShowAgents] = useState(false)
  // One pane at a time while narrow. Both rails here are a fixed w-64, so at
  // 390px the transcript column measured 86px and the message paragraph inside
  // it 2px -- a column that cannot hold one character per line.
  const { isMobile, showList, showDetail, openDetail, closeDetail } = useListDetailView()
  const [showAddAgent, setShowAddAgent] = useState(false)
  const [showAttachSession, setShowAttachSession] = useState(false)
  const [sessionOnlyPickerFor, setSessionOnlyPickerFor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  /** The last channel-list read was refused, so an empty list is unknown, not empty. */
  const [listFailed, setListFailed] = useState(false)
  const [error, setError] = useState<ChannelPageError | null>(null)
  const errorNoticeRef = useRef<HTMLDivElement>(null)
  const clearDoneRef = useRef<HTMLParagraphElement>(null)
  const [clearDone, setClearDone] = useState<{ channelId: string; roles?: string; agentId?: string; messagesDeleted?: boolean } | null>(null)
  const [clearError, setClearError] = useState<
    (ChannelPageError & { channelId?: string; agentId?: string }) | null
  >(null)
  // A rejected channelPost, kept apart from `error`: its notice sits next to
  // the composer that still holds the unsent text (keyed by thread so it shows
  // beside the right one), so it must not offer the agent hand-off `error` does.
  const [postError, setPostError] = useState<{
    message: string
    threadId: string | null
  } | null>(null)
  // Every failed request on this page lands in the one in-page ErrorNotice;
  // `title` names the action, the body is the backend's message.
  const fail = useCallback((titleKey: string, err: unknown, opts?: { keepExisting?: boolean }) => {
    const title = i18nT(titleKey)
    const next = { title, message: apiError(err, title) }
    // `keepExisting`: a secondary read (the team presets) must not paper over
    // the primary one (the channel list) when both fail on the same load.
    setError((prev) => (opts?.keepExisting && prev ? prev : next))
  }, [])
  // A form save that failed after its form was closed: the text is already parsed.
  const failWithMessage = useCallback((titleKey: string, message: string) => {
    setError({ title: i18nT(titleKey), message })
  }, [])
  const noteClearResult = useCallback((channelId: string, res: { cleared?: unknown } | null | undefined, scope: ClearScope = 'all', agentId?: string) => {
    // An agent-scope clear resolves only THAT agent's failed attempt; another agent's
    // failure, and a channel-wide one, are still unresolved and keep their notice.
    setClearError(prev => {
      if (!prev) return null
      if (prev.channelId && prev.channelId !== channelId) return prev
      if (scope === 'agent' && prev.agentId !== agentId) return prev
      return null
    })
    // Silence reads the same as a click that did nothing, so a clean clear must say so --
    // and an agent-scope clear must name what it cleared, or it reads as channel-wide.
    // Guarded like the `setClearError` writer above: without it, a clear still in flight on
    // channel A lands after the user has moved to B and cleared there, and A's late success
    // overwrites B's just-rendered acknowledgment with one addressed to a channel that is
    // not on screen -- so B's line and row mark silently vanish.
    const done = res?.cleared
    setClearDone(prev => isOffscreenClearResult(channelId) ? prev : (
      scope === 'agent' && Array.isArray(done) && done.length > 0
        // The agent's own id travels with the result so the row the user clicked can confirm
        // in place. The page-top line alone put the outcome where the eye was not.
        ? { channelId, roles: fmtList(done as string[]), agentId }
        // All-scope is the ONLY case that also wipes the shared log (`ch.messages.clear()` in
        // the server's scope=all branch), so it is the only one that may say so.
        : { channelId, messagesDeleted: scope !== 'agent' }
    ))
  }, [isOffscreenClearResult])
  const failClearContext = useCallback((channelId: string, err: unknown, agentId?: string) => {
    const failed = i18nT('pages.channelPage.failed_to_clear_context')
    // A request that FAILED stays true after the user navigates away, so it takes the page
    // notice rather than being dropped unreported.
    if (isOffscreenClearResult(channelId)) {
      fail('pages.channelPage.failed_to_clear_context', err)
      return
    }
    // The server's 404 body is a bare "not found", which tells the user neither what is gone
    // nor that retrying cannot help.
    const gone = err instanceof ApiError && err.status === 404
      ? i18nT(agentId ? 'pages.channelPage.clear_context_agent_gone' : 'pages.channelPage.clear_context_channel_gone')
      : undefined
    setClearError({
      title: failed,
      message: gone ?? apiError(err, failed),
      channelId,
      agentId,
    })
  }, [fail, isOffscreenClearResult])

  const [threadId, setThreadId] = useState<string | null>(null)
  // Which thread the unsent reply belongs to, so it is neither discarded on
  // navigation nor inherited by a different thread.
  // Keyed by thread id: a draft belongs to one thread, so switching threads or
  // channels neither discards it nor hands it to a different conversation.
  const [threadDrafts, setThreadDrafts] = useState<Record<string, string>>({})
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const channel = channels.find(c => c.id === activeId) || channels[0] || null
  // Mirrored into a ref because the clear-result callbacks are `[]`-dep and would otherwise
  // close over the channel rendered on first paint.
  useEffect(() => { renderedIdRef.current = channel?.id ?? null }, [channel?.id])
  const topLevelMessages = useMemo(() => channel?.messages.filter(m => !m.threadId) ?? [], [channel?.messages])

  // Load channels on mount
  const reload = useCallback(async () => {
    try {
      const res = await api.channelsList()
      const mapped = (res.channels || []).map(mapChannel)
      setChannels(mapped)
      setListFailed(false)
      if (!activeId && mapped.length > 0) setActiveId(mapped[0].id)
    } catch (e) {
      setListFailed(true)
      fail('pages.channelPage.failed_to_load_channels', e)
    }
    setLoading(false)
  }, [activeId, fail])

  useEffect(() => {
    reload()
    api
      .channelPresets()
      .then((r) => {
        if (r.presets?.length) setPresets(r.presets)
      })
      .catch((e) =>
        fail('pages.channelPage.failed_to_load_presets', e, {
          keepExisting: true,
        }),
      )
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // A thread id and the agents panel both belong to one channel: `threadId` names a
  // message in it, and the panel lists its members. Leaving either set across a change
  // of channel is not cosmetic -- the thread panel's composer sends against the ACTIVE
  // channel, so a stale id parents a reply to a message that channel does not contain.
  //
  // The shared `error` is deliberately NOT reset here. It carries no channel, so the reset
  // could not be scoped without adding a third scoping mechanism to this page beside
  // `postError` and `clearError`; and it fires on the FIRST selection too, where `reload`
  // assigns `activeId` from null, which erased the presets-load failure raised moments
  // earlier with `keepExisting: true` and left FALLBACK_PRESETS on screen unreported.
  useEffect(() => {
    setThreadId(null)
    setShowAgents(false)
  }, [activeId])

  useEffect(() => {
    if (!sessionOnlyPickerFor || sessionOnlyPickerFor !== activeId) return
    setShowAgents(true)
    setShowAttachSession(true)
    setSessionOnlyPickerFor(null)
  }, [activeId, sessionOnlyPickerFor])

  useEffect(() => {
    if (!clearDone || clearDone.messagesDeleted) return
    const t = setTimeout(() => setClearDone(null), 4000)
    return () => clearTimeout(t)
  }, [clearDone])

  // Keyed on the RENDERED channel, not `activeId`: the two diverge whenever `activeId` names
  // a channel that is gone (a local close, or another client's `channel_closed`), and `channel`
  // falls back to `channels[0]`. Keyed on `activeId`, a failure against the channel actually on
  // screen matched nothing and a destructive action failed with no notice anywhere -- the
  // pre-diff `fail()` path always rendered one. Matches `clearDone` and `clearBusyHere`.
  const shownClearError =
    clearError && (!clearError.channelId || clearError.channelId === channel?.id) ? clearError : null

  useEffect(() => {
    setClearError(prev => (prev?.channelId && prev.channelId !== channel?.id ? null : prev))
  }, [channel?.id])

  useEffect(() => {
    if (!shownClearError) return
    errorNoticeRef.current?.scrollIntoView({ block: 'nearest' })
  }, [shownClearError])

  // A result whose channel is no longer active is not shown, and so must not scroll either.
  useEffect(() => {
    if (!clearDone) return
    clearDoneRef.current?.scrollIntoView({ block: 'nearest' })
  }, [clearDone])

  // Load full channel (with messages) when switching
  useEffect(() => {
    if (!activeId) return
    api
      .channelGet(activeId)
      .then((res) => {
        const full = mapChannel(res)
        setChannels((prev) => prev.map((c) => (c.id === activeId ? full : c)))
      })
      .catch((e) => fail('pages.channelPage.failed_to_load_channel', e))
  }, [activeId, fail])

  // Channel WS events dispatched via existing useWebSocket in shell/appShell.tsx
  // Listen for custom events on window
  useEffect(() => {
    const handler = (e: Event) => {
      const { type, data } = (e as CustomEvent).detail
      if (type === 'channel_message' && data.message) {
        const msg = mapMsg(data.message)
        setChannels((prev) =>
          prev.map((c) =>
            c.id === data.channel_id ? { ...c, messages: [...c.messages, msg] } : c,
          ),
        )
      } else if (type === 'channel_agent_status') {
        setChannels((prev) =>
          prev.map((c) =>
            c.id === data.channel_id
              ? {
                  ...c,
                  agents: c.agents.map((a) =>
                    a.id === data.agent_id ? { ...a, state: data.state } : a,
                  ),
                }
              : c,
          ),
        )
      } else if (type === 'channel_created') {
        const ch = mapChannel(data)
        setChannels((prev) => (prev.some((c) => c.id === ch.id) ? prev : [ch, ...prev]))
      } else if (type === 'channel_closed') {
        setChannels((prev) => prev.filter((c) => c.id !== data.channel_id))
      } else if (type === 'channel_agent_joined') {
        reload()
      } else if (type === 'channel_attach_requests') {
        setChannels((prev) =>
          prev.map((c) =>
            c.id === data.channel_id
              ? { ...c, attachRequests: (data.requests || []).map(mapAttachRequest) }
              : c,
          ),
        )
      } else if (type === 'channel_agent_left') {
        setChannels((prev) =>
          prev.map((c) =>
            c.id === data.channel_id
              ? {
                  ...c,
                  agents: c.agents.filter((a) => a.id !== data.agent_id),
                }
              : c,
          ),
        )
      } else if (type === 'channel_coordinator_changed') {
        setChannels((prev) =>
          prev.map((c) =>
            c.id === data.channel_id
              ? {
                  ...c,
                  agents: c.agents.map((a) => ({
                    ...a,
                    isCoordinator: a.id === data.agent_id,
                  })),
                }
              : c,
          ),
        )
      } else if (type === 'channel_context_cleared' && data.scope === 'all') {
        // Another client cleared shared context — drop our stale message buffer.
        setChannels((prev) =>
          prev.map((c) => (c.id === data.channel_id ? { ...c, messages: [] } : c)),
        )
      }
    }
    window.addEventListener('kirocrew-channel', handler)
    return () => window.removeEventListener('kirocrew-channel', handler)
  }, [reload])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [channel?.messages.length, activeId])

  // Resolves true only when the post was accepted; callers keep the composer
  // text on false so a rejected send does not also drop the message.
  const sendMessage = async (text: string, tid?: string): Promise<boolean> => {
    if (!text.trim() || !channel) return false
    const msg = text.trim()
    const mentionIds = channel.agents
      .filter((a) => msg.toLowerCase().includes('@' + a.role.toLowerCase()))
      .map((a) => a.id)
    setPostError(null)
    try {
      await api.channelPost(channel.id, msg, mentionIds.length ? mentionIds : undefined, tid)
      return true
    } catch (e) {
      setPostError({
        message: apiError(e, i18nT('pages.channelPage.failed_to_send_message')),
        threadId: tid ?? null,
      })
      return false
    }
  }

  const threadInput = threadId ? (threadDrafts[threadId] ?? '') : ''
  const setThreadInput = (v: string) => {
    if (threadId) setThreadDrafts((d) => ({ ...d, [threadId]: v }))
  }
  const discardThreadDraft = (id: string) => {
    setThreadDrafts((d) => {
      const { [id]: _gone, ...rest } = d
      return rest
    })
  }

  const openThread = (id: string) => {
    setThreadId(id)
    // Exclusivity is a narrow-viewport concern: both overlays are `w-full` there, so
    // opening the second would split the viewport. A desktop shows them side by side
    // and that capability is left alone.
    if (isMobile) setShowAgents(false)
  }

  const handleSend = async () => {
    if (!input.trim()) return
    if (await sendMessage(input)) setInput('')
  }

  // After a refused optimistic change, re-read the channel from the server and
  // let ITS answer replace the row. A hand-rolled rollback cannot tell the
  // optimistic value from one a socket event (`channel_agent_left`) or a
  // newer request wrote in the meantime, so it could resurrect a genuinely
  // removed agent or overwrite a later, successful change. If the re-read
  // fails too, the row is left as is; the notice already names the failure.
  const reconcileChannel = useCallback(async (channelId: string) => {
    try {
      const res = await api.channelGet(channelId)
      setChannels(prev => prev.map(c => c.id === channelId ? mapChannel(res) : c))
    } catch (e) {
      fail('pages.channelPage.failed_to_load_channel', e, { keepExisting: true })
    }
  }, [fail])

  // One entry per outstanding clear, so a per-agent clear settling while the channel-wide one
  // is still open does not re-enable the destructive button.
  const [clearBusyTargets, setClearBusyTargets] = useState<string[]>([])
  const runClear = useCallback(
    async (target: string, scope: ClearScope, agentId?: string) => {
      setClearBusyTargets(prev => [...prev, target])
      try {
        const r = scope === 'agent'
          ? await api.channelClearContext(target, 'agent', agentId)
          : await api.channelClearContext(target, 'all')
        noteClearResult(target, r, scope, agentId)
        await reconcileChannel(target)
        return r
      } catch (e) {
        failClearContext(target, e, agentId)
        return undefined
      } finally {
        // Removes ONE occurrence, not every match: two concurrent clears on the same channel
        // must each be counted, or the first to settle would clear the other's busy state.
        setClearBusyTargets(prev => {
          const at = prev.indexOf(target)
          return at === -1 ? prev : [...prev.slice(0, at), ...prev.slice(at + 1)]
        })
      }
    },
    [noteClearResult, reconcileChannel, failClearContext],
  )

  // Scoped to THIS channel: a clear still running on the channel the user left must not
  // disable the button on the one they moved to, which is a different operation.
  const clearBusyHere = channel?.id !== undefined && clearBusyTargets.includes(channel.id)

  // NOT destructured as `confirm`: that shadows the global in this scope, and the close
  // button's remaining `window.confirm` guard would then test a Promise, which is always truthy.
  const { confirm: askConfirm, cancel: cancelConfirm, confirmDialog } = useConfirm()
  // The channel the open confirm was raised for. `channel` is recomputed every render, so
  // without pinning it a close mid-dialog redirects the confirm onto one nobody chose.
  // A per-agent confirm also pins its agent: a row that leaves mid-dialog withdraws it.
  const pendingClearRef = useRef<{ channelId: string; agentId?: string } | null>(null)
  const clearTargetLive = useCallback((cs: Channel[], channelId: string, agentId?: string) => {
    const ch = cs.find(c => c.id === channelId)
    return !!ch && (!agentId || ch.agents.some(a => a.id === agentId && a.state !== 'done'))
  }, [])
  useEffect(() => {
    const target = pendingClearRef.current
    if (target && !clearTargetLive(channels, target.channelId, target.agentId)) {
      pendingClearRef.current = null
      cancelConfirm()
    }
  }, [channels, cancelConfirm, clearTargetLive])

  const askClearAll = useCallback(async (target: string) => {
    pendingClearRef.current = { channelId: target }
    const ok = await askConfirm({
      title: i18nT('pages.channelPage.clear_all_context'),
      body: i18nT('pages.channelPage.this_will_reset_conversation_history_for_all_age'),
      confirmLabel: i18nT('pages.channelPage.clear_context_and_delete_messages'),
    })
    pendingClearRef.current = null
    // The answer arrives an arbitrary time later, so the target is re-checked rather than
    // trusted: the channel can be gone by now, and clearing its successor was never asked for.
    if (!ok || !channels.some(c => c.id === target)) return
    await runClear(target, 'all')
  }, [channels, askConfirm, runClear])

  const askClearAgent = useCallback(async (channelId: string, agent: ChannelAgent, begin: () => void) => {
    pendingClearRef.current = { channelId, agentId: agent.id }
    const ok = await askConfirm({
      title: i18nT('pages.channelPage.clear_context'),
      body: i18nT('pages.channelPage.reset_role_s_llm_session_the_channel_s_shared_me', { role: agent.role }),
      confirmLabel: i18nT('pages.channelPage.clear_context'),
    })
    pendingClearRef.current = null
    if (!ok || !clearTargetLive(channels, channelId, agent.id)) return
    begin()
    await runClear(channelId, 'agent', agent.id)
  }, [channels, askConfirm, runClear, clearTargetLive])

  // Optimistic, but a refusal reconciles the row from the server: a notice that
  // says "Failed to dismiss agent" beside a row that shows it dismissed would
  // contradict itself until the next reload.
  const handleDismiss = async (agentId: string) => {
    if (!channel) return
    try {
      const result = await api.channelDismissAgent(channel.id, agentId)
      if (!result.ok) return
      setChannels((prev) =>
        prev.map((c) =>
          c.id !== channel.id
            ? c
            : {
                ...c,
                agents: c.agents.filter((a) => a.id !== agentId),
              },
        ),
      )
    } catch (e) {
      // The server remains the source of truth; the member is retained and the
      // refusal is named rather than passing for a completed dismissal.
      fail('pages.channelPage.failed_to_dismiss_agent', e)
    }
  }

  const handleListenChange = async (agentId: string, mode: ChannelAgent['listenMode']) => {
    if (!channel) return
    setChannels((prev) =>
      prev.map((c) =>
        c.id !== channel.id
          ? c
          : {
              ...c,
              agents: c.agents.map((a) => (a.id === agentId ? { ...a, listenMode: mode } : a)),
            },
      ),
    )
    try {
      await api.channelUpdateAgent(channel.id, agentId, { listen: mode })
    } catch (e) {
      await reconcileChannel(channel.id)
      fail('pages.channelPage.failed_to_update_agent', e)
    }
  }

  if (loading)
    return (
      <div className="flex items-center justify-center h-full text-muted">
        {i18nT('pages.channelPage.loading_channels')}
      </div>
    )

  // The dialog stays open until the create lands (it then closes itself):
  // closing it first unmounted the typed topic and preset, so a refused create
  // threw them away.
  const handleCreateChannel = async (topic: string, presetId: string): Promise<string | null> => {
    const tmpl = presets.find(p => p.id === presetId) || presets[0]
    const sessionOnly = tmpl?.agents.length === 0
    try {
      const res = await api.channelCreate(
        topic,
        (tmpl?.agents || []).map((a) => ({
          role: a.role,
          task: channelTaskPrompt(a.task, topic),
          is_orchestrator: a.is_orchestrator || false,
        })),
        sessionOnly,
      )
      if (res.channel) {
        const ch = mapChannel(res.channel)
        setChannels((prev) => (prev.some((c) => c.id === ch.id) ? prev : [ch, ...prev]))
        setActiveId(res.channel.id)
        openDetail()
        if (sessionOnly) {
          setSessionOnlyPickerFor(res.channel.id)
        }
      }
      return null
    } catch (err) {
      return apiError(err, i18nT('pages.channelPage.failed_to_create_channel'))
    }
  }

  return (
    <>
      <PageHeader
        title={i18nT('pages.channelPage.channels')}
        subtitle={i18nT('pages.channelPage.multi_agent_collaboration_spaces')}
      />
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {/* Failed create / add-agent / clear-context / load requests. Every input
            behind these was already submitted (or never existed), so the hand-off
            risks nothing; the composer's own failure renders beside it instead.
            `apiError` falls back to the title when the backend sent no text, so
            the title is dropped rather than shown twice. */}
        <ErrorNotice
          title={error && error.message !== error.title ? error.title : undefined}
          message={error?.message}
          onDismiss={() => setError(null)}
          askAgent
          className="mb-2"
          testId="channel-error"
        />
      {/* No hand-off: the unsent message text (`input`) and thread replies (`threadDrafts`) */}
      <div ref={errorNoticeRef}>
        <ErrorNotice
          title={shownClearError && shownClearError.message !== shownClearError.title ? shownClearError.title : undefined}
          message={shownClearError?.message}
          onDismiss={() => setClearError(null)}
          className="mb-2"
          testId="clear-context-error"
        />
      </div>
        <div className={`flex h-full relative ${isMobile ? '-mx-4 -mb-8' : ''}`}>
          {showNew && (
            <NewChannelDialog
              onClose={() => setShowNew(false)}
              presets={presets}
              onCreate={handleCreateChannel}
              onClosedError={message => failWithMessage('pages.channelPage.failed_to_create_channel', message)}
            />
          )}

          {/* Channel list sidebar */}
          <div
            className={`flex flex-col ${showList ? '' : 'hidden'} ${isMobile ? 'w-full' : 'w-64 shrink-0 border-r border-border'}`}
          >
            <div className="px-3 py-3 border-b border-border flex items-center justify-between">
              <span className="text-sm font-semibold text-text-strong">
                {i18nT('pages.channelPage.channels')}
              </span>
              <Btn
                onClick={() => setShowNew(true)}
                primary
                title={i18nT('pages.channelPage.new_channel_2')}
              >
                {i18nT('pages.channelPage.new')}
              </Btn>
            </div>
            <div className="flex-1 overflow-y-auto p-2 space-y-1">
              {/* An empty list is only "no channels yet" when the read succeeded:
                  under a load failure the onboarding copy would claim zero channels. */}
              {channels.length === 0 && !listFailed && (
                <EmptyState
                  icon={<MessageSquare className="lucide-inline" />}
                  title={i18nT('pages.channelPage.no_channels_yet')}
                />
              )}
              {channels.map((ch) => (
                <ChannelListItem
                  key={ch.id}
                  ch={ch}
                  active={ch.id === activeId}
                  onClick={() => {
                    setActiveId(ch.id)
                    openDetail()
                  }}
                />
              ))}
            </div>
            {/* Above the channel layout this line displaced the header itself,
                so every success dropped the controls by its own height. */}

          </div>

          {/* Channel content */}
          {channel ? (
            <div className={`flex-1 flex-col min-w-0 ${showDetail ? 'flex' : 'hidden'}`}>
            {clearDone && channel.id === clearDone.channelId && (
              <p
                ref={clearDoneRef}
                role="status"
                className={
                  clearDone.messagesDeleted
                    ? 'basis-full flex items-center gap-1.5 text-[13px] text-text-strong font-medium'
                    : 'basis-full text-[12px] text-muted'
                }
                data-testid="clear-context-done"
              >
                {clearDone.roles
                  ? i18nT('pages.channelPage.clear_context_cleared_roles', { roles: clearDone.roles })
                  : clearDone.messagesDeleted
                    ? `${i18nT('pages.channelPage.clear_context_done')} ${i18nT('pages.channelPage.clear_context_messages_deleted')}`
                    : i18nT('pages.channelPage.clear_context_done')}
                {clearDone.messagesDeleted && (
                  <Btn
                    onClick={() => setClearDone(null)}
                    aria-label={i18nT('pages.channelPage.dismiss')}
                    title={i18nT('pages.channelPage.dismiss')}
                    data-testid="clear-context-done-dismiss"
                  >
                    <X className="lucide-inline" />
                  </Btn>
                )}
              </p>
            )}
              <div className="border-b border-border px-4 py-2.5 flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
                <div className="flex flex-1 items-center gap-1 min-w-0 basis-full sm:basis-auto">
                  {isMobile && (
                    <ListDetailBack
                      label={i18nT('pages.channelPage.channels')}
                      onBack={closeDetail}
                    />
                  )}
                  <h2 className="text-base font-semibold text-text-strong truncate min-w-0">
                    {channel.topic}
                  </h2>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  <Btn
                    onClick={() => {
                      setShowAgents((v) => !v)
                      if (isMobile) setThreadId(null)
                    }}
                  >
                    <Users className="lucide-inline" />{' '}
                    {i18nT('pages.channelPage.agent_2', {
                      count: channel.agents.length,
                    })}
                    {channel.agents.some(
                      (a) => a.state === 'working' || a.state === 'tool_running',
                    ) && <Badge variant="ok">●</Badge>}
                  </Btn>
              {/* `aria-disabled` plus an early return, NOT `disabled`: the dialog's focus trap
                  restores focus to this button on unmount, and its FOCUSABLE selector excludes
                  `button:not([disabled])`. Closing the modal commits the close and the busy flip
                  together, so a truly disabled button made that restore a silent no-op and left a
                  keyboard user on `<body>`. `aria-disabled` announces the same state while
                  keeping the element focusable. */}
              <Btn onClick={() => { if (clearBusyHere) return; void askClearAll(channel.id) }}
                className="aria-disabled:opacity-30 aria-disabled:cursor-not-allowed aria-disabled:hover:border-border aria-disabled:hover:bg-transparent"
                aria-disabled={clearBusyHere} aria-busy={clearBusyHere} title={i18nT('pages.channelPage.clear_all_context')}>
                {clearBusyHere
                  ? <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
                  : <RotateCcw className="lucide-inline" />} {i18nT('pages.channelPage.clear_context_2')}
              </Btn>
                  <Btn
                    onClick={async () => {
                      if (
                        !confirm(
                          i18nT(
                            'pages.channelPage.close_this_channel_all_agents_will_be_dismissed',
                          ),
                        )
                      )
                        return
                      // A refused close keeps the channel in the list: removing
                      // it would show the action as done under a notice saying
                      // it failed.
                      try {
                        await api.channelClose(channel.id)
                      } catch (e) {
                        fail('pages.channelPage.failed_to_close_channel', e)
                        return
                      }
                      setChannels((prev) => prev.filter((c) => c.id !== channel.id))
                      setActiveId(null)
                      // Without this the narrow layout keeps the transcript pane while no
                      // channel exists: the list holding "+ New" stays hidden and the Back
                      // control unmounted with the channel, leaving no way out.
                      closeDetail()
                    }}
                    danger
                    title={i18nT('pages.channelPage.close_channel')}
                  >
                    <X className="lucide-inline" /> {i18nT('pages.channelPage.close')}
                  </Btn>
                </div>
              </div>

              <div className="flex flex-1 min-h-0">
                <div
                  className={`flex-1 overflow-y-auto py-3 space-y-1 ${isMobile ? 'px-0' : 'px-2'} ${isMobile && (showAgents || threadId) ? 'hidden' : ''}`}
                >
                  {topLevelMessages.length === 0 && (
                    <EmptyState
                      icon={<Zap className="lucide-inline" />}
                      title={i18nT('pages.channelPage.setting_up_channel')}
                      subtitle={i18nT('pages.channelPage.agent_joining', {
                        count: channel.agents.length,
                      })}
                    />
                  )}
                  {topLevelMessages.map((msg) => (
                    <MessageBubble
                      key={msg.id}
                      msg={msg}
                      agents={channel.agents}
                      onReply={() => openThread(msg.id)}
                      onOpenThread={() => openThread(msg.id)}
                      onApprove={
                        msg.msgType === 'approval'
                          ? (action, pattern) =>
                              api.channelApproveAgent(channel.id, msg.fromId, action, pattern)
                          : undefined
                      }
                    />
                  ))}
                  {channel.agents
                    .filter((a) => a.state === 'working' || a.state === 'tool_running')
                    .map((a) => (
                      <div
                        key={a.id + '-typing'}
                        className="flex items-center gap-2 px-3 py-1.5 text-[13px] text-muted animate-pulse"
                      >
                        <Badge variant="ok">
                          {a.state === 'tool_running' ? <Wrench className="lucide-inline" /> : '●'}
                        </Badge>{' '}
                        <span className="font-medium">{a.role}</span>{' '}
                        {a.state === 'tool_running'
                          ? i18nT('pages.channelPage.running_tool')
                          : i18nT('pages.channelPage.is_working')}
                      </div>
                    ))}
                  <div ref={messagesEndRef} />
                </div>

                {/* Thread panel */}
                <AnimatePresence>
                  {threadId &&
                    (() => {
                      const parent = channel.messages.find((m) => m.id === threadId)
                      const replies = channel.messages.filter((m) => m.threadId === threadId)
                      return (
                        <DetailPanel
                          key="thread-panel"
                          title={i18nT('pages.channelPage.thread')}
                          onClose={() => setThreadId(null)}
                          initialWidth={320}
                          minWidth={260}
                          storageKey="mc-channel-thread-width"
                          footer={
                            <>
                              {/* No hand-off: the unsent thread reply (threadDrafts[threadId]) */}
                              <ErrorNotice
                                title={i18nT('pages.channelPage.failed_to_send_message')}
                                message={
                                  postError?.threadId === threadId ? postError.message : null
                                }
                                onDismiss={() => setPostError(null)}
                                className="mb-2"
                              />
                              <MentionInput
                                agents={channel.agents}
                                value={threadInput}
                                onChange={setThreadInput}
                                onSend={async () => {
                                  if (!threadInput.trim() || !threadId) return
                                  if (await sendMessage(threadInput, threadId))
                                    discardThreadDraft(threadId)
                                }}
                              />
                            </>
                          }
                        >
                          <div className="flex flex-col gap-1 -mx-3 -mt-2">
                            {parent && (
                              <MessageBubble
                                key={parent.id}
                                msg={parent}
                                agents={channel.agents}
                                onApprove={
                                  parent.msgType === 'approval'
                                    ? (action, pattern) =>
                                        api.channelApproveAgent(
                                          channel.id,
                                          parent.fromId,
                                          action,
                                          pattern,
                                        )
                                    : undefined
                                }
                              />
                            )}
                            {replies.length > 0 && <div className="border-t border-border my-2" />}
                            {replies.map((msg) => (
                              <MessageBubble
                                key={msg.id}
                                msg={msg}
                                agents={channel.agents}
                                onApprove={
                                  msg.msgType === 'approval'
                                    ? (action, pattern) =>
                                        api.channelApproveAgent(
                                          channel.id,
                                          msg.fromId,
                                          action,
                                          pattern,
                                        )
                                    : undefined
                                }
                              />
                            ))}
                            {channel.agents
                              .filter((a) => a.state === 'working' || a.state === 'tool_running')
                              .map((a) => (
                                <div
                                  key={a.id + '-typing-t'}
                                  className="flex items-center gap-2 px-2 py-1 text-[13px] text-muted animate-pulse"
                                >
                                  <Badge variant="ok">
                                    {a.state === 'tool_running' ? (
                                      <Wrench className="lucide-inline" />
                                    ) : (
                                      '●'
                                    )}
                                  </Badge>{' '}
                                  <span className="font-medium">{a.role}</span>{' '}
                                  {a.state === 'tool_running'
                                    ? i18nT('pages.channelPage.running_tool')
                                    : i18nT('pages.channelPage.is_working')}
                                </div>
                              ))}
                          </div>
                        </DetailPanel>
                      )
                    })()}
                </AnimatePresence>

                <AnimatePresence>
                  {showAgents && (
                    <DetailPanel
                      key="channel-agents-panel"
                      title={i18nT('pages.channelPage.agents')}
                      onClose={() => setShowAgents(false)}
                      initialWidth={360}
                      minWidth={300}
                      storageKey="mc-channel-agents-width"
                      noPadding
                      customHeader={
                        <div className="px-3 py-2.5 border-b border-border flex items-center justify-between">
                          <span className="text-sm font-semibold text-text-strong">
                            {i18nT('pages.channelPage.agents')}
                          </span>
                          <Btn
                            onClick={() => setShowAgents(false)}
                            aria-label={i18nT('pages.channelPage.close_agents_panel')}
                            className="!p-0 !border-none !rounded-none text-muted hover:text-text text-sm"
                          >
                            <X className="lucide-inline" />
                          </Btn>
                        </div>
                      }
                    >
                      <div className="flex-1 overflow-y-auto p-2 space-y-1">
                        {channel.agents.map((agent) => (
                          <AgentControlRow
                            key={agent.id}
                            agent={agent}
                            onDismiss={() => handleDismiss(agent.id)}
                            onListenChange={(m) => handleListenChange(agent.id, m)}
                            onSetCoordinator={async () => {
                              try {
                                await api.channelUpdateAgent(channel.id, agent.id, {
                                  coordinator: true,
                                })
                                setChannels((prev) =>
                                  prev.map((c) =>
                                    c.id === channel.id
                                      ? {
                                          ...c,
                                          agents: c.agents.map((a) => ({
                                            ...a,
                                            isCoordinator: a.id === agent.id,
                                          })),
                                        }
                                      : c,
                                  ),
                                )
                              } catch (err) {
                                setError(
                                  apiError(
                                    err,
                                    i18nT('pages.channelPage.failed_to_set_coordinator'),
                                  ),
                                )
                              }
                            }}
                            justCleared={clearDone?.channelId === channel.id && clearDone.agentId === agent.id}
                            onClearContext={begin => askClearAgent(channel.id, agent, begin)}
                          />
                        ))}
                      </div>
                      <div className="p-2 border-t border-border">
                        {showAddAgent ? (
                          <AddAgentForm
                            onCancel={() => setShowAddAgent(false)}
                            onClosedError={message => failWithMessage('pages.channelPage.failed_to_add_agent', message)}
                            onAdd={async (role, task, agent) => {
                              try {
                                await api.channelAddAgent(channel.id, {
                                  role,
                                  task: task || channel.topic,
                                  agent,
                                })
                              } catch (err) {
                                return apiError(err, i18nT('pages.channelPage.failed_to_add_agent'))
                              }
                              return null
                            }}
                          />
                        ) : showAttachSession ? (
                          <AttachSessionForm
                            attachedSessionKeys={
                              new Set(
                                channel.agents
                                  .map((agent) => agent.sessionKey)
                                  .filter(Boolean) as string[],
                              )
                            }
                            onCancel={() => setShowAttachSession(false)}
                            onAttach={async (slot) => {
                              try {
                                await api.channelAttachSession(channel.id, slot)
                                const res = await api.channelGet(channel.id)
                                setChannels((prev) =>
                                  prev.map((c) => (c.id === channel.id ? mapChannel(res) : c)),
                                )
                                setShowAttachSession(false)
                              } catch (err) {
                                fail('pages.channelPage.failed_to_add_agent', err)
                              }
                            }}
                          />
                        ) : (
                          <div className="flex gap-1">
                            <Btn onClick={() => setShowAddAgent(true)} primary className="flex-1">
                              {i18nT('pages.channelPage.add_agent')}
                            </Btn>
                            <Btn onClick={() => setShowAttachSession(true)} className="flex-1">
                              {i18nT('pages.chatSidebar.sessions')}
                            </Btn>
                          </div>
                        )}
                      </div>
                    </DetailPanel>
                  )}
                </AnimatePresence>
              </div>

              <div
                className={`border-t border-border px-4 py-3 ${isMobile && (threadId || showAgents) ? 'hidden' : ''}`}
              >
                <AttachRequestBatches
                  requests={channel.attachRequests}
                  roleOf={(id) => channel.agents.find((a) => a.id === id)?.role ?? id}
                  onApproveAll={async (batch) => {
                    // One request at a time through the same route as a single
                    // click, so each approval re-runs every server-side check.
                    for (const req of batch) {
                      try {
                        await api.channelResolveAttachRequest(channel.id, req.id, 'approve')
                      } catch (err) {
                        fail('pages.channelPage.failed_to_resolve_attach_request', err)
                      }
                    }
                    reload()
                  }}
                />
                {channel.attachRequests.map((req) => (
                  <AttachRequestCard
                    key={req.id}
                    request={req}
                    coordinator={
                      channel.agents.find((a) => a.id === req.requestedBy)?.role ?? ''
                    }
                    onResolve={async (action) => {
                      try {
                        await api.channelResolveAttachRequest(channel.id, req.id, action)
                      } catch (err) {
                        fail('pages.channelPage.failed_to_resolve_attach_request', err)
                      } finally {
                        reload()
                      }
                    }}
                  />
                ))}
                {/* No hand-off: the unsent message text (`input`) */}
                <ErrorNotice
                  title={i18nT('pages.channelPage.failed_to_send_message')}
                  message={postError && postError.threadId === null ? postError.message : null}
                  onDismiss={() => setPostError(null)}
                  className="mb-2"
                />
                <div className="flex gap-2">
                  <MentionInput
                    agents={channel.agents}
                    value={input}
                    onChange={setInput}
                    onSend={handleSend}
                  />
                  <Btn onClick={handleSend} primary>
                    {i18nT('pages.channelPage.send')}
                  </Btn>
                </div>
              </div>
            </div>
          ) : (
            <div className="flex-1 flex items-center justify-center">
              {!listFailed && (
                <EmptyState
                  icon={<Users className="lucide-inline" />}
                  title={i18nT('pages.channelPage.create_a_channel_to_get_started')}
                />
              )}
            </div>
          )}
        </div>
      </div>
      {confirmDialog}
    </>
  )
}
