/**
 * Pure logic behind the arc board console: link safety, the lane picker's
 * session groups, each lane's live state, and the feed's filter + history
 * cursor. Kept free of React so every rule here has a direct test.
 */

import type { ChatSlot } from '../../types'
import type { ArcBoardEntry, ArcBoardLane } from '../../api/client/arcBoard'

/** An entry's `link` is free evidence text ("https://… — note", a branch name,
 *  a path). Only a leading http(s) URL becomes a clickable href; everything
 *  else stays text, so a lane can never hand the owner an executable link. */
export function splitLink(link: string): { href: string | null; text: string } {
  const m = /^\s*(https?:\/\/[^\s<>"']+)(.*)$/i.exec(link)
  if (!m) return { href: null, text: link }
  return { href: m[1], text: m[2].replace(/^\s*[—–-]\s*/, '').trim() }
}

/** The arc store keys members by the ledger spelling: dashboard prefixes folded
 *  off (`session_ledger_layout.ledger_key`). Slots and nudge loops may carry
 *  either spelling, so every comparison goes through this. */
export function laneKey(key: string): string {
  let k = key.startsWith('dashboard:') ? key.slice('dashboard:'.length) : key
  while (k.startsWith('dashboard_')) k = k.slice('dashboard_'.length)
  return k
}

export type SessionGroup = 'mates' | 'kits' | 'other'

/** Which picker group a session belongs to: a crewmate (its agent is a
 *  configured member), a mission kit (a `kit-*` seat), or any other session. */
export function sessionGroup(slot: Pick<ChatSlot, 'agent' | 'agent_kind' | 'runtime_agent'>): SessionGroup {
  const agent = slot.agent || slot.runtime_agent || ''
  if (agent.startsWith('kit-')) return 'kits'
  if (slot.agent_kind === 'member') return 'mates'
  return 'other'
}

/** The name a lane picker row shows: the session title, else its agent. */
export function sessionName(slot: Pick<ChatSlot, 'title' | 'agent' | 'key'>): string {
  return (slot.title || '').trim() || slot.agent || laneKey(slot.key)
}

/** A role suggestion for a newly added lane: a crewmate's or kit's agent name,
 *  else the session title, so "baton" → "Baton" and "kit-import" → "Import kit". */
export function suggestedRole(slot: Pick<ChatSlot, 'title' | 'agent' | 'agent_kind' | 'runtime_agent' | 'key'>): string {
  const agent = slot.agent || ''
  const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
  if (agent.startsWith('kit-')) return cap(agent.slice(4).replace(/-/g, ' '))
  if (slot.agent_kind === 'member' && agent) return cap(agent)
  return sessionName(slot).slice(0, 60)
}

export interface PickerSession {
  key: string
  name: string
  agent: string
  group: SessionGroup
  running: boolean
  role: string
}

/** Every session the owner can add, grouped and sorted, minus current members. */
export function pickerSessions(slots: ChatSlot[], members: Iterable<string>): Record<SessionGroup, PickerSession[]> {
  const taken = new Set(Array.from(members, laneKey))
  const out: Record<SessionGroup, PickerSession[]> = { mates: [], kits: [], other: [] }
  for (const slot of slots) {
    const key = laneKey(slot.key)
    if (!key || taken.has(key)) continue
    const group = sessionGroup(slot)
    out[group].push({
      key,
      name: sessionName(slot),
      agent: slot.agent || '',
      group,
      running: !!slot.running,
      role: suggestedRole(slot),
    })
  }
  for (const g of Object.keys(out) as SessionGroup[]) {
    out[g].sort((a, b) => a.name.localeCompare(b.name))
  }
  return out
}

export type LaneState = 'working' | 'idle' | 'gone'

export interface LaneView extends ArcBoardLane {
  state: LaneState
  /** Last activity of the lane's session (any turn), else its last board entry. */
  activeTs: string | null
  title: string
  agent: string
  nudge: boolean
}

/** Join the store's lanes with the live sessions and the armed nudge loops. */
export function laneViews(
  lanes: ArcBoardLane[],
  slots: ChatSlot[] | undefined,
  nudgeSlots: Iterable<string>,
): LaneView[] {
  const byKey = new Map((slots ?? []).map(s => [laneKey(s.key), s]))
  const nudges = new Set(Array.from(nudgeSlots, laneKey))
  return lanes.map(lane => {
    const slot = byKey.get(lane.key)
    const state: LaneState = !slot && slots ? 'gone' : slot?.running ? 'working' : 'idle'
    return {
      ...lane,
      state,
      activeTs: slot?.last_activity_ts || slot?.last_ts || lane.last_ts,
      title: slot ? sessionName(slot) : lane.key,
      agent: slot?.agent || '',
      nudge: nudges.has(lane.key),
    }
  })
}

export const ENTRY_KINDS = ['CLAIM', 'FACT', 'FAIL', 'ASK', 'DONE'] as const

/**
 * The feed as the owner sees it: newest first, filtered by kind and lane, and
 * cut at the history cursor. `cursor` is the number of entries replayed from
 * the start of the arc; `null` is LIVE and follows every new entry.
 */
export function feedView(
  entries: ArcBoardEntry[],
  opts: { kinds: ReadonlySet<string>; lane: string | null; cursor: number | null },
): ArcBoardEntry[] {
  const upto = opts.cursor === null ? entries.length : Math.max(0, Math.min(opts.cursor, entries.length))
  return entries
    .slice(0, upto)
    .filter(e => opts.kinds.has(e.kind) && (opts.lane === null || e.author === opts.lane))
    .reverse()
}

/** Entries whose ids were not in the previous poll: what the stage animates. */
export function newEntries(prevIds: ReadonlySet<string> | null, entries: ArcBoardEntry[]): ArcBoardEntry[] {
  if (prevIds === null) return []
  return entries.filter(e => !prevIds.has(e.id))
}

/** The per-turn brief cap (`arc_brief.PREFIX_CAP`): the meter's denominator. */
export const BRIEF_CAP = 4000
