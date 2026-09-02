import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Check, Ear, Hourglass, RotateCcw, Wrench, X } from 'lucide-react'
import { api, type RestartBlockerReport, type RestartBlockerResult } from '../api/client'
import { ApiError } from '../api/apiError'
import { Badge, Btn } from './ui'
import { i18nT } from '../i18n/t'

/** The gateway's refusal code when live sessions have not released the reset. */
const ACK_REQUIRED = 'restart_ack_required'

/**
 * Did this rejection come from the coordinated-reset barrier?
 *
 * Lives beside the panel because it is the panel's own entry condition, and it
 * is shared rather than re-tested per caller: every endpoint that drains ACP
 * sessions answers with this ONE code, and a second copy of the test is how one
 * surface starts rendering the refusal as a plain error line while another
 * offers the controls. Any other failure is a plain message, because there is
 * nothing here for an operator to act on.
 */
export function isRestartAckRequired(e: unknown): boolean {
  if (!(e instanceof ApiError) || e.status !== 409) return false
  try {
    return (JSON.parse(e.body) as { code?: unknown })?.code === ACK_REQUIRED
  } catch { return false }
}

/** Worker state → the channel page's own label, so one vocabulary describes a
 *  worker wherever the operator meets it. A literal map (rather than a computed
 *  key) is what keeps these references statically checkable. */
const STATE_LABEL: Record<string, () => string> = {
  pending: () => i18nT('pages.channelPage.state_pending'),
  working: () => i18nT('pages.channelPage.state_working'),
  listening: () => i18nT('pages.channelPage.state_listening'),
  done: () => i18nT('pages.channelPage.state_done'),
  failed: () => i18nT('pages.channelPage.state_failed'),
  tool_running: () => i18nT('pages.channelPage.state_running'),
}

const STATE_ICON: Record<string, React.ReactNode> = {
  pending: <Hourglass className="lucide-inline" />,
  working: <>●</>,
  listening: <Ear className="lucide-inline" />,
  done: <Check className="lucide-inline" />,
  failed: <X className="lucide-inline" />,
  tool_running: <Wrench className="lucide-inline" />,
}

const NO_ACTION_REASON: Record<string, () => string> = {
  not_a_channel_worker: () => i18nT('components.restartBlockers.reason_not_a_channel_worker'),
  attached_dashboard_session: () =>
    i18nT('components.restartBlockers.reason_attached_dashboard_session'),
}

const OUTCOME_LABEL: Record<string, () => string> = {
  cleared: () => i18nT('components.restartBlockers.outcome_cleared'),
  skipped: () => i18nT('components.restartBlockers.outcome_skipped'),
  failed: () => i18nT('components.restartBlockers.outcome_failed'),
}

/**
 * Names what is holding up a coordinated reset, and offers the one safe action
 * an operator has over it.
 *
 * The split is the whole point. A dashboard slot acknowledges for itself
 * through its own agent, so it is listed and left alone; a channel-owned worker
 * has nobody to acknowledge for it, which is what turned it into an anonymous
 * "busy slotless worker" a restart simply refused on. Clearing its context runs
 * the channel's own per-worker lifecycle — the worker keeps its membership and
 * cold-starts on its next message — and the barrier is re-read after the batch,
 * so what is still blocking is measured rather than assumed.
 */
