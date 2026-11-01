/**
 * Pick a lane: start a NEW session on any agent (crewmates such as Baton or
 * Atelier, mission kits, other agents), or add a session that is already open.
 * Either way the lane joins the arc's channel and gets its kickoff; the idle
 * nudge is on by default. The role is prefilled from the mate or kit.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Btn } from '../../components/ui'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import type { ChatSlot } from '../../types'
import { pickerAgents, pickerSessions, type LaneSpec, type SessionGroup } from './arcBoardModel'
import { C } from './theme'

export const ARC_BOARD_SLOTS_KEY = ['arc-board', 'slots'] as const
const ARC_BOARD_AGENTS_KEY = ['arc-board', 'agents'] as const

export function useSessionSlots(pollMs: number) {
  return useQuery({
    queryKey: ARC_BOARD_SLOTS_KEY,
    queryFn: () => api.chatSlots() as Promise<ChatSlot[]>,
    refetchInterval: pollMs,
  })
}

const GROUP_KEY: Record<SessionGroup, string> = {
  mates: 'pages.arcBoard.group_mates',
  kits: 'pages.arcBoard.group_kits',
  other: 'pages.arcBoard.group_other',
}
const AGENT_GROUP_KEY: Record<SessionGroup, string> = {
  mates: 'pages.arcBoard.group_mates',
  kits: 'pages.arcBoard.group_kits',
  other: 'pages.arcBoard.group_agents',
}

interface Row {
  id: string
  name: string
  detail: string
  running?: boolean
  role: string
  spec: (role: string, nudge: boolean) => LaneSpec
}

export default function LanePicker({
  slots,
  members,
  onAdd,
  onClose,
  pending,
  error,
}: {
  slots: ChatSlot[]
  members: Iterable<string>
  /** Resolve `false` to keep the selection (the add failed). */
  onAdd: (lane: LaneSpec) => void | Promise<boolean>
  onClose: () => void
  pending: boolean
  error: string | null
}) {
  const [tab, setTab] = useState<'new' | 'existing'>('new')
  const [query, setQuery] = useState('')
  const [chosen, setChosen] = useState<Row | null>(null)
  const [role, setRole] = useState('')
  const [nudge, setNudge] = useState(true)
  const catalog = useQuery({ queryKey: ARC_BOARD_AGENTS_KEY, queryFn: () => api.agentCatalog() })

  const groups = useMemo<Record<SessionGroup, Row[]>>(() => {
    if (tab === 'existing') {
      const s = pickerSessions(slots, members)
      const rows = (g: SessionGroup): Row[] =>
        s[g].map(x => ({
          id: 's:' + x.key,
          name: x.name,
          detail: x.agent,
          running: x.running,
          role: x.role,
          spec: (r, n) => ({ mode: 'existing', session_key: x.key, role: r, nudge: n, label: x.name }),
        }))
      return { mates: rows('mates'), kits: rows('kits'), other: rows('other') }
    }
    const a = pickerAgents(catalog.data?.agents ?? [])
    const rows = (g: SessionGroup): Row[] =>
      a[g].map(x => ({
        id: 'a:' + x.kind + ':' + x.name,
        name: x.label,
        detail: x.label === x.name ? '' : x.name,
        role: x.role,
        spec: (r, n) => ({ mode: 'new', agent: x.name, agent_kind: x.kind, role: r, nudge: n, label: x.label }),
      }))
    return { mates: rows('mates'), kits: rows('kits'), other: rows('other') }
  }, [tab, slots, members, catalog.data])

  const q = query.trim().toLowerCase()
  const match = (r: Row) => !q || r.name.toLowerCase().includes(q) || r.detail.toLowerCase().includes(q)
  const labels = tab === 'new' ? AGENT_GROUP_KEY : GROUP_KEY
  const empty = (['mates', 'kits', 'other'] as const).every(g => groups[g].filter(match).length === 0)

  return (
    <div className="rounded-lg border p-2" style={{ borderColor: C.accent }}>
      <div className="mb-2 flex gap-1" role="tablist">
        {(['new', 'existing'] as const).map(t => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            onClick={() => {
              setTab(t)
              setChosen(null)
              setRole('')
            }}
            className="flex-1 rounded-md border px-2 py-1 text-[11px]"
            style={tab === t ? { borderColor: C.accent, color: C.accent } : { borderColor: C.border, color: C.dim }}
          >
            {t === 'new' ? i18nT('pages.arcBoard.tab_new_session') : i18nT('pages.arcBoard.tab_existing_session')}
          </button>
        ))}
      </div>
      <input
        value={query}
        onChange={e => setQuery(e.target.value)}
        aria-label={tab === 'new' ? i18nT('pages.arcBoard.picker_search_agents') : i18nT('pages.arcBoard.picker_search')}
        placeholder={tab === 'new' ? i18nT('pages.arcBoard.picker_search_agents') : i18nT('pages.arcBoard.picker_search')}
        className="mb-2 w-full rounded-md border bg-transparent px-2 py-1 text-[12px]"
        style={{ borderColor: C.border, color: C.text }}
      />
      <div className="max-h-[260px] overflow-y-auto">
        {(['mates', 'kits', 'other'] as const).map(g => {
          const rows = groups[g].filter(match)
          if (rows.length === 0) return null
          return (
            <div key={g} className="mb-2">
              <div className="px-1 pb-1 text-[10px] font-semibold uppercase tracking-wide" style={{ color: C.dim }}>
                {i18nT(labels[g])} · {rows.length}
              </div>
              {rows.map(r => (
                <button
                  key={r.id}
                  type="button"
                  onClick={() => {
                    setChosen(r)
                    setRole(r.role)
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-[12px]"
                  style={chosen?.id === r.id ? { backgroundColor: C.cardHl, color: C.text } : { color: C.text }}
                >
                  {r.running !== undefined ? (
                    <span
                      className="inline-block h-2 w-2 shrink-0 rounded-full"
                      style={{ backgroundColor: r.running ? C.ok : C.border }}
                      aria-hidden
                    />
                  ) : null}
                  <span className="min-w-0 flex-1 truncate">{r.name}</span>
                  {r.detail ? (
                    <span className="shrink-0 font-mono text-[10px]" style={{ color: C.dim }}>
                      {r.detail}
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          )
        })}
        {empty ? (
          <div className="px-1 py-2 text-[12px]" style={{ color: C.dim }}>
            {tab === 'new' && catalog.isLoading ? i18nT('pages.arcBoard.loading') : i18nT('pages.arcBoard.picker_none')}
          </div>
        ) : null}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <input
          value={role}
          onChange={e => setRole(e.target.value)}
          aria-label={i18nT('pages.arcBoard.form_lane_role_ph')}
          placeholder={i18nT('pages.arcBoard.form_lane_role_ph')}
          disabled={!chosen}
          className="min-w-0 flex-1 rounded-md border bg-transparent px-2 py-1 text-[12px]"
          style={{ borderColor: C.border, color: C.text }}
        />
        <label className="flex items-center gap-1 text-[11px]" style={{ color: C.dim }}>
          <input
            type="checkbox"
            aria-label={i18nT('pages.arcBoard.nudge_label')}
            checked={nudge}
            onChange={e => setNudge(e.target.checked)}
          />
          {i18nT('pages.arcBoard.nudge_label')}
        </label>
      </div>
      <div className="mt-1.5 flex items-center gap-1.5">
        <Btn
          type="button"
          primary
          disabled={!chosen || !role.trim() || pending}
          onClick={() => {
            if (!chosen) return
            void Promise.resolve(onAdd(chosen.spec(role.trim(), nudge))).then(ok => {
              if (ok === false) return
              setChosen(null)
              setRole('')
            })
          }}
          className="shrink-0 px-2 py-1 text-[11px]"
        >
          {tab === 'new' ? i18nT('pages.arcBoard.start_lane') : i18nT('pages.arcBoard.form_add_lane')}
        </Btn>
        <Btn type="button" onClick={onClose} className="shrink-0 px-2 py-1 text-[11px]">
          {i18nT('pages.arcBoard.form_cancel')}
        </Btn>
      </div>
      {error ? (
        <div className="mt-1 text-[11px]" style={{ color: C.warn }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}
