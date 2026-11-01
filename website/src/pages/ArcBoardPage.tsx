/**
 * ArcBoardPage — the owner's console over the durable arc boards.
 *
 * Three panes, the shape the owner approved in the console prototype:
 *   left   — the arcs, the selected arc's milestone track, and its lanes with
 *            their live state, claim, last activity and nudge;
 *   centre — counters, the live stage (lanes around the board, the owner below),
 *            the board feed with filters and a history scrubber, and a Report tab;
 *   right  — what needs the owner (asks, the milestone to accept or send back),
 *            autonomy, the selected lane's per-turn brief, and flags.
 *
 * Everything shown is what the store keeps (entries redacted server-side), and
 * every action is a plain write to the same store the `kirocrew arc` CLI and the
 * lanes' MCP tools share. Lanes are picked from every live session: crewmates,
 * mission kits and any other session.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import type React from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check } from 'lucide-react'

import { Btn, Card, EmptyState, PageHeader } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { AUTONUDGE_LOOPS_QUERY_KEY } from '../components/autoNudgeLoop'
import { errMessage } from '../utils/thunkError'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import {
  ARC_BOARD_POLL_MS,
  arcBoardListKey,
  arcBoardQueryKey,
  arcBoardReportKey,
  type ArcBoardCreateSpec,
  type ArcBoardEntry,
  type ArcBoardSummary,
} from '../api/client/arcBoard'
import ArcRail from './arcBoard/ArcRail'
import ArcStage from './arcBoard/ArcStage'
import ClampText from './arcBoard/ClampText'
import BoardFeed from './arcBoard/BoardFeed'
import ControlRail, { MilestoneCard } from './arcBoard/ControlRail'
import NewArcForm from './arcBoard/NewArcForm'
import ReportTab from './arcBoard/ReportTab'
import { useSessionSlots } from './arcBoard/LanePicker'
import { laneViews, newEntries, type LaneSpec } from './arcBoard/arcBoardModel'
import { addLaneToArc, laneJoinNote, type LaneNote } from './arcBoard/laneActions'
import { C } from './arcBoard/theme'

function Counter({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="min-w-[96px] flex-1 rounded-lg border px-3 py-2" style={{ borderColor: C.border }}>
      <div className="text-[18px] font-semibold tabular-nums" style={{ color: tone ?? C.text }}>
        {value}
      </div>
      <div className="text-[11px]" style={{ color: C.dim }}>
        {label}
      </div>
    </div>
  )
}

export default function ArcBoardPage() {
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [showForm, setShowForm] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [tab, setTab] = useState<'board' | 'report'>('board')
  const [lanePick, setLanePick] = useState<string | null>(null)
  const [laneNotes, setLaneNotes] = useState<LaneNote[]>([])
  const [laneError, setLaneError] = useState<string | null>(null)
  const [lanePending, setLanePending] = useState(false)
  const needsRef = useRef<HTMLDivElement>(null)

  const list = useQuery({
    queryKey: arcBoardListKey,
    queryFn: () => api.arcBoardArcs(),
    refetchInterval: ARC_BOARD_POLL_MS,
  })
  const arcs: ArcBoardSummary[] = list.data?.arcs ?? []
  const selectedId = params.get('arc')
  const arcId = arcs.some(a => a.id === selectedId) ? (selectedId as string) : (arcs[0]?.id ?? '')

  const detail = useQuery({
    queryKey: arcBoardQueryKey(arcId),
    queryFn: () => api.arcBoardDetail(arcId),
    enabled: arcId.length > 0,
    refetchInterval: ARC_BOARD_POLL_MS,
  })
  const slots = useSessionSlots(ARC_BOARD_POLL_MS)
  const nudges = useQuery({
    queryKey: AUTONUDGE_LOOPS_QUERY_KEY,
    queryFn: () => api.autonudgeList(),
    refetchInterval: 30_000,
  })

  const data = detail.data && detail.data.arc.id === arcId ? detail.data : undefined
  const lanes = useMemo(
    () =>
      laneViews(
        data?.lanes ?? [],
        slots.data,
        (nudges.data?.loops ?? []).filter(l => l.active).map(l => l.slot_key),
      ),
    [data, slots.data, nudges.data],
  )
  // Nothing is selected until the owner picks a lane; picking it again, an
  // empty part of the stage, or Escape clears it.
  const selectedLane = lanes.some(l => l.key === lanePick) ? lanePick : null
  const toggleLane = (key: string) => setLanePick(k => (k === key ? null : key))
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setLanePick(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const laneLabel = lanes.find(l => l.key === selectedLane)?.role ?? ''

  // What arrived since the previous poll of THIS arc: the stage animates it.
  const prev = useRef<{ arc: string; ids: Set<string> } | null>(null)
  const [arrivals, setArrivals] = useState<ArcBoardEntry[]>([])
  useEffect(() => {
    if (!data) return
    const ids = prev.current?.arc === data.arc.id ? prev.current.ids : null
    setArrivals(newEntries(ids, data.entries))
    prev.current = { arc: data.arc.id, ids: new Set(data.entries.map(e => e.id)) }
  }, [data])

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: arcBoardListKey })
    if (arcId) {
      void queryClient.invalidateQueries({ queryKey: arcBoardQueryKey(arcId) })
      void queryClient.invalidateQueries({ queryKey: arcBoardReportKey(arcId) })
    }
  }

  // Create the arc with its open-session lanes in one write (the server joins
  // them to the channel and kicks them off), then start each new-session lane.
  const createMut = useMutation({
    mutationFn: async ({ spec, lanes: picked }: { spec: ArcBoardCreateSpec; lanes: LaneSpec[] }) => {
      const existing = picked.flatMap(l =>
        l.mode === 'existing' ? [{ session_key: l.session_key, role: l.role, nudge: l.nudge }] : [],
      )
      const res = await api.arcBoardCreate({ ...spec, members: existing })
      const roleOf = (key: string) => res.arc.members[key]?.role ?? key
      const notes = res.lanes.map(l => laneJoinNote(roleOf(l.key), l))
      for (const lane of picked) {
        if (lane.mode !== 'new') continue
        try {
          notes.push(laneJoinNote(lane.role, await addLaneToArc(res.arc, lane)))
        } catch (e) {
          notes.push({
            tone: 'warn',
            text: i18nT('pages.arcBoard.lane_create_failed', { lane: lane.role, error: errMessage(e) || '' }),
          })
        }
      }
      return { arc: res.arc, notes }
    },
    onSuccess: res => {
      setShowForm(false)
      setFormError(null)
      setLaneNotes(res.notes)
      setParams({ arc: res.arc.id }, { replace: true })
      void queryClient.invalidateQueries({ queryKey: arcBoardListKey })
      void slots.refetch()
    },
    onError: e => setFormError(errMessage(e) || i18nT('pages.arcBoard.create_failed')),
  })
  const addLane = async (lane: LaneSpec): Promise<boolean> => {
    if (!data) return false
    setLanePending(true)
    setLaneError(null)
    try {
      const joined = await addLaneToArc(data.arc, lane)
      setLaneNotes(n => [laneJoinNote(lane.role, joined), ...n].slice(0, 4))
      return true
    } catch (e) {
      setLaneError(errMessage(e) || i18nT('pages.arcBoard.lane_failed'))
      return false
    } finally {
      setLanePending(false)
      refresh()
      void slots.refetch()
    }
  }
  const openChannelMut = useMutation({
    mutationFn: () => api.arcBoardOpenChannel(arcId),
    onSuccess: res => {
      const roleOf = (key: string) => res.arc.members[key]?.role ?? key
      setLaneNotes(res.lanes.map(l => laneJoinNote(roleOf(l.key), l)))
      refresh()
    },
  })
  const removeLaneMut = useMutation({
    mutationFn: (key: string) => api.arcBoardRemoveMember(arcId, key),
    onSuccess: refresh,
  })
  const autonomyMut = useMutation({
    mutationFn: (level: 1 | 2 | 3) => api.arcBoardAutonomy(arcId, level),
    onSuccess: refresh,
  })
  // Answer, accept and send-back reject on failure so the card that started
  // them keeps its input and shows the error; any outcome re-reads the board
  // (a 409 on accept means the milestone moved under the owner).
  const settle = async <T,>(work: Promise<T>): Promise<void> => {
    try {
      await work
    } finally {
      refresh()
    }
  }

  if (!showForm && arcs.length === 0 && !list.isLoading && !list.isError) {
    return (
      <>
        <PageHeader title={i18nT('pages.arcBoard.page_title')} subtitle={i18nT('pages.arcBoard.subtitle')} />
        <EmptyState
          icon={<Check size={40} />}
          title={i18nT('pages.arcBoard.empty_title')}
          subtitle={i18nT('pages.arcBoard.empty_body')}
          action={
            <Btn type="button" primary onClick={() => setShowForm(true)}>
              {i18nT('pages.arcBoard.new_arc')}
            </Btn>
          }
          testId="arc-board-empty"
        />
      </>
    )
  }

  const ownerAsks = data ? data.open_asks.filter(a => a.to === 'owner').length : 0
  const byId = new Map((data?.entries ?? []).map(e => [e.id, e]))
  const collisionIds = new Set((data?.collisions ?? []).flat())
  const collisionPairs: [string, string][] = (data?.collisions ?? []).flatMap(group => {
    const authors = group.map(id => byId.get(id)?.author).filter((a): a is string => !!a)
    return authors.slice(1).map(b => [authors[0], b] as [string, string])
  })
  const working = lanes.filter(l => l.state === 'working').length

  return (
    <>
      <PageHeader
        title={i18nT('pages.arcBoard.page_title')}
        subtitle={i18nT('pages.arcBoard.subtitle')}
        actions={
          <Btn
            type="button"
            primary
            onClick={() => {
              setFormError(null)
              setShowForm(s => !s)
            }}
          >
            {showForm ? i18nT('pages.arcBoard.form_cancel') : i18nT('pages.arcBoard.new_arc')}
          </Btn>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-8">
        {list.isError ? (
          <ErrorNotice
            title={i18nT('pages.arcBoard.load_failed')}
            message={errMessage(list.error) || ''}
            askAgent
            variant="inline"
          />
        ) : null}

        {showForm ? (
          <NewArcForm
            slots={slots.data ?? []}
            onCancel={() => {
              setShowForm(false)
              setFormError(null)
            }}
            onCreate={(spec, lanes) => createMut.mutate({ spec, lanes })}
            pending={createMut.isPending}
            error={formError}
          />
        ) : (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-[250px_minmax(0,1fr)] 2xl:grid-cols-[260px_minmax(0,1fr)_330px]">
            <ArcRail
              arcs={arcs}
              arcId={arcId}
              onSelectArc={id => {
                setParams({ arc: id }, { replace: true })
                setLanePick(null)
                setLaneNotes([])
                setTab('board')
              }}
              arc={data?.arc ?? null}
              lanes={lanes}
              selectedLane={selectedLane}
              onSelectLane={toggleLane}
              slots={slots.data ?? []}
              onAddLane={addLane}
              onRemoveLane={key => removeLaneMut.mutate(key)}
              laneError={
                laneError ??
                (removeLaneMut.isError ? errMessage(removeLaneMut.error) || i18nT('pages.arcBoard.lane_failed') : null)
              }
              lanePending={lanePending}
              laneNotes={laneNotes}
              channel={data?.channel ?? null}
              onOpenChannel={() => openChannelMut.mutate()}
              channelPending={openChannelMut.isPending}
              channelError={
                openChannelMut.isError
                  ? errMessage(openChannelMut.error) || i18nT('pages.arcBoard.channel_open_failed')
                  : null
              }
              milestoneControl={
                data ? (
                  <MilestoneCard
                    compact
                    key={`${data.arc.id}:${data.arc.milestone_index}`}
                    detail={data}
                    onAccept={expected => settle(api.arcBoardAccept(arcId, expected))}
                    onSendBack={text => settle(api.arcBoardNote(arcId, text))}
                  />
                ) : null
              }
            />

            <div className="flex min-w-0 flex-col gap-3">
              {detail.isLoading && !data ? (
                <div className="text-[13px]" style={{ color: C.dim }}>
                  {i18nT('pages.arcBoard.loading')}
                </div>
              ) : null}
              {detail.isError ? (
                <ErrorNotice
                  title={i18nT('pages.arcBoard.load_failed')}
                  message={errMessage(detail.error) || ''}
                  askAgent
                  variant="inline"
                />
              ) : null}
              {data ? (
                <>
                  <Card className="p-3">
                    <div className="text-[15px] font-semibold" style={{ color: C.text }}>
                      {data.arc.title}
                    </div>
                    {data.arc.goal ? (
                      <ClampText
                        lines={2}
                        className="mt-0.5 text-[12px]"
                        style={{ color: C.dim }}
                        text={`${i18nT('pages.arcBoard.goal')}: ${data.arc.goal}`}
                      />
                    ) : null}
                    {data.arc.lineage ? (
                      <ClampText
                        lines={2}
                        className="text-[12px]"
                        style={{ color: C.dim }}
                        text={`${i18nT('pages.arcBoard.form_lineage')}: ${data.arc.lineage}`}
                      />
                    ) : null}
                    {data.arc.reason ? (
                      <ClampText
                        lines={2}
                        className="text-[12px]"
                        style={{ color: C.dim }}
                        text={`${i18nT('pages.arcBoard.reason')}: ${data.arc.reason}`}
                      />
                    ) : null}
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Counter
                        label={i18nT('pages.arcBoard.counter_working')}
                        value={`${working}/${lanes.length}`}
                        tone={working > 0 ? C.ok : undefined}
                      />
                      <Counter label={i18nT('pages.arcBoard.counter_entries')} value={data.entries.length} />
                      <Counter
                        label={i18nT('pages.arcBoard.counter_asks')}
                        value={ownerAsks}
                        tone={ownerAsks > 0 ? C.warn : undefined}
                      />
                      <Counter
                        label={i18nT('pages.arcBoard.counter_collisions')}
                        value={data.collisions.length}
                        tone={data.collisions.length > 0 ? C.danger : undefined}
                      />
                      <Counter
                        label={i18nT('pages.arcBoard.counter_milestone')}
                        value={`${Math.min(data.arc.milestone_index + 1, data.arc.milestones.length)}/${data.arc.milestones.length}`}
                      />
                    </div>
                  </Card>

                  <div className="flex gap-1" role="tablist">
                    {(['board', 'report'] as const).map(t => (
                      <button
                        key={t}
                        type="button"
                        role="tab"
                        aria-selected={tab === t}
                        onClick={() => setTab(t)}
                        className="rounded-md border px-3 py-1 text-[12px]"
                        style={
                          tab === t
                            ? { borderColor: C.accent, color: C.accent, backgroundColor: C.cardHl }
                            : { borderColor: C.border, color: C.dim }
                        }
                      >
                        {t === 'board' ? i18nT('pages.arcBoard.tab_board') : i18nT('pages.arcBoard.tab_report')}
                      </button>
                    ))}
                  </div>

                  {tab === 'board' ? (
                    <>
                      <Card className="p-2">
                        <ArcStage
                          lanes={lanes}
                          arrivals={arrivals}
                          ownerAsks={ownerAsks}
                          collisions={collisionPairs}
                          selected={selectedLane}
                          onSelect={toggleLane}
                          onClear={() => setLanePick(null)}
                          onOwner={() => needsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
                        />
                      </Card>
                      <BoardFeed
                        key={data.arc.id}
                        arc={data.arc}
                        entries={data.entries}
                        lane={selectedLane}
                        laneLabel={laneLabel}
                        collisionIds={collisionIds}
                      />
                    </>
                  ) : (
                    <ReportTab arc={data.arc} />
                  )}
                </>
              ) : null}
            </div>

            {data ? (
              <div className="lg:col-span-2 2xl:col-span-1">
                <ControlRail
                  detail={data}
                  laneKey={selectedLane}
                  laneLabel={laneLabel}
                  needsRef={needsRef}
                  onAnswer={(askId, text) => settle(api.arcBoardAnswer(arcId, askId, text))}
                  onAccept={expected => settle(api.arcBoardAccept(arcId, expected))}
                  onSendBack={text => settle(api.arcBoardNote(arcId, text))}
                  onAutonomy={level => autonomyMut.mutate(level)}
                  autonomyPending={autonomyMut.isPending}
                  autonomyError={
                    autonomyMut.isError ? errMessage(autonomyMut.error) || i18nT('pages.arcBoard.autonomy_failed') : null
                  }
                />
              </div>
            ) : null}
          </div>
        )}
      </div>
    </>
  )
}
