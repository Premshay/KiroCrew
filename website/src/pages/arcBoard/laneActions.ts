/**
 * Adding a lane end to end: a NEW lane first gets its own dashboard session on
 * the chosen agent (in the arc's project, when it has one); then the lane is
 * added to the arc, which joins it to the arc's channel, sends its kickoff and,
 * when asked, arms its idle nudge.
 */

import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import type { ArcBoardArc, ArcBoardLaneJoin } from '../../api/client/arcBoard'
import type { ChatSlot } from '../../types'
import { laneKey, type LaneSpec } from './arcBoardModel'

/** Start the session a new lane runs in and return its member key. */
export async function startLaneSession(arc: Pick<ArcBoardArc, 'title' | 'project'>, spec: Extract<LaneSpec, { mode: 'new' }>): Promise<string> {
  const title = i18nT('pages.arcBoard.new_lane_title', { role: spec.role, arc: arc.title }).slice(0, 120)
  const project = arc.project || ''
  const slot = (await api.createChatSlot(
    undefined,
    spec.agent,
    undefined,
    undefined,
    undefined,
    title,
    undefined,
    undefined,
    spec.agent_kind,
    project ? true : undefined,
    undefined,
    true,
  )) as ChatSlot
  if (project) await api.chatSlotProject(slot.key, project)
  return laneKey(slot.key)
}

export async function addLaneToArc(arc: ArcBoardArc, spec: LaneSpec): Promise<ArcBoardLaneJoin> {
  const key = spec.mode === 'new' ? await startLaneSession(arc, spec) : spec.session_key
  const res = await api.arcBoardAddMember(arc.id, key, spec.role, { nudge: spec.nudge })
  return res.lane
}

const NOT_JOINED_KEY = {
  no_channel: 'pages.arcBoard.lane_not_joined_no_channel',
  session_not_found: 'pages.arcBoard.lane_not_joined_session_not_found',
  channel_full: 'pages.arcBoard.lane_not_joined_channel_full',
  failed: 'pages.arcBoard.lane_not_joined_failed',
} as const

export interface LaneNote {
  tone: 'ok' | 'warn'
  text: string
}

/** What reached a lane, in plain words; never the raw codes. */
export function laneJoinNote(role: string, lane: ArcBoardLaneJoin): LaneNote {
  const parts: string[] = []
  let tone: LaneNote['tone'] = 'ok'
  if (lane.attached) {
    parts.push(
      lane.kickoff === 'posted'
        ? i18nT('pages.arcBoard.lane_joined', { lane: role })
        : i18nT('pages.arcBoard.lane_joined_quiet', { lane: role }),
    )
  } else {
    tone = 'warn'
    const key = NOT_JOINED_KEY[(lane.reason ?? 'failed') as keyof typeof NOT_JOINED_KEY] ?? NOT_JOINED_KEY.failed
    parts.push(i18nT(key, { lane: role }))
  }
  if (lane.nudge === 'set') parts.push(i18nT('pages.arcBoard.nudge_set'))
  if (lane.nudge === 'failed') {
    tone = 'warn'
    parts.push(i18nT('pages.arcBoard.nudge_failed', { error: lane.nudge_error || '' }))
  }
  return { tone, text: parts.join(' ') }
}
