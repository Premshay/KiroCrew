/** The new-arc form: the same fields the CLI takes, without the JSON. Lanes
 *  are picked from the live sessions, like the rail's "Add lane". */

import { useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import { Btn, Card } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import type { ArcBoardCreateSpec } from '../../api/client/arcBoard'
import type { ChatSlot } from '../../types'
import LanePicker from './LanePicker'
import { AUTONOMY_KEY, C } from './theme'

export default function NewArcForm({
  onCancel,
  onCreate,
  pending,
  error,
  slots,
}: {
  slots: ChatSlot[]
  onCancel: () => void
  onCreate: (spec: ArcBoardCreateSpec) => void
  pending: boolean
  error: string | null
}) {
  const [title, setTitle] = useState('')
  const [goal, setGoal] = useState('')
  const [reason, setReason] = useState('')
  const [lineage, setLineage] = useState('')
  const [autonomy, setAutonomy] = useState<1 | 2 | 3>(2)
  const [milestones, setMilestones] = useState<{ name: string; criteria: string }[]>([
    { name: '', criteria: '' },
  ])
  const [lanes, setLanes] = useState<{ session_key: string; role: string }[]>([])
  const [picking, setPicking] = useState(false)

  const canCreate = title.trim().length > 0 && milestones.some(m => m.name.trim().length > 0)

  const setMilestone = (i: number, patch: Partial<{ name: string; criteria: string }>) =>
    setMilestones(ms => ms.map((m, j) => (j === i ? { ...m, ...patch } : m)))

  const submit = () => {
    if (!canCreate || pending) return
    onCreate({
      title: title.trim(),
      goal: goal.trim() || undefined,
      reason: reason.trim() || undefined,
      lineage: lineage.trim() || undefined,
      autonomy,
      milestones: milestones
        .filter(m => m.name.trim().length > 0)
        .map(m => ({ name: m.name.trim(), criteria: m.criteria.trim() })),
      members: lanes,
    })
  }

  const label = (k: string) => (
    <div className="mb-1 text-[11px] font-medium" style={{ color: C.dim }}>
      {i18nT(k)}
    </div>
  )
  const inputCls = 'w-full rounded-md border bg-transparent px-2 py-1.5 text-[13px]'
  const inputStyle = { borderColor: C.border, color: C.text }

  return (
    <Card className="p-3 md:p-5">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div>
          {label('pages.arcBoard.form_title_label')}
          <input
            value={title}
            onChange={e => setTitle(e.target.value)}
            aria-label={i18nT('pages.arcBoard.form_title_label')}
            placeholder={i18nT('pages.arcBoard.form_title_ph')}
            className={inputCls}
            style={inputStyle}
          />
        </div>
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
        <div>
          {label('pages.arcBoard.form_goal')}
          <input value={goal} onChange={e => setGoal(e.target.value)} aria-label={i18nT('pages.arcBoard.form_goal')} className={inputCls} style={inputStyle} />
        </div>
        <div>
          {label('pages.arcBoard.form_reason')}
          <input value={reason} onChange={e => setReason(e.target.value)} aria-label={i18nT('pages.arcBoard.form_reason')} className={inputCls} style={inputStyle} />
        </div>
        <div>
          {label('pages.arcBoard.form_lineage')}
          <input
            value={lineage}
            onChange={e => setLineage(e.target.value)}
            aria-label={i18nT('pages.arcBoard.form_lineage')}
            placeholder={i18nT('pages.arcBoard.form_lineage_ph')}
            className={inputCls}
            style={inputStyle}
          />
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
          <div key={l.session_key} className="mb-1.5 flex items-center gap-1.5">
            <span className="min-w-0 flex-1 truncate text-[12px]" style={{ color: C.text }}>
              {l.role}
              <span className="ml-1.5 font-mono text-[10px]" style={{ color: C.dim }}>
                {l.session_key}
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
            members={lanes.map(l => l.session_key)}
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
