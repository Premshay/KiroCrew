/**
 * Pick lanes from every live session: crewmates (Baton, Atelier, Tenon, …),
 * mission-kit sessions (`kit-*`) and any other session, grouped and searchable.
 * The role is prefilled from the session's mate or kit and stays editable.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Btn } from '../../components/ui'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import type { ChatSlot } from '../../types'
import { pickerSessions, type PickerSession, type SessionGroup } from './arcBoardModel'
import { C } from './theme'

export const ARC_BOARD_SLOTS_KEY = ['arc-board', 'slots'] as const

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
  onAdd: (lane: { session_key: string; role: string }) => void | Promise<boolean>
  onClose: () => void
  pending: boolean
  error: string | null
}) {
  const [query, setQuery] = useState('')
  const [chosen, setChosen] = useState<PickerSession | null>(null)
  const [role, setRole] = useState('')
  const groups = useMemo(() => pickerSessions(slots, members), [slots, members])
  const q = query.trim().toLowerCase()
  const match = (s: PickerSession) =>
    !q || s.name.toLowerCase().includes(q) || s.agent.toLowerCase().includes(q) || s.key.includes(q)

  return (
    <div className="rounded-lg border p-2" style={{ borderColor: C.accent }}>
      <input
        value={query}
        onChange={e => setQuery(e.target.value)}
        aria-label={i18nT('pages.arcBoard.picker_search')}
        placeholder={i18nT('pages.arcBoard.picker_search')}
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
                {i18nT(GROUP_KEY[g])} · {rows.length}
              </div>
              {rows.map(s => (
                <button
                  key={s.key}
                  type="button"
                  onClick={() => {
                    setChosen(s)
                    setRole(s.role)
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-[12px]"
                  style={chosen?.key === s.key ? { backgroundColor: C.cardHl, color: C.text } : { color: C.text }}
                >
                  <span
                    className="inline-block h-2 w-2 shrink-0 rounded-full"
                    style={{ backgroundColor: s.running ? C.ok : C.border }}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1 truncate">{s.name}</span>
                  {s.agent ? (
                    <span className="shrink-0 font-mono text-[10px]" style={{ color: C.dim }}>
                      {s.agent}
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          )
        })}
        {(['mates', 'kits', 'other'] as const).every(g => groups[g].filter(match).length === 0) ? (
          <div className="px-1 py-2 text-[12px]" style={{ color: C.dim }}>
            {i18nT('pages.arcBoard.picker_none')}
          </div>
        ) : null}
      </div>
      <div className="mt-2 flex items-center gap-1.5">
        <input
          value={role}
          onChange={e => setRole(e.target.value)}
          aria-label={i18nT('pages.arcBoard.form_lane_role_ph')}
          placeholder={i18nT('pages.arcBoard.form_lane_role_ph')}
          disabled={!chosen}
          className="min-w-0 flex-1 rounded-md border bg-transparent px-2 py-1 text-[12px]"
          style={{ borderColor: C.border, color: C.text }}
        />
        <Btn
          type="button"
          primary
          disabled={!chosen || !role.trim() || pending}
          onClick={() => {
            if (!chosen) return
            void Promise.resolve(onAdd({ session_key: chosen.key, role: role.trim() })).then(ok => {
              if (ok === false) return
              setChosen(null)
              setRole('')
            })
          }}
          className="shrink-0 px-2 py-1 text-[11px]"
        >
          {i18nT('pages.arcBoard.form_add_lane')}
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
