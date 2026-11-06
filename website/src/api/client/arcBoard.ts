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
export const arcBoardReportKey = (arcId: string) => ['arc-board', arcId, 'report']

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
  /** A DONE that says it finishes the current milestone: a sign-off request. */
  completes_milestone?: boolean
  /** A coordinator FACT that signed the milestone off for the owner. */
  signoff?: boolean
  /** Set on a merged PR the board recorded from GitHub (author "github"). */
  pr?: number
  head?: string
  merge_sha?: string
  reviewers?: string[]
  verified?: boolean
  /** The member whose branch it was, or "". */
  lane?: string
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
  /** The arc opened this channel for itself; absent on arcs made before it existed. */
  channel_owned?: boolean
  /** Where new lane sessions start; empty keeps each session's default. */
  project?: string
  /** GitHub owner/name whose merged "Arc: <id>" PRs are recorded; "" for none. */
  repo?: string
  /** The member who may sign milestones off for the owner; "" for none. */
  coordinator?: string
  members: Record<string, ArcBoardMember>
  milestone_index: number
  created_at: string
}

/** One member as the store sees it: its board activity and open claim. */
export interface ArcBoardLane {
  key: string
  role: string
  joined_at: string
  entries: number
  last_ts: string | null
  last_kind: string | null
  claim: ArcBoardEntry | null
  /** Whether the lane's session is in the arc's channel (urgent entries reach it). */
  in_channel?: boolean
}

export interface ArcBoardChannel {
  id: string
  owned: boolean
  open: boolean
  topic: string
  members: string[]
}

/** What reached a lane when it was added: the channel, its kickoff, its nudge. */
export interface ArcBoardLaneJoin {
  key: string
  attached: boolean
  kickoff: 'posted' | 'skipped'
  reason?: 'no_channel' | 'session_not_found' | 'channel_full' | 'failed' | string
  nudge?: 'set' | 'failed'
  nudge_error?: string
}

/** `GET /api/arc-board/arcs/{id}`: the arc, its lanes, redacted entries and each lane's brief. */
export interface ArcBoardDetail {
  arc: ArcBoardArc
  lanes: ArcBoardLane[]
  /** Each member's per-turn brief, keyed by member key, exactly as the lane reads it. */
  briefs: Record<string, string>
  entries: ArcBoardEntry[]
  open_claims: ArcBoardEntry[]
  open_asks: ArcBoardAsk[]
  /** Groups of open-claim ids by different lanes on one milestone and step. */
  collisions: string[][]
  /** DONE entries that finish the current milestone: the lanes asking for sign-off. */
  milestone_reports: ArcBoardEntry[]
  channel: ArcBoardChannel
}

export interface ArcBoardMedianMax {
  median: number | null
  max: number | null
}

/** `GET /api/arc-board/arcs/{id}/report`: board-only pilot metrics (minutes). */
export interface ArcBoardReport {
  as_of: string
  since: string | null
  entries: number
  by_kind: Record<string, number>
  by_lane: Record<string, Record<string, number>>
  done_links: string[]
  idle_gaps: {
    overall: ArcBoardMedianMax
    per_member: Record<string, ArcBoardMedianMax>
    open_gaps_min: Record<string, number>
  }
  ask_age: {
    time_to_reply_min: ArcBoardMedianMax
    open_asks: number
    oldest_open_ask_min: number | null
  }
  collisions: number
  repeated_fails: number
  reading_load_words: number
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
  project?: string
  /** `new` (default) opens a channel for the arc; `existing` shares an open one. */
  channel?: { mode: 'new' } | { mode: 'existing'; id: string } | { mode: 'none' }
  /** First lanes for the arc, written with it in one store write. */
  members?: { session_key: string; role: string; nudge?: boolean }[]
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
        post('/api/arc-board/arcs', spec).then(j) as Promise<{ arc: ArcBoardArc; lanes: ArcBoardLaneJoin[] }>,
      /** Adds the lane, joins it to the arc's channel and sends its kickoff. */
      arcBoardAddMember: (arcId: string, sessionKey: string, role: string, opts?: { nudge?: boolean; kickoff?: boolean }) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/members', {
          session_key: sessionKey,
          role,
          ...(opts?.nudge ? { nudge: true } : {}),
          ...(opts?.kickoff === false ? { kickoff: false } : {}),
        }).then(j) as Promise<{ arc: ArcBoardArc; lane: ArcBoardLaneJoin }>,
      /** The arc's GitHub repo and the coordinator who signs off for the owner. */
      arcBoardSettings: (arcId: string, settings: { repo?: string; coordinator?: string }) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/settings', settings).then(j) as Promise<{ arc: ArcBoardArc }>,
      /** Gives an arc with no open channel its own and joins every lane to it. */
      arcBoardOpenChannel: (arcId: string) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/channel', {}).then(j) as Promise<{
          arc: ArcBoardArc
          lanes: ArcBoardLaneJoin[]
        }>,
      arcBoardAnswer: (arcId: string, askId: string, text: string) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/answer', {
          ask_id: askId,
          text,
        }).then(j) as Promise<{ entry: ArcBoardEntry; delivery: ArcBoardDelivery }>,
      arcBoardRemoveMember: (arcId: string, sessionKey: string) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/members/remove', {
          session_key: sessionKey,
        }).then(j) as Promise<{ arc: ArcBoardArc }>,
      arcBoardAutonomy: (arcId: string, autonomy: 1 | 2 | 3) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/autonomy', { autonomy }).then(j) as Promise<{ arc: ArcBoardArc }>,
      arcBoardNote: (arcId: string, text: string) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/note', { text }).then(j) as Promise<{
          entry: ArcBoardEntry
          delivery: ArcBoardDelivery
        }>,
      arcBoardReport: (arcId: string) =>
        get('/api/arc-board/arcs/' + id(arcId) + '/report').then(j) as Promise<ArcBoardReport>,
      /** `expectedIndex` is the milestone the console showed; a stale one answers 409. */
      arcBoardAccept: (arcId: string, expectedIndex: number) =>
        post('/api/arc-board/arcs/' + id(arcId) + '/accept', { expected_index: expectedIndex }).then(j) as Promise<{
          arc: ArcBoardArc
        }>,
    },
  }
}
