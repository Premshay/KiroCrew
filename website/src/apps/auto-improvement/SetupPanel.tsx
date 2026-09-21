// Repository setup + run control for Auto-Improvement.
//
// This is the front door the user reported missing: without it there is no way
// to say WHICH repository the loop should work on. The flow is deliberately
// three explicit steps rather than one opaque field, because each step has a
// real failure mode worth surfacing on its own:
//   1. paste a GitHub URL -> the backend validates + clones it PUSH-DISABLED
//      (POST /setup-clone). A bad URL or a clone that cannot be made push-safe
//      fails HERE, before anything else is configured.
//   2. pick a base branch -> the picker only offers branches that actually
//      exist in the clone (GET /branches), so a run can never target a typo.
//   3. start the run -> POST /run; live status polls GET /run.
//
// All colors come from the app's design tokens (accent / warn / muted / border)
// via Tailwind utilities — never a hardcoded hex — so the panel matches whatever
// theme the dashboard is on.
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FolderGit2, GitBranch, Loader2, Play, Square } from 'lucide-react'

import ErrorNotice from '../../components/ErrorNotice'
import TestEnvironmentPanel, { requestJson } from './TestEnvironmentPanel'

import SimpleSelect from '../../components/SimpleSelect'
import { Badge, Btn, Card, CardTitle, Input } from '../../components/ui'
import { fmtDateFields } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useImeGuard } from '../../hooks/useImeGuard'

const API = '/api/apps/auto-improvement'

interface CloneResult {
  ok: boolean
  error?: string
  clone?: { display: string; push_disabled: boolean }
  config?: Record<string, unknown>
}
/** One line of the run's activity feed.
 *
 * The backend emits four different shapes on this channel — a plain note, a
 * stage transition, a stage transition carrying discovery counts, and a nested
 * agent event — so this is a union, not a string. Getting that wrong is what
 * crashed the page: React cannot render `{t, agent}` as a child, and the
 * minified failure (React #31) reports only "object with keys {t, agent}".
 */
interface ActivityItem {
  t?: number
  note?: string
  error?: string
  stage?: string
  cycle?: number
  discovered?: number
  fresh?: number
  agent?: { kind?: string; tool?: string; detail?: string }
  proposers?: Record<string, number>
  budget?: Record<string, number>
}

interface RunStatus {
  stats?: { publication?: string }
  status: string
  run_id?: string
  cycle?: number
  kept?: number
  drafted?: number
  activity?: ActivityItem[]
  error?: string
}

/** Render one activity item as a single readable line. Exported for tests. */
export function activityLine(a: ActivityItem): string {
  // These read the app language; toLocaleTimeString() would follow the BROWSER locale
  // and print en-US clock times inside a non-English dashboard. Explicit h/m/s fields
  // rather than fmtTime: the activity feed is read at second precision, and the
  // `timeStyle` preset that would give seconds is deliberately not on fmtTime's type.
  const time = a.t
    ? fmtDateFields(a.t * 1000, { hour: 'numeric', minute: '2-digit', second: '2-digit' })
    : ''
  const body = (() => {
    if (a.error) return `error: ${a.error}`
    if (a.note) return a.note
    if (a.agent) {
      const { kind, tool, detail } = a.agent
      const label = tool ? `${kind ?? 'agent'}:${tool}` : (kind ?? 'agent')
      return detail ? `${label} — ${detail}` : label
    }
    if (a.stage) {
      const extra =
        a.discovered !== undefined
          ? ' ' +
            i18nT('autoImprovement.activityDiscovered', {
              discovered: a.discovered,
              fresh: a.fresh ?? 0,
            })
          : ''
      return `cycle ${a.cycle ?? '?'} · ${a.stage}${extra}`
    }
    // An unknown future shape must degrade to something honest, never crash.
    return JSON.stringify(a)
  })()
  return time ? `${time}  ${body}` : body
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  return requestJson<T>(path, body)
}

export default function SetupPanel({ config }: { config?: Record<string, unknown> }) {
  const repo = String(config?.target_url || config?.clone || '')
  return <RepositorySetup key={repo} config={config} repo={repo} />
}

