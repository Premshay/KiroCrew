/**
 * Left rail: the arcs, the selected arc's milestone track, and its lanes —
 * each lane's live state (working, idle, session gone), current claim, last
 * activity and whether an idle nudge is armed. Lanes are added from every
 * live session through the lane picker and can be taken off the arc.
 */

import { useState } from 'react'
import { Check, Plus, X } from 'lucide-react'
import { Badge, Btn, Card, CardTitle } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { fmtRelative } from '../../i18n/format'
import type { ArcBoardArc, ArcBoardSummary } from '../../api/client/arcBoard'
import type { ChatSlot } from '../../types'
import type { LaneView } from './arcBoardModel'
import LanePicker from './LanePicker'
import { C } from './theme'

function StateDot({ state }: { state: LaneView['state'] }) {
  const color = state === 'working' ? C.ok : state === 'gone' ? C.danger : C.dim
  const key =
    state === 'working'
      ? 'pages.arcBoard.state_working'
      : state === 'gone'
        ? 'pages.arcBoard.state_gone'
        : 'pages.arcBoard.state_idle'
  return (
    <span className="inline-flex items-center gap-1 text-[10px]" style={{ color }}>
      <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: color }} aria-hidden />
      {i18nT(key)}
    </span>
  )
}

export default function ArcRail({
  arcs,
  arcId,
  onSelectArc,
  arc,
  lanes,
  selectedLane,
  onSelectLane,
  slots,
  onAddLane,
  onRemoveLane,
  laneError,
  lanePending,
}: {
  arcs: ArcBoardSummary[]
  arcId: string
  onSelectArc: (id: string) => void
  arc: ArcBoardArc | null
  lanes: LaneView[]
  selectedLane: string | null
  onSelectLane: (key: string) => void
  slots: ChatSlot[]
  onAddLane: (lane: { session_key: string; role: string }) => Promise<boolean>
  onRemoveLane: (key: string) => void
  laneError: string | null
  lanePending: boolean
}) {
  const [picking, setPicking] = useState(false)
  const [removing, setRemoving] = useState<string | null>(null)

  return (
    <div className="flex flex-col gap-3">
      <Card className="p-0">
        <CardTitle className="px-3 pt-3">{i18nT('pages.arcBoard.arcs')}</CardTitle>
        <div className="flex flex-col pb-1">
          {arcs.map(a => (
            <button
              key={a.id}
              type="button"
              onClick={() => onSelectArc(a.id)}
              className="border-l-2 px-3 py-2 text-left"
              style={
                a.id === arcId
                  ? { borderColor: C.accent, backgroundColor: C.cardHl }
                  : { borderColor: 'transparent' }
              }
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-[13px] font-medium" style={{ color: C.text }}>
                  {a.title}
                </span>
                <span className="shrink-0 text-[11px] tabular-nums" style={{ color: C.dim }}>
                  {a.milestone_index}/{a.milestones_total}
                </span>
              </div>
              <div className="mt-0.5 truncate text-[11px]" style={{ color: C.dim }}>
                {a.milestone
                  ? i18nT('pages.arcBoard.on_milestone', { m: a.milestone })
                  : i18nT('pages.arcBoard.all_accepted')}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {a.open_asks > 0 ? (
                  <Badge variant="warn" className="px-1.5 text-[10px]">
                    {i18nT('pages.arcBoard.asks_n', { n: a.open_asks })}
                  </Badge>
                ) : null}
                <span className="text-[11px]" style={{ color: C.dim }}>
                  {i18nT('pages.arcBoard.lanes_n', { n: a.members })}
                </span>
              </div>
            </button>
          ))}
        </div>
      </Card>

      {arc ? (
        <Card className="p-3">
          <CardTitle>{i18nT('pages.arcBoard.milestones')}</CardTitle>
          <ol className="flex flex-col gap-1.5">
            {arc.milestones.map((m, i) => {
              const done = i < arc.milestone_index
              const current = i === arc.milestone_index
              return (
                <li key={i} className="flex items-start gap-2 text-[12px]" title={m.criteria || m.name}>
                  <span
                    className="mt-[1px] inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[9px]"
                    style={{
                      borderColor: done ? C.ok : current ? C.accent : C.border,
                      color: done ? C.ok : current ? C.accent : C.dim,
                    }}
                  >
                    {done ? <Check size={10} /> : i + 1}
                  </span>
                  <span style={{ color: current ? C.text : C.dim, fontWeight: current ? 600 : 400 }}>{m.name}</span>
                </li>
              )
            })}
          </ol>
        </Card>
      ) : null}

      {arc ? (
        <Card className="p-0">
          <div className="flex items-center justify-between px-3 pt-3">
            <CardTitle className="mb-0">{i18nT('pages.arcBoard.lanes')}</CardTitle>
            {!picking ? (
              <Btn type="button" onClick={() => setPicking(true)} className="px-1.5 py-0.5 text-[11px]">
                <Plus size={12} />
                {i18nT('pages.arcBoard.form_add_lane')}
              </Btn>
            ) : null}
          </div>
          {picking ? (
            <div className="px-3 pt-2">
              <LanePicker
                slots={slots}
                members={Object.keys(arc.members)}
                onAdd={async lane => {
                  const ok = await onAddLane(lane)
                  if (ok) setPicking(false)
                  return ok
                }}
                onClose={() => setPicking(false)}
                pending={lanePending}
                error={laneError}
              />
            </div>
          ) : null}
          <div className="flex flex-col py-1">
            {lanes.length === 0 ? (
              <div className="px-3 py-2 text-[12px]" style={{ color: C.dim }}>
                {i18nT('pages.arcBoard.no_lanes')}
              </div>
            ) : null}
            {lanes.map(l => (
              <div
                key={l.key}
                className="border-l-2 px-3 py-2"
                style={
                  selectedLane === l.key
                    ? { borderColor: C.accent, backgroundColor: C.cardHl }
                    : { borderColor: 'transparent' }
                }
              >
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => onSelectLane(l.key)}
                    className="min-w-0 flex-1 truncate text-left text-[13px] font-medium"
                    style={{ color: C.text }}
                  >
                    {l.role}
                  </button>
                  <StateDot state={l.state} />
                  {removing === l.key ? (
                    <>
                      <Btn
                        type="button"
                        danger
                        onClick={() => {
                          onRemoveLane(l.key)
                          setRemoving(null)
                        }}
                        className="px-1.5 py-0 text-[10px]"
                      >
                        {i18nT('pages.arcBoard.remove')}
                      </Btn>
                      <Btn type="button" onClick={() => setRemoving(null)} className="px-1.5 py-0 text-[10px]">
                        {i18nT('pages.arcBoard.form_cancel')}
                      </Btn>
                    </>
                  ) : (
                    <button
                      type="button"
                      aria-label={i18nT('pages.arcBoard.remove_lane', { lane: l.role })}
                      title={i18nT('pages.arcBoard.remove_lane', { lane: l.role })}
                      onClick={() => setRemoving(l.key)}
                      className="shrink-0"
                      style={{ color: C.dim }}
                    >
                      <X size={12} />
                    </button>
                  )}
                </div>
                <div className="mt-0.5 truncate text-[11px]" style={{ color: C.dim }}>
                  {l.title}
                  {l.agent ? ` · ${l.agent}` : ''}
                </div>
                <div className="mt-0.5 truncate text-[11px]" style={{ color: l.claim ? C.aim : C.dim }}>
                  {l.claim ? i18nT('pages.arcBoard.claiming', { step: l.claim.step || l.claim.text }) : i18nT('pages.arcBoard.no_claim')}
                </div>
                <div className="mt-0.5 flex flex-wrap gap-x-2 text-[10px]" style={{ color: C.dim }}>
                  <span>{i18nT('pages.arcBoard.active_ago', { ago: fmtRelative(l.activeTs) })}</span>
                  <span>{i18nT('pages.arcBoard.entries_n', { n: l.entries })}</span>
                  <span style={{ color: l.nudge ? C.aim : C.dim }}>
                    {l.nudge ? i18nT('pages.arcBoard.nudge_on') : i18nT('pages.arcBoard.nudge_off')}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </Card>
      ) : null}
    </div>
  )
}
