/**
 * ArcBoardPage — the owner's console over the durable arc boards.
 *
 * Three panes, the shape the owner approved in the console prototype: the left
 * rail lists the arcs and where each stands; the middle holds the selected
 * arc's milestones, its live entry feed and the history scrubber; the right
 * rail holds what is waiting on the owner (the asks lanes addressed to
 * `owner`) and the arc's own brief. The new-arc form replaces the rail
 * without any JSON: title, goal, reason, autonomy, milestones and lanes.
 *
 * Everything the owner sees here is what the store keeps — the entries are the
 * lanes' own board posts, redacted server-side the same way the members'
 * routes redact them — and everything the owner can do here (answer an ask,
 * accept a milestone, create an arc, add a lane) is a plain write to the same
 * store the `kirocrew arc` CLI and the lanes' MCP tools share. The page adds
 * no state of its own: the board IS the state, and a refresh is a re-read.
 */

import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Plus, Trash2 } from 'lucide-react'

import { Badge, Btn, Card, CardTitle, EmptyState, PageHeader } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { errMessage } from '../utils/thunkError'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { fmtRelative } from '../i18n/format'
import {
  ARC_BOARD_POLL_MS,
  arcBoardListKey,
  arcBoardQueryKey,
  type ArcBoardArc,
  type ArcBoardAsk,
  type ArcBoardCreateSpec,
  type ArcBoardDetail,
  type ArcBoardEntry,
  type ArcBoardSummary,
} from '../api/client/arcBoard'

/** Theme tokens only; a literal hex fails `no-raw-colors` and would ignore
 *  the user's theme. */
const C = {
  text: 'var(--text)',
  dim: 'var(--text-dim)',
  border: 'var(--border)',
  cardHl: 'var(--card-hl)',
  accent: 'var(--accent)',
  warn: 'var(--warn)',
} as const

/** Store vocabulary, shown verbatim like the crew board shows its statuses.
 *  Only the chip colour is ours. */
const KIND_VARIANT = {
  CLAIM: 'aim',
  FACT: 'muted',
  FAIL: 'err',
  ASK: 'warn',
  DONE: 'ok',
} as const
function kindVariant(kind: ArcBoardEntry['kind']): keyof typeof KIND_VARIANT | 'muted' {
  return (KIND_VARIANT as Record<string, keyof typeof KIND_VARIANT>)[kind] ?? 'muted'
}

/** Autonomy is a store field with exactly three values; the brief renders the
 *  same three sentences, so the console and the lane's own brief cannot say
 *  two different things. Literal keys — assembling one is what
 *  `dynamicKeys.test.ts` forbids. */
const AUTONOMY_KEY = {
  1: 'pages.arcBoard.autonomy_1',
  2: 'pages.arcBoard.autonomy_2',
  3: 'pages.arcBoard.autonomy_3',
} as const

/** The owner is an author like any lane; the others are named by the role the
 *  owner gave them when they joined the arc. */
function authorLabel(arc: ArcBoardArc, author: string): string {
  if (author === 'owner') return i18nT('pages.arcBoard.owner')
  return arc.members[author]?.role ?? author
}