function RepositorySetup({ config, repo }: { config?: Record<string, unknown>; repo: string }) {
  const ime = useImeGuard()
  const qc = useQueryClient()
  const configured = Boolean(config?.clone)
  const display = String(config?.target_display || config?.target_url || '')

  const [url, setUrl] = useState('')
  const [branch, setBranch] = useState(String(config?.branch || ''))

  const configBranch = String(config?.branch || '')
  useEffect(() => {
    setBranch(configBranch)
  }, [configBranch])

  // Branch list only makes sense once a clone exists; the query is gated on it.
  const { data: branchResp } = useQuery({
    queryKey: ['auto-improvement-branches', repo],
    queryFn: () => fetch(`${API}/branches`).then((r) => (r.ok ? r.json() : { branches: [] })),
    enabled: configured,
  })
  const branches: string[] = branchResp?.branches ?? []

  const clone = useMutation({
    mutationFn: (u: string) => postJson<CloneResult>('/setup-clone', { url: u }),
    onSuccess: (r) => {
      if (r.ok) {
        qc.invalidateQueries({ queryKey: ['auto-improvement-config'] })
        qc.invalidateQueries({ queryKey: ['auto-improvement-branches'] })
      }
    },
  })

  // One mutation for any allowlisted config key. The backend rejects anything
  // not on its allowlist (clone/target_url can only move through setup-clone), so
  // this cannot be used to retarget the repository.
  const saveConfig = useMutation({
    mutationFn: (patch: Record<string, unknown>) =>
      requestJson('/config', patch, 'PUT'),
    onSuccess: () => Promise.all([
      qc.invalidateQueries({ queryKey: ['auto-improvement-config'] }),
      qc.invalidateQueries({ queryKey: ['auto-improvement-ruler'] }),
    ]),
  })

  const directCommit = Boolean(config?.directCommit)

  const { data: run, error: runError } = useQuery({
    queryKey: ['auto-improvement-run'],
    queryFn: () => requestJson<RunStatus>('/run', undefined, 'GET'),
    refetchInterval: (q) => (['running', 'calibrating', 'stopping'].includes(q.state.data?.status ?? '') ? 3000 : 15000),
  })
  const running = run?.status === 'running'
  const active = ['running', 'calibrating', 'stopping'].includes(run?.status ?? '')
  useEffect(() => {
    qc.invalidateQueries({ queryKey: ['auto-improvement-ruler'] })
  }, [qc, run?.status])
  const [environmentBusy, setEnvironmentBusy] = useState(false)

  const startRun = useMutation({
    mutationFn: () => postJson<RunStatus>('/run', {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['auto-improvement-run'] }),
  })
  const stopRun = useMutation({
    mutationFn: () => postJson<RunStatus>('/run/stop', {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['auto-improvement-run'] }),
  })

  const locked = active || !run || Boolean(runError) || startRun.isPending || stopRun.isPending
  const setupBusy = locked || clone.isPending || saveConfig.isPending

  return (
    <Card>
      <CardTitle className="flex items-center gap-2">
        <FolderGit2 className="lucide-inline" size={15} />
        {i18nT('autoImprovement.setupHeading')}
      </CardTitle>

      {/* Step 1 — choose the repository */}
      <div className="flex flex-col gap-2">
        <label className="text-[13px] text-muted">{i18nT('autoImprovement.repoLabel')}</label>
        <div className="flex items-center gap-2">
          <Input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder={i18nT('apps.autoImprovement.setupPanel.https_github_com_owner_repo')}
            className="flex-1"
            aria-label={i18nT('autoImprovement.repoLabel')}
            {...ime.bindEnter({ onEnter: () => { if (url.trim() && !setupBusy && !environmentBusy) clone.mutate(url.trim()) } })}
          />
          <Btn
            primary
            aria-label={i18nT('autoImprovement.cloneBtn')}
            disabled={!url.trim() || setupBusy || environmentBusy}
            onClick={() => clone.mutate(url.trim())}
          >
            {clone.isPending ? (
              <Loader2 className="lucide-inline animate-spin" size={14} />
            ) : (
              i18nT('autoImprovement.cloneBtn')
            )}
          </Btn>
        </div>
        {/* No hand-off: the repository URL and environment draft are unsaved. */}
        <ErrorNotice message={clone.error?.message || saveConfig.error?.message || startRun.error?.message || stopRun.error?.message || runError?.message} messageClassName="max-h-40 overflow-auto whitespace-pre-wrap break-words select-text" />
        {configured ? (
          <p className="text-[12px] text-muted">
            {i18nT('autoImprovement.currentRepo')} <span className="text-accent">{display}</span>
            {config?.clone ? (
              <Badge variant="ok" className="ml-2">
                {i18nT('autoImprovement.pushDisabled')}
              </Badge>
            ) : null}
          </p>
        ) : null}
      </div>

      {/* Step 2 — pick a base branch (only when a clone exists) */}
      {configured && branches.length > 0 ? (
        <div className="mt-3 flex flex-col gap-2">
          <label className="flex items-center gap-1 text-[13px] text-muted">
            <GitBranch className="lucide-inline" size={13} />
            {i18nT('autoImprovement.branchLabel')}
          </label>
          {/* Guarantee the CONFIGURED branch is always a selectable option. A picker
              whose value matches no option falls back to its placeholder, which would
              silently misreport the branch a run targets — so if the branches list has
              not yet produced the persisted value, prepend it. */}
          <SimpleSelect
            options={branch && !branches.includes(branch) ? [branch, ...branches] : branches}
            value={branch}
            disabled={setupBusy || environmentBusy}
            onChange={(v) => {
              saveConfig.mutate({ branch: v }, { onSuccess: () => setBranch(v) })
            }}
            clearLabel={i18nT('autoImprovement.branchDefault')}
            className="h-7 text-[13px]"
            aria-label={i18nT('autoImprovement.branchLabel')}
          />

          {/* Autocommit: push a verified change straight to the base branch
              instead of drafting a pull request. The backend still refuses a
              protected branch (the spine's non-overridable denylist), so this
              only ever applies to a feature branch the operator named. */}
          <label className="mt-1 flex items-start gap-2">
            <input
              type="checkbox"
              checked={directCommit}
              disabled={setupBusy || environmentBusy}
              onChange={(e) => saveConfig.mutate({ directCommit: e.target.checked })}
              className="mt-0.5"
              aria-label={i18nT('autoImprovement.autocommitLabel')}
            />
            <span className="text-[13px]">
              {i18nT('autoImprovement.autocommitLabel')}
              <span className="block text-[11px] text-muted">
                {i18nT('autoImprovement.autocommitHint')}
              </span>
            </span>
          </label>
        </div>
      ) : null}

      {configured ? (
        <TestEnvironmentPanel
          config={config!}
          branch={branch}
          disabled={setupBusy}
          onBusyChange={setEnvironmentBusy}
        />
      ) : null}

      {/* Step 3 — run control */}
      {configured ? (
        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border pt-3">
          {active ? (
            <Btn danger onClick={() => stopRun.mutate()} disabled={stopRun.isPending || run?.status === 'stopping'}>
              <Square className="lucide-inline" size={14} /> {i18nT('autoImprovement.stopBtn')}
            </Btn>
          ) : (
            <Btn primary onClick={() => startRun.mutate()} disabled={setupBusy || environmentBusy}>
              <Play className="lucide-inline" size={14} /> {i18nT('autoImprovement.runBtn')}
            </Btn>
          )}
          <span role="status" aria-live="polite" className="text-[13px] text-muted">
            {run?.stats?.publication === 'published'
              ? i18nT('boundedMeasurement.published')
              : run?.stats?.publication === 'pending' && running
                ? i18nT('boundedMeasurement.publishing')
              : running
              ? i18nT('autoImprovement.runningStatus', {
                  cycle: run?.cycle ?? 0,
                  kept: run?.kept ?? 0,
                  drafted: run?.drafted ?? 0,
                })
              : run?.status === 'calibrating'
                ? i18nT('autoImprovementCalibration.calibrating')
                : run?.status === 'stopping'
                  ? i18nT('autoImprovementCalibration.stopping')
                  : run?.status === 'error' ? null : i18nT('autoImprovement.idleStatus')}
          </span>
          {/* No hand-off: the repository URL and environment draft are unsaved. */}
          <ErrorNotice message={run?.status === 'error' ? run.error || i18nT('autoImprovement.runError') : undefined} />
        </div>
      ) : null}

      {/* Live activity feed while a run is going */}
      {active && run?.activity && run.activity.length > 0 ? (
        <div role="log" aria-live="polite" className="mt-3 max-h-40 overflow-auto rounded border border-border bg-card p-2 font-mono text-[11px] text-muted select-text">
          {run.activity.slice(-30).map((item, i) => (
            <div key={i} className="whitespace-pre-wrap">
              {activityLine(item)}
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  )
}
