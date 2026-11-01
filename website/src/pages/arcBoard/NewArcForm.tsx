/** The new-arc form: the same fields the CLI takes, without the JSON. The arc
 *  gets its own channel by default; lanes are new sessions on any agent or
 *  sessions already open, picked like the rail's "Add lane". */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { Btn, Card } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import type { ArcBoardCreateSpec } from '../../api/client/arcBoard'
import type { ChatSlot } from '../../types'
import type { LaneSpec } from './arcBoardModel'
import LanePicker from './LanePicker'
import { AUTONOMY_KEY, C } from './theme'

type ChannelMode = 'new' | 'existing' | 'none'

export default function NewArcForm({
  onCancel,
  onCreate,
  pending,
  error,
  slots,
}: {
  slots: ChatSlot[]
  onCancel: () => void
  onCreate: (spec: ArcBoardCreateSpec, lanes: LaneSpec[]) => void
  pending: boolean
  error: string | null
}) {
  const [title, setTitle] = useState('')
  const [goal, setGoal] = useState('')
  const [reason, setReason] = useState('')
  const [lineage, setLineage] = useState('')
  const [project, setProject] = useState('')
  const [autonomy, setAutonomy] = useState<1 | 2 | 3>(2)
  const [channelMode, setChannelMode] = useState<ChannelMode>('new')
  const [channelId, setChannelId] = useState('')
  const [milestones, setMilestones] = useState<{ name: string; criteria: string }[]>([{ name: '', criteria: '' }])
  const [lanes, setLanes] = useState<LaneSpec[]>([])
  const [picking, setPicking] = useState(false)
  const channels = useQuery({
    queryKey: ['arc-board', 'channels'],
    queryFn: () => api.channelsList() as Promise<{ channels: { id: string; topic: string }[] }>,
    enabled: channelMode === 'existing',
  })

  const canCreate =
    title.trim().length > 0 &&
    milestones.some(m => m.name.trim().length > 0) &&
    (channelMode !== 'existing' || channelId.length > 0)

  const setMilestone = (i: number, patch: Partial<{ name: string; criteria: string }>) =>
    setMilestones(ms => ms.map((m, j) => (j === i ? { ...m, ...patch } : m)))

  const submit = () => {
    if (!canCreate || pending) return
    onCreate(
      {
        title: title.trim(),
        goal: goal.trim() || undefined,
        reason: reason.trim() || undefined,
        lineage: lineage.trim() || undefined,
        project: project.trim() || undefined,
        autonomy,
        channel:
          channelMode === 'existing' ? { mode: 'existing', id: channelId } : { mode: channelMode },
        milestones: milestones
          .filter(m => m.name.trim().length > 0)
          .map(m => ({ name: m.name.trim(), criteria: m.criteria.trim() })),
      },
      lanes,
    )
  }

  const label = (k: string) => (
    <div className="mb-1 text-[11px] font-medium" style={{ color: C.dim }}>
      {i18nT(k)}
    </div>
  )
  const inputCls = 'w-full rounded-md border bg-transparent px-2 py-1.5 text-[13px]'
  const inputStyle = { borderColor: C.border, color: C.text }
  const field = (key: string, value: string, set: (v: string) => void, ph?: string) => (
    <div>
      {label(key)}
      <input
        value={value}
        onChange={e => set(e.target.value)}
        aria-label={i18nT(key)}
        placeholder={ph ? i18nT(ph) : undefined}
        className={inputCls}
        style={inputStyle}
      />
    </div>
  )

  return (
    <Card className="p-3 md:p-5">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {field('pages.arcBoard.form_title_label', title, setTitle, 'pages.arcBoard.form_title_ph')}
        <div>
          {label('pages.arcBoard.form_autonomy')}
          <div className="flex flex-col gap-1.5">
            {([1, 2, 3] as const).map(n => (
              <label key={n} className="flex items-start gap-2 text-[12px]" style={{ color: autonomy === n ? C.accent : C.dim }}>
                <input
                  type="radio"
                  name="arc-autonomy"
                  aria-label={i18nT(AUTONOMY_KEY[n])}
                  checked={autonomy === n}
                  onChange={() => setAutonomy(n)}
                  className="mt-0.5"
                />
                <span>
                  {n} — {i18nT(AUTONOMY_KEY[n])}
                </span>
              </label>
            ))}
          </div>
        </div>
        {field('pages.arcBoard.form_goal', goal, setGoal)}
        {field('pages.arcBoard.form_reason', reason, setReason)}
        {field('pages.arcBoard.form_lineage', lineage, setLineage, 'pages.arcBoard.form_lineage_ph')}
        {field('pages.arcBoard.form_project', project, setProject, 'pages.arcBoard.form_project_ph')}
        <div className="md:col-span-2">
          {label('pages.arcBoard.form_channel')}
          <div className="flex flex-col gap-1.5 md:flex-row md:flex-wrap md:gap-4">
            {(['new', 'existing', 'none'] as const).map(m => (
              <label key={m} className="flex items-center gap-2 text-[12px]" style={{ color: channelMode === m ? C.accent : C.dim }}>
                <input
                  type="radio"
                  name="arc-channel"
                  aria-label={
                    m === 'new'
                      ? i18nT('pages.arcBoard.form_channel_new')
                      : m === 'existing'
                        ? i18nT('pages.arcBoard.form_channel_existing')
                        : i18nT('pages.arcBoard.form_channel_none')
                  }
                  checked={channelMode === m}
                  onChange={() => setChannelMode(m)}
                />
                {m === 'new'
                  ? i18nT('pages.arcBoard.form_channel_new')
                  : m === 'existing'
                    ? i18nT('pages.arcBoard.form_channel_existing')
                    : i18nT('pages.arcBoard.form_channel_none')}
              </label>
            ))}
          </div>
          {channelMode === 'existing' ? (
            <div className="mt-2">
              <SimpleSelect
                options={(channels.data?.channels ?? []).map(ch => ch.id)}
                optionLabels={(channels.data?.channels ?? []).map(ch => ch.topic)}
                value={channelId}
                onChange={setChannelId}
                triggerFallback={i18nT('pages.arcBoard.form_channel_pick')}
                aria-label={i18nT('pages.arcBoard.form_channel_pick')}
              />
            </div>
          ) : null}
          <div className="mt-1 text-[11px]" style={{ color: C.dim }}>
            {i18nT('pages.arcBoard.form_channel_hint')}
          </div>
        </div>
      </div>

      <div className="mt-4">
        {label('pages.arcBoard.form_milestones')}
        {milestones.map((m, i) => (
          <div key={i} className="mb-1.5 flex flex-col gap-1.5 md:flex-row">
            <input
              value={m.name}
              onChange={e => setMilestone(i, { name: e.target.value })}
              aria-label={i18nT('pages.arcBoard.form_milestone_name', { n: i + 1 })}
              placeholder={i18nT('pages.arcBoard.form_milestone_name', { n: i + 1 })}
              className={inputCls}
              style={inputStyle}
            />
            <input
              value={m.criteria}
              onChange={e => setMilestone(i, { criteria: e.target.value })}
              aria-label={i18nT('pages.arcBoard.form_criteria_ph')}
              placeholder={i18nT('pages.arcBoard.form_criteria_ph')}
              className={inputCls}
              style={inputStyle}
            />
            {milestones.length > 1 ? (
              <Btn
                type="button"
                danger
                aria-label={i18nT('pages.arcBoard.remove')}
                onClick={() => setMilestones(ms => ms.filter((_, j) => j !== i))}
                className="shrink-0 px-1.5 py-1 text-[11px]"
              >
                <Trash2 size={12} />
              </Btn>
            ) : null}
          </div>
        ))}
        <Btn type="button" onClick={() => setMilestones(ms => [...ms, { name: '', criteria: '' }])} className="mt-1 px-2 py-1 text-[11px]">
          <Plus size={12} />
          {i18nT('pages.arcBoard.form_add_milestone')}
        </Btn>
      </div>

      <div className="mt-4">
        {label('pages.arcBoard.form_lanes')}
        {lanes.map((l, i) => (
          <div key={i} className="mb-1.5 flex items-center gap-1.5">
            <span className="min-w-0 flex-1 truncate text-[12px]" style={{ color: C.text }}>
              {l.role}
              <span className="ml-1.5 text-[10px]" style={{ color: C.dim }}>
                {l.mode === 'new'
                  ? i18nT('pages.arcBoard.lane_new_on', { agent: l.label })
                  : i18nT('pages.arcBoard.lane_existing', { session: l.label })}
                {l.nudge ? ` · ${i18nT('pages.arcBoard.nudge_on')}` : ''}
              </span>
            </span>
            <Btn
              type="button"
              danger
              aria-label={i18nT('pages.arcBoard.remove_lane', { lane: l.role })}
              onClick={() => setLanes(ls => ls.filter((_, j) => j !== i))}
              className="shrink-0 px-1.5 py-1 text-[11px]"
            >
              <Trash2 size={12} />
            </Btn>
          </div>
        ))}
        {picking ? (
          <LanePicker
            slots={slots}
            members={lanes.flatMap(l => (l.mode === 'existing' ? [l.session_key] : []))}
            onAdd={lane => {
              setLanes(ls => [...ls, lane])
              setPicking(false)
            }}
            onClose={() => setPicking(false)}
            pending={false}
            error={null}
          />
        ) : (
          <Btn type="button" onClick={() => setPicking(true)} className="mt-1 px-2 py-1 text-[11px]">
            <Plus size={12} />
            {i18nT('pages.arcBoard.form_add_lane')}
          </Btn>
        )}
      </div>

      {error ? (
        <div className="mt-3 text-[12px]" style={{ color: C.warn }}>
          {error}
        </div>
      ) : null}

      <div className="mt-5 flex items-center gap-2">
        <Btn type="button" primary disabled={!canCreate || pending} onClick={submit}>
          {i18nT('pages.arcBoard.form_create')}
        </Btn>
        <Btn type="button" disabled={pending} onClick={onCancel}>
          {i18nT('pages.arcBoard.form_cancel')}
        </Btn>
      </div>
    </Card>
  )
}
