/**
 * Right rail: what is waiting on the owner (asks addressed to the owner, and
 * the current milestone with what the lanes reported as done), the arc's
 * autonomy, the selected lane's per-turn brief with its share of the cap, and
 * the flags (colliding claims, recent FAILs).
 */

import { useState } from 'react'
import type React from 'react'
import { Check } from 'lucide-react'
import { Badge, Btn, Card, CardTitle } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { fmtRelative } from '../../i18n/format'
import type { ArcBoardArc, ArcBoardAsk, ArcBoardDetail } from '../../api/client/arcBoard'
import { BRIEF_CAP } from './arcBoardModel'
import { EntryLink, authorLabel } from './EntryRow'
import { AUTONOMY_KEY, C } from './theme'

const TEXT_MAX = 100

/** One open ask addressed to the owner. The answer box clears only once the
 *  answer is stored, so a failed send keeps what the owner typed. */
function AskCard({
  arc,
  ask,
  onAnswer,
}: {
  arc: ArcBoardArc
  ask: ArcBoardAsk
  onAnswer: (askId: string, text: string) => Promise<void>
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const tooLong = text.length > TEXT_MAX
  const send = async () => {
    setBusy(true)
    setError(null)
    try {
      await onAnswer(ask.id, text.trim())
      setText('')
    } catch (e) {
      setError((e as Error)?.message || i18nT('pages.arcBoard.answer_failed'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="border-b px-3 py-2" style={{ borderColor: C.border }}>
      <div className="text-[12px] leading-5" style={{ color: C.text }}>
        {ask.text}
      </div>
      <div className="mt-1 flex flex-wrap items-baseline gap-x-2 text-[11px]" style={{ color: C.dim }}>
        <span>{authorLabel(arc, ask.author)}</span>
        <span>{i18nT('pages.arcBoard.waiting', { ago: fmtRelative(ask.ts) })}</span>
        {ask.seen_by.length > 0 ? (
          <span>{i18nT('pages.arcBoard.seen_by', { who: ask.seen_by.map(k => authorLabel(arc, k)).join(', ') })}</span>
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
          disabled={text.trim().length === 0 || tooLong || busy}
          onClick={() => void send()}
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

/** The current milestone: criteria, what lanes reported, and the owner's call.
 *  Accept is two-step (it cannot be undone) and sends the index the owner saw. */
function MilestoneCard({
  detail,
  onAccept,
  onSendBack,
}: {
  detail: ArcBoardDetail
  onAccept: (expectedIndex: number) => Promise<void>
  onSendBack: (text: string) => Promise<void>
}) {
  const { arc, milestone_reports: reports } = detail
  const index = arc.milestone_index
  const m = arc.milestones[index]
  const [confirming, setConfirming] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!m) {
    return (
      <div className="px-3 py-2 text-[12px]" style={{ color: C.ok }}>
        {i18nT('pages.arcBoard.all_accepted')}
      </div>
    )
  }
  const run = async (work: () => Promise<void>, fallbackKey: 'accept' | 'note') => {
    setBusy(true)
    setError(null)
    try {
      await work()
      setConfirming(false)
      setNote(null)
    } catch (e) {
      setError(
        (e as Error)?.message ||
          (fallbackKey === 'accept' ? i18nT('pages.arcBoard.accept_failed') : i18nT('pages.arcBoard.note_failed')),
      )
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="border-b px-3 py-2" style={{ borderColor: C.border }}>
      <div className="flex items-center gap-2">
        <span className="text-[12px] font-semibold" style={{ color: C.text }}>
          {i18nT('pages.arcBoard.milestone_n', { n: index + 1, name: m.name })}
        </span>
        {reports.length > 0 ? (
          <Badge variant="ok" className="px-1.5 text-[10px]">
            {i18nT('pages.arcBoard.reported_n', { n: reports.length })}
          </Badge>
        ) : null}
      </div>
      {m.criteria ? (
        <div className="mt-1 whitespace-pre-wrap text-[11px]" style={{ color: C.dim }}>
          {i18nT('pages.arcBoard.criteria')}: {m.criteria}
        </div>
      ) : null}
      {reports.map(r => (
        <div key={r.id} className="mt-1.5 text-[11px]" style={{ color: C.text }}>
          <span style={{ color: C.ok }}>✓</span> {authorLabel(arc, r.author)}: {r.text}
          {r.link ? <EntryLink link={r.link} /> : null}
        </div>
      ))}
      {note !== null ? (
        <div className="mt-2">
          <textarea
            value={note}
            onChange={e => setNote(e.target.value)}
            rows={2}
            aria-label={i18nT('pages.arcBoard.send_back_ph')}
            placeholder={i18nT('pages.arcBoard.send_back_ph')}
            className="w-full resize-none rounded-md border bg-transparent px-2 py-1 text-[12px]"
            style={{ borderColor: C.border, color: C.text }}
          />
          {note.length > TEXT_MAX ? (
            <div className="text-[11px]" style={{ color: C.warn }}>
              {i18nT('pages.arcBoard.answer_too_long')}
            </div>
          ) : null}
          <div className="mt-1 flex gap-1.5">
            <Btn
              type="button"
              primary
              disabled={busy || !note.trim() || note.length > TEXT_MAX}
              onClick={() => void run(() => onSendBack(note.trim()), 'note')}
              className="px-2 py-1 text-[11px]"
            >
              {i18nT('pages.arcBoard.send')}
            </Btn>
            <Btn type="button" onClick={() => setNote(null)} className="px-2 py-1 text-[11px]">
              {i18nT('pages.arcBoard.form_cancel')}
            </Btn>
          </div>
        </div>
      ) : confirming ? (
        <div className="mt-2">
          <div className="text-[11px]" style={{ color: C.warn }}>
            {i18nT('pages.arcBoard.accept_confirm', { name: m.name })}
          </div>
          <div className="mt-1 flex gap-1.5">
            <Btn
              type="button"
              primary
              disabled={busy}
              onClick={() => void run(() => onAccept(index), 'accept')}
              className="px-2 py-1 text-[11px]"
            >
              <Check size={12} />
              {i18nT('pages.arcBoard.accept')}
            </Btn>
            <Btn type="button" onClick={() => setConfirming(false)} className="px-2 py-1 text-[11px]">
              {i18nT('pages.arcBoard.form_cancel')}
            </Btn>
          </div>
        </div>
      ) : (
        <div className="mt-2 flex flex-wrap gap-1.5">
          <Btn type="button" primary onClick={() => setConfirming(true)} className="px-2 py-1 text-[11px]">
            <Check size={12} />
            {i18nT('pages.arcBoard.accept')}
          </Btn>
          <Btn type="button" onClick={() => setNote('')} className="px-2 py-1 text-[11px]">
            {i18nT('pages.arcBoard.send_back')}
          </Btn>
        </div>
      )}
      {error ? (
        <div className="mt-1 text-[11px]" style={{ color: C.warn }}>
          {error}
        </div>
      ) : null}
    </div>
  )
}

export default function ControlRail({
  detail,
  laneKey,
  laneLabel,
  onAnswer,
  onAccept,
  onSendBack,
  onAutonomy,
  autonomyPending,
  autonomyError,
  needsRef,
}: {
  detail: ArcBoardDetail
  laneKey: string | null
  laneLabel: string
  onAnswer: (askId: string, text: string) => Promise<void>
  onAccept: (expectedIndex: number) => Promise<void>
  onSendBack: (text: string) => Promise<void>
  onAutonomy: (level: 1 | 2 | 3) => void
  autonomyPending: boolean
  autonomyError: string | null
  needsRef: React.RefObject<HTMLDivElement>
}) {
  const { arc, open_asks, briefs, collisions, entries } = detail
  const ownerAsks = open_asks.filter(a => a.to === 'owner')
  const brief = laneKey ? briefs[laneKey] ?? '' : ''
  const byId = new Map(entries.map(e => [e.id, e]))
  const dayAgo = Date.now() - 24 * 3600 * 1000
  const fails = entries.filter(e => e.kind === 'FAIL' && Date.parse(e.ts) >= dayAgo).slice(-5).reverse()
  const share = Math.min(100, Math.round((brief.length / BRIEF_CAP) * 100))

  return (
    <div className="flex flex-col gap-3">
      <div ref={needsRef}>
        <Card className="p-0">
          <CardTitle className="px-3 pt-3">
            {i18nT('pages.arcBoard.needs_you')}
            {ownerAsks.length > 0 ? (
              <Badge variant="warn" className="ml-2 px-1.5 text-[10px]">
                {ownerAsks.length}
              </Badge>
            ) : null}
          </CardTitle>
          {ownerAsks.map(a => (
            <AskCard key={a.id} arc={arc} ask={a} onAnswer={onAnswer} />
          ))}
          <MilestoneCard key={`${arc.id}:${arc.milestone_index}`} detail={detail} onAccept={onAccept} onSendBack={onSendBack} />
          {open_asks.length > ownerAsks.length ? (
            <div className="px-3 py-2 text-[11px]" style={{ color: C.dim }}>
              {i18nT('pages.arcBoard.other_asks', { n: open_asks.length - ownerAsks.length })}
            </div>
          ) : null}
        </Card>
      </div>

      <Card className="p-3">
        <CardTitle>{i18nT('pages.arcBoard.form_autonomy')}</CardTitle>
        <div className="flex flex-col gap-1.5" role="radiogroup" aria-label={i18nT('pages.arcBoard.form_autonomy')}>
          {([1, 2, 3] as const).map(n => (
            <label
              key={n}
              className="flex items-start gap-2 text-[12px]"
              style={{ color: arc.autonomy === n ? C.accent : C.dim }}
            >
              <input
                type="radio"
                name={`arc-autonomy-${arc.id}`}
                aria-label={i18nT(AUTONOMY_KEY[n])}
                checked={arc.autonomy === n}
                disabled={autonomyPending}
                onChange={() => onAutonomy(n)}
                className="mt-0.5"
              />
              <span>
                {n} — {i18nT(AUTONOMY_KEY[n])}
              </span>
            </label>
          ))}
        </div>
        {autonomyError ? (
          <div className="mt-1 text-[11px]" style={{ color: C.warn }}>
            {autonomyError}
          </div>
        ) : null}
      </Card>

      <Card className="p-3">
        <CardTitle>{laneKey ? i18nT('pages.arcBoard.brief_for', { lane: laneLabel }) : i18nT('pages.arcBoard.brief')}</CardTitle>
        {laneKey ? (
          <>
            <div className="mb-1.5 flex items-center gap-2 text-[10px]" style={{ color: C.dim }}>
              <div className="h-1.5 flex-1 overflow-hidden rounded-full" style={{ backgroundColor: C.border }}>
                <div
                  className="h-full rounded-full"
                  style={{ width: `${share}%`, backgroundColor: share > 85 ? C.warn : C.accent }}
                />
              </div>
              <span className="tabular-nums">
                {brief.length}/{BRIEF_CAP}
              </span>
            </div>
            <div
              className="max-h-[34vh] overflow-y-auto whitespace-pre-wrap font-mono text-[11px] leading-5"
              style={{ color: C.dim }}
            >
              {brief}
            </div>
          </>
        ) : (
          <div className="text-[12px]" style={{ color: C.dim }}>
            {i18nT('pages.arcBoard.brief_pick_lane')}
          </div>
        )}
      </Card>

      <Card className="p-3">
        <CardTitle>{i18nT('pages.arcBoard.flags')}</CardTitle>
        {collisions.length === 0 && fails.length === 0 ? (
          <div className="text-[12px]" style={{ color: C.dim }}>
            {i18nT('pages.arcBoard.no_flags')}
          </div>
        ) : null}
        {collisions.map(group => {
          const claims = group.map(id => byId.get(id)).filter(Boolean)
          const first = claims[0]
          return (
            <div key={group.join()} className="mb-2 text-[12px]" style={{ color: C.text }}>
              <Badge variant="err" className="mr-1 px-1.5 text-[10px]">
                {i18nT('pages.arcBoard.collision')}
              </Badge>
              {i18nT('pages.arcBoard.collision_text', {
                lanes: claims.map(c => authorLabel(arc, c!.author)).join(', '),
                step: first?.step || '',
              })}
            </div>
          )
        })}
        {fails.map(f => (
          <div key={f.id} className="mb-2 text-[12px]" style={{ color: C.text }}>
            <Badge variant="err" className="mr-1 px-1.5 text-[10px]">
              FAIL
            </Badge>
            {authorLabel(arc, f.author)}: {f.text}
          </div>
        ))}
      </Card>
    </div>
  )
}