function EntryRow({ arc, entry }: { arc: ArcBoardArc; entry: ArcBoardEntry }) {
  return (
    <div className="border-b px-3 py-2" style={{ borderColor: C.border }}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <Badge variant={kindVariant(entry.kind)} className="px-1.5 text-[10px]">
          {entry.kind}
        </Badge>
        <span className="text-[12px] font-medium" style={{ color: C.text }}>
          {authorLabel(arc, entry.author)}
        </span>
        {entry.to ? (
          <span className="text-[11px]" style={{ color: C.dim }}>
            {i18nT('pages.arcBoard.to', { to: entry.to === 'owner' ? i18nT('pages.arcBoard.owner') : entry.to })}
          </span>
        ) : null}
        <span className="ml-auto shrink-0 text-[11px]" style={{ color: C.dim }}>
          {fmtRelative(entry.ts)}
        </span>
      </div>
      {entry.text ? (
        <div className="mt-1 whitespace-pre-wrap text-[12px] leading-5" style={{ color: C.text }}>
          {entry.text}
        </div>
      ) : null}
      {entry.step ? (
        <div className="mt-0.5 text-[11px]" style={{ color: C.dim }}>
          {i18nT('pages.arcBoard.step')}: {entry.step}
        </div>
      ) : null}
      {Array.isArray(entry.checked) && entry.checked.length > 0 ? (
        <div className="mt-0.5 text-[11px]" style={{ color: C.dim }}>
          {entry.checked.join(' · ')}
        </div>
      ) : null}
      {entry.link ? (
        <a
          href={entry.link}
          target="_blank"
          rel="noreferrer"
          className="mt-1 inline-block text-[11px] underline-offset-2 hover:underline"
          style={{ color: C.accent }}
        >
          {entry.link}
        </a>
      ) : null}
      {entry.reply_to ? (
        <div className="mt-0.5 text-[11px]" style={{ color: C.dim }}>
          ↩ {entry.reply_to}
        </div>
      ) : null}
    </div>
  )
}

/** One open ask the owner has to answer: the question, who asked, who has
 *  seen it, and the answer box. The answer is a FACT replying to the ask, so
 *  the feed below shows the exchange in order. */