export default function RestartBlockers({ onCleared }: { onCleared?: () => void }) {
  const [report, setReport] = useState<RestartBlockerReport | null>(null)
  const [results, setResults] = useState<RestartBlockerResult[]>([])
  const [confirming, setConfirming] = useState(false)
  const [clearing, setClearing] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      setReport(await api.restartBlockers())
      setError('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('components.restartBlockers.load_failed'))
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const workers = report?.channel_blockers ?? []
  const others = report?.other_blockers ?? []
  const pending = report?.maintenance?.pending ?? []

  const clear = async () => {
    setClearing(true)
    try {
      const next = await api.clearRestartBlockers(workers.map(w => w.session_key))
      setResults(next.results)
      setReport(next)
      setError('')
      onCleared?.()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('components.restartBlockers.clear_failed'))
    } finally {
      setClearing(false)
      setConfirming(false)
    }
  }

  if (!report) {
    return error
      ? <div className="text-[13px] text-danger" data-testid="restart-blockers-error">{error}</div>
      : null
  }

  // An emptied barrier still owes the operator the outcome of the batch that
  // emptied it: collapsing straight to "nothing is blocking" would hide a
  // worker that was skipped rather than cleared.
  const outcomes = results.length > 0 && (
    <div className="space-y-1" data-testid="restart-blockers-results">
      {results.map(row => (
        <div key={row.session_key} className="text-[12px] break-all">
          <span className="font-mono text-text">{row.role || row.session_key}</span>
          {' — '}
          <span className={row.outcome === 'failed' ? 'text-danger' : 'text-muted'}>
            {OUTCOME_LABEL[row.outcome]?.() ?? row.outcome}
          </span>
          {row.detail ? ` (${row.detail})` : ''}
        </div>
      ))}
    </div>
  )

  if (!workers.length && !others.length && !pending.length) {
    return (
      <div className="mt-2 space-y-1 text-left" data-testid="restart-blockers-empty">
        <div className="text-[13px] text-muted">
          {i18nT('components.restartBlockers.nothing_blocking')}
        </div>
        {outcomes}
      </div>
    )
  }

  return (
    <div className="mt-2 p-3 rounded-lg border border-border bg-bg-elevated space-y-3 text-left"
      data-testid="restart-blockers">
      <div className="flex items-center gap-1.5 text-sm font-semibold text-text-strong">
        <AlertTriangle size={14} className="lucide-inline" />
        {i18nT('components.restartBlockers.title')}
      </div>

      {pending.length > 0 && (
        <div className="space-y-1" data-testid="restart-blockers-pending">
          <div className="text-[12px] font-medium text-muted">
            {i18nT('components.restartBlockers.pending_sessions')}
          </div>
          {pending.map(key => (
            <div key={key} className="text-[13px] text-text font-mono break-all">{key}</div>
          ))}
        </div>
      )}

      {workers.length > 0 && (
        <div className="space-y-1.5" data-testid="restart-blockers-workers">
          <div className="text-[12px] font-medium text-muted">
            {i18nT('components.restartBlockers.channel_workers')}
          </div>
          {workers.map(worker => (
            <div key={worker.session_key} className="flex items-start justify-between gap-2"
              data-testid={`restart-blocker-worker-${worker.session_key}`}>
              <div className="min-w-0">
                <div className="text-[13px] text-text break-words">{worker.role}</div>
                <div className="text-[12px] text-muted break-words">{worker.channel_topic}</div>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                {worker.is_coordinator && (
                  <Badge variant="aim">{i18nT('pages.channelPage.coordinator')}</Badge>
                )}
                <Badge variant={worker.state === 'failed' ? 'err' : 'ok'}>
                  {STATE_ICON[worker.state] ?? null} {STATE_LABEL[worker.state]?.() ?? worker.state}
                </Badge>
              </div>
            </div>
          ))}
          {/* Said before the button, not after it: the operator is choosing
              between three things that all look like "make it stop", and only
              one of them is on offer here. */}
          <p className="text-[12px] text-muted">
            {i18nT('components.restartBlockers.not_a_dismissal')}
          </p>
          {confirming ? (
            <div className="flex items-center gap-1.5">
              <Btn onClick={clear} primary disabled={clearing}
                data-testid="restart-blockers-confirm">
                {clearing
                  ? i18nT('components.restartBlockers.clearing')
                  : i18nT('components.restartBlockers.confirm_clear')}
              </Btn>
              <Btn onClick={() => setConfirming(false)} disabled={clearing}
                data-testid="restart-blockers-cancel">
                {i18nT('components.restartBlockers.cancel')}
              </Btn>
            </div>
          ) : (
            <Btn onClick={() => setConfirming(true)} data-testid="restart-blockers-clear">
              <RotateCcw className="lucide-inline" /> {i18nT('components.restartBlockers.clear_context')}
            </Btn>
          )}
        </div>
      )}

      {others.length > 0 && (
        <div className="space-y-1" data-testid="restart-blockers-other">
          <div className="text-[12px] font-medium text-muted">
            {i18nT('components.restartBlockers.other_blockers')}
          </div>
          {others.map(row => (
            <div key={row.session_key} className="text-[12px] text-muted break-all">
              <span className="font-mono text-text">{row.session_key}</span>
              {' — '}
              {NO_ACTION_REASON[row.reason]?.() ?? row.reason}
            </div>
          ))}
        </div>
      )}

      {outcomes}

      {error && (
        <div className="text-[12px] text-danger" data-testid="restart-blockers-error">{error}</div>
      )}
    </div>
  )
}
