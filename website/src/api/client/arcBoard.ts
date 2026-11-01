/**
 * The owner's arc board console: the durable per-arc boards (arcs, their
 * milestone progress, lanes, the entry feed, open asks and claims), plus the
 * owner's own writes — create an arc, add a lane, answer an ask, accept a
 * milestone. Owner-gated server side; the members' board routes live under
 * `/api/arcs` and are not part of this surface.
 */

import type { ClientTransport } from './transport'

/** The board is a live surface: lanes write to it while the owner reads it. 10 s
 *  matches the other board (crewBoard) and costs the lane nothing. */
export const ARC_BOARD_POLL_MS = 10_000

/** One stable key per arc, so polling the selected arc never invalidates the rail. */
export const arcBoardListKey = ['arc-board'] as const
export const arcBoardQueryKey = (arcId: string) => ['arc-board', arcId]

export interface ArcBoardMilestone {
  name: string
  criteria: string
}

/** One arc as `GET /api/arc-board/arcs` reports it: the left rail's row. */
export interface ArcBoardSummary {
  id: string
  title: string
  goal: string
  reason: string
  lineage: string
  autonomy: number
  milestone_index: number
  /** The current milestone's name; `""` once every milestone is accepted. */
  milestone: string
  milestones_total: number
  members: number
  open_asks: number
  open_claims: number
  created_at: string
}

export interface ArcBoardMember {
  role: string
  joined_at: string
}

/** One board entry, redacted the same way the members' routes redact it. */
export interface ArcBoardEntry {
  id: string
  ts: string
  author: string
  kind: 'CLAIM' | 'FACT' | 'FAIL' | 'ASK' | 'DONE' | string
  text: string
  link?: string
  reply_to?: string
  step?: string
  checked?: string[]
  to?: string
  milestone?: number
}

/** An open ask: its age and who has seen it, for the "needs you" rail. */
export interface ArcBoardAsk extends ArcBoardEntry {
  age_secs: number
  seen_by: string[]
}

export interface ArcBoardArc {
  id: string
  title: string
  goal: string
  reason: string
  lineage: string
  source: string
  autonomy: number
  milestones: ArcBoardMilestone[]
  decisions: string[]
  rules: string[]
  reuse: string[]
  channel_id: string
  members: Record<string, ArcBoardMember>
  milestone_index: number
  created_at: string
}

/** `GET /api/arc-board/arcs/{id}`: the arc, its redacted entries and the owner's brief. */
export interface ArcBoardDetail {
  arc: ArcBoardArc
  brief: string
  entries: ArcBoardEntry[]
  open_claims: ArcBoardEntry[]
  open_asks: ArcBoardAsk[]
}

export interface ArcBoardCreateSpec {
  title: string
  goal?: string
  reason?: string
  lineage?: string
  autonomy?: number
  milestones?: ArcBoardMilestone[]
  decisions?: string[]
  rules?: string[]
  reuse?: string[]
  /** First lanes for the arc; each is added as a member after creation. */
  members?: { session_key: string; role: string }[]
}

export interface ArcBoardDelivery {
  recipients: string[]
  outcome: string
  unreachable?: string[]
}

export function createArcBoardEndpoints({ get, post, j }: ClientTransport) {
  const id = (arcId: string) => encodeURIComponent(arcId)

  return {
    board: {
      arcBoardArcs: () => get('/api/arc-board/arcs').then(j) as Promise<{ arcs: ArcBoardSummary[] }>,
      arcBoardDetail: (arcId: string) =>
        get('/api/arc-board/arcs/' + id(arcId)).then(j) as Promise<ArcBoardDetail>,
      arcBoardCreate: (spec: ArcBoardCreateSpec) =>
        post('/api/arc-board/arcs', spec).then(j) as Promise<{ arc: ArcBoardArc }>,
      arcBoardAddMember: (arcId: string, sessionKey: string, role: string) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/members', {
          session_key: sessionKey,
          role,
        }).then(j) as Promise<{ arc: ArcBoardArc }>,
      arcBoardAnswer: (arcId: string, askId: string, text: string) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/answer', {
          ask_id: askId,
          text,
        }).then(j) as Promise<{ entry: ArcBoardEntry; delivery: ArcBoardDelivery }>,
      arcBoardAccept: (arcId: string) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/accept', {}).then(j) as Promise<{ arc: ArcBoardArc }>,
    },
  }
}