function AskCard({
  arc,
  ask,
  onAnswer,
  answering,
  error,
}: {
  arc: ArcBoardArc
  ask: ArcBoardAsk
  onAnswer: (askId: string, text: string) => void
  answering: boolean
  error: string | null
}) {
  const [text, setText] = useState('')
  const tooLong = text.length > 100
  return (
    <div className="border-b px-3 py-2" style={{ borderColor: C.border }}>
      <div className="text-[12px] leading-5" style={{ color: C.text }}>
        {ask.text}
      </div>
      <div className="mt-1 flex flex-wrap items-baseline gap-x-2 text-[11px]" style={{ color: C.dim }}>
        <span>{authorLabel(arc, ask.author)}</span>
        <span>{fmtRelative(ask.ts)}</span>
        {ask.seen_by.length > 0 ? (
          <span>{i18nT('pages.arcBoard.seen_by', { who: ask.seen_by.join(', ') })}</span>
        ) : null}
      </div>
      <div className="mt-2 flex items-start gap-1.5">
        <textarea
          value={text}
          onChange={e => setText(e.target.value)}
          aria-label={i18nT('pages.arcBoard.answer_ph')}
          rows={2}
          placeholder={i18nT('pages.arcBoard.answer_ph')}
          className="w-full resize-none rounded-md border bg-transparent px-2 py-1 text-[12px]"
          style={{ borderColor: C.border, color: C.text }}
        />
        <Btn
          type="button"
          primary
          disabled={text.trim().length === 0 || tooLong || answering}
          onClick={() => {
            onAnswer(ask.id, text.trim())
            setText('')
          }}
          className="shrink-0 px-2 py-1 text-[11px]"
        >
          {i18nT('pages.arcBoard.send')}
        </Btn>
      </div>
      {tooLong ? (
        <div className="mt-1 text-[11px]" style={{ color: C.warn }}>
          {i18nT('pages.arcBoard.answer_too_long')}
        </div>
      ) : null}
      {error ? (
        <div className="mt-1 text-[11px]" style={{ color: C.warn }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}

/** The middle + right panes of the selected arc. */
function ArcDetail({
  detail,
  onAccept,
  onAnswer,
  answering,
  answerError,
  acceptError,
}: {
  detail: ArcBoardDetail
  onAccept: () => void
  onAnswer: (askId: string, text: string) => void
  answering: boolean
  answerError: string | null
  acceptError: string | null
}) {
  const { arc, brief, entries, open_asks, open_claims } = detail
  const milestones = arc.milestones
  const index = arc.milestone_index
  const complete = index >= milestones.length
  // Newest first: the board is read from the top, like a feed. The scrubber
  // then reaches BACKWARD — value 1 is "only the latest entry", the full
  // length is "everything since the arc started".
  const [visible, setVisible] = useState(entries.length)
  const total = entries.length
  const shown = entries.slice(Math.max(0, total - (visible >= 1 ? visible : 1))).reverse()
  const ownerAsks = open_asks.filter(a => a.to === 'owner')

  return (
    <div className="flex flex-col gap-4 md:flex-row">
      {/* Middle: milestones, the feed, the scrubber. */}
      <div className="min-w-0 flex-1">
        <Card className="p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="text-[14px] font-semibold" style={{ color: C.text }}>
                {arc.title}
              </div>
              {arc.goal ? (
                <div className="mt-0.5 text-[12px]" style={{ color: C.dim }}>
                  {i18nT('pages.arcBoard.goal')}: {arc.goal}
                </div>
              ) : null}
              {arc.reason ? (
                <div className="text-[12px]" style={{ color: C.dim }}>
                  {i18nT('pages.arcBoard.reason')}: {arc.reason}
                </div>
              ) : null}
            </div>
            <div className="flex items-center gap-2">
              <Badge variant={complete ? 'ok' : 'aim'} className="text-[11px]">
                {complete
                  ? i18nT('pages.arcBoard.all_accepted')
                  : i18nT('pages.arcBoard.milestone_pos', {
                      done: index,
                      total: milestones.length,
                    })}
              </Badge>
              {!complete ? (
                <Btn type="button" primary onClick={onAccept} className="px-2 py-1 text-[11px]">
                  <Check size={13} />
                  {i18nT('pages.arcBoard.accept')}
                </Btn>
              ) : null}
            </div>
          </div>

          {/* The milestone strip: done, current, ahead. */}
          <div className="mt-3 flex flex-wrap gap-1.5">
            {milestones.map((m, i) => (
              <span
                key={i}
                title={m.criteria || m.name}
                className="inline-flex items-center gap-1 rounded-full border px-2 py-[2px] text-[11px]"
                style={
                  i < index
                    ? { borderColor: C.border, color: C.dim }
                    : i === index
                      ? { borderColor: C.accent, color: C.accent }
                      : { borderColor: C.border, color: C.dim, opacity: 0.55 }
                }
              >
                {i < index ? <Check size={11} /> : null}
                {m.name}
              </span>
            ))}
          </div>
          {acceptError ? (
            <div className="mt-2 text-[11px]" style={{ color: C.warn }}>
              {acceptError}
            </div>
          ) : null}
        </Card>

        <Card className="p-0">
          <CardTitle className="px-3 pt-3">
            {i18nT('pages.arcBoard.board')}
            <span className="ml-1 font-normal" style={{ color: C.dim }}>
              {i18nT('pages.arcBoard.entries_n', { n: total })}
            </span>
          </CardTitle>
          {total === 0 ? (
            <div className="px-3 pb-3 text-[12px]" style={{ color: C.dim }}>
              {i18nT('pages.arcBoard.board_empty')}
            </div>
          ) : (
            <>
              <div className="max-h-[55vh] overflow-y-auto">
                {shown.map(e => (
                  <EntryRow key={e.id} arc={arc} entry={e} />
                ))}
              </div>
              {/* The history scrubber: slide back through the feed in order. */}
              <div className="flex items-center gap-2 px-3 pb-3 pt-2">
                <span className="shrink-0 text-[11px]" style={{ color: C.dim }}>
                  {i18nT('pages.arcBoard.history')}
                </span>
                <input
                  type="range"
                  min={1}
                  max={total}
                  value={Math.min(visible, total)}
                  onChange={e => setVisible(Number(e.target.value))}
                  className="w-full"
                  aria-label={i18nT('pages.arcBoard.history')}
                />
                <span className="shrink-0 text-[11px] tabular-nums" style={{ color: C.dim }}>
                  {Math.min(visible, total)}/{total}
                </span>
              </div>
            </>
          )}
        </Card>
      </div>

      {/* Right rail: what needs the owner, the lanes' open claims, the brief. */}
      <div className="w-full shrink-0 md:w-[300px]">
        <Card className="p-0">
          <CardTitle className="px-3 pt-3">{i18nT('pages.arcBoard.needs_you')}</CardTitle>
          {ownerAsks.length === 0 ? (
            <div className="px-3 pb-3 text-[12px]" style={{ color: C.dim }}>
              {i18nT('pages.arcBoard.nothing_pending')}
            </div>
          ) : (
            <>
              {ownerAsks.map(a => (
                <AskCard key={a.id} arc={arc} ask={a} onAnswer={onAnswer} answering={answering} error={answerError} />
              ))}
              {open_asks.length > ownerAsks.length ? (
                <div className="px-3 pb-2 text-[11px]" style={{ color: C.dim }}>
                  {i18nT('pages.arcBoard.other_asks', { n: open_asks.length - ownerAsks.length })}
                </div>
              ) : null}
            </>
          )}
        </Card>

        {open_claims.length > 0 ? (
          <Card className="p-0">
            <CardTitle className="px-3 pt-3">{i18nT('pages.arcBoard.open_claims')}</CardTitle>
            {open_claims.map(c => (
              <div key={c.id} className="border-b px-3 py-2" style={{ borderColor: C.border }}>
                <div className="text-[12px]" style={{ color: C.text }}>
                  {c.step || c.text}
                </div>
                <div className="mt-0.5 text-[11px]" style={{ color: C.dim }}>
                  {authorLabel(arc, c.author)} · {fmtRelative(c.ts)}
                </div>
              </div>
            ))}
          </Card>
        ) : null}

        {brief ? (
          <Card className="p-3">
            <CardTitle>{i18nT('pages.arcBoard.brief')}</CardTitle>
            <div className="max-h-[38vh] overflow-y-auto whitespace-pre-wrap text-[12px] leading-5" style={{ color: C.dim }}>
              {brief}
            </div>
          </Card>
        ) : null}
      </div>
    </div>
  )
}

/** The new-arc form: the same fields the CLI takes, without the JSON. */
function NewArcForm({
  onCancel,
  onCreate,
  pending,
  error,
}: {
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

  const canCreate = title.trim().length > 0 && milestones.some(m => m.name.trim().length > 0)

  const setMilestone = (i: number, patch: Partial<{ name: string; criteria: string }>) =>
    setMilestones(ms => ms.map((m, j) => (j === i ? { ...m, ...patch } : m)))
  const setLane = (i: number, patch: Partial<{ session_key: string; role: string }>) =>
    setLanes(ls => ls.map((l, j) => (j === i ? { ...l, ...patch } : l)))

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
      members: lanes
        .filter(l => l.session_key.trim().length > 0)
        .map(l => ({ session_key: l.session_key.trim(), role: l.role.trim() })),
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
          <div key={i} className="mb-1.5 flex flex-col gap-1.5 md:flex-row">
            <input
              value={l.session_key}
              onChange={e => setLane(i, { session_key: e.target.value })}
              aria-label={i18nT('pages.arcBoard.form_lane_key_ph')}
              placeholder={i18nT('pages.arcBoard.form_lane_key_ph')}
              className={inputCls}
              style={inputStyle}
            />
            <input
              value={l.role}
              onChange={e => setLane(i, { role: e.target.value })}
              aria-label={i18nT('pages.arcBoard.form_lane_role_ph')}
              placeholder={i18nT('pages.arcBoard.form_lane_role_ph')}
              className={inputCls}
              style={inputStyle}
            />
            <Btn
              type="button"
              danger
              onClick={() => setLanes(ls => ls.filter((_, j) => j !== i))}
              className="shrink-0 px-1.5 py-1 text-[11px]"
            >
              <Trash2 size={12} />
            </Btn>
          </div>
        ))}
        <Btn type="button" onClick={() => setLanes(ls => [...ls, { session_key: '', role: '' }])} className="mt-1 px-2 py-1 text-[11px]">
          <Plus size={12} />
          {i18nT('pages.arcBoard.form_add_lane')}
        </Btn>
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

export default function ArcBoardPage() {
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [showForm, setShowForm] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

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

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: arcBoardListKey })
    if (arcId) void queryClient.invalidateQueries({ queryKey: arcBoardQueryKey(arcId) })
  }

  const createMut = useMutation({
    mutationFn: (spec: ArcBoardCreateSpec) => api.arcBoardCreate(spec),
    onSuccess: res => {
      setShowForm(false)
      setFormError(null)
      setParams({ arc: res.arc.id }, { replace: true })
      void queryClient.invalidateQueries({ queryKey: arcBoardListKey })
    },
    onError: e => setFormError(errMessage(e) || i18nT('pages.arcBoard.create_failed')),
  })
  const answerMut = useMutation({
    mutationFn: ({ askId, text }: { askId: string; text: string }) => api.arcBoardAnswer(arcId, askId, text),
    onSuccess: invalidate,
  })
  const acceptMut = useMutation({
    mutationFn: () => api.arcBoardAccept(arcId),
    onSuccess: invalidate,
  })

  return (
    <>
      <PageHeader
        title={i18nT('pages.arcBoard.page_title')}
        subtitle={i18nT('pages.arcBoard.subtitle')}
        actions={
          <Btn type="button" primary onClick={() => { setFormError(null); setShowForm(s => !s) }}>
            {showForm ? i18nT('pages.arcBoard.form_cancel') : i18nT('pages.arcBoard.new_arc')}
          </Btn>
        }
      />
      <div className="px-4 pb-8 overflow-y-auto flex-1 min-h-0">
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
            onCancel={() => { setShowForm(false); setFormError(null) }}
            onCreate={spec => createMut.mutate(spec)}
            pending={createMut.isPending}
            error={formError}
          />
        ) : (
          <>
            {arcs.length === 0 && !list.isLoading ? (
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
            ) : null}

            {arcs.length > 0 ? (
              <div className="flex flex-col gap-4">
                {/* Left rail: the arcs and where each stands. */}
                <div className="flex flex-col gap-1.5 md:flex-row md:flex-wrap">
                  {arcs.map(a => (
                    <button
                      key={a.id}
                      type="button"
                      onClick={() => setParams({ arc: a.id }, { replace: true })}
                      className="min-w-[220px] flex-1 rounded-lg border px-3 py-2 text-left"
                      style={
                        a.id === arcId
                          ? { borderColor: C.accent, backgroundColor: C.cardHl }
                          : { borderColor: C.border }
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
                        {a.milestone ? i18nT('pages.arcBoard.on_milestone', { m: a.milestone }) : i18nT('pages.arcBoard.all_accepted')}
                      </div>
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        {a.open_asks > 0 ? (
                          <Badge variant="warn" className="px-1.5 text-[10px]">
                            {i18nT('pages.arcBoard.asks_n', { n: a.open_asks })}
                          </Badge>
                        ) : null}
                        {a.open_claims > 0 ? (
                          <Badge variant="muted" className="px-1.5 text-[10px]">
                            {i18nT('pages.arcBoard.claims_n', { n: a.open_claims })}
                          </Badge>
                        ) : null}
                        <span className="text-[11px]" style={{ color: C.dim }}>
                          {i18nT('pages.arcBoard.lanes_n', { n: a.members })}
                        </span>
                      </div>
                    </button>
                  ))}
                </div>

                {detail.isLoading ? (
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
                {detail.data ? (
                  <ArcDetail
                    detail={detail.data}
                    onAccept={() => acceptMut.mutate()}
                    onAnswer={(askId, text) => answerMut.mutate({ askId, text })}
                    answering={answerMut.isPending}
                    answerError={answerMut.isError ? errMessage(answerMut.error) || i18nT('pages.arcBoard.answer_failed') : null}
                    acceptError={acceptMut.isError ? errMessage(acceptMut.error) || i18nT('pages.arcBoard.accept_failed') : null}
                  />
                ) : null}
              </div>
            ) : null}
          </>
        )}
      </div>
    </>
  )
}
