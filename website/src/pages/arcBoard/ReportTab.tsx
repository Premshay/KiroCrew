import type React from 'react'
/**
 * The Report tab: board-only pilot metrics for the arc (the same functions
 * `scripts/arc_pilot_report.py` uses). PR product guards need GitHub and stay
 * in that script; the tab says so rather than showing a guessed number.
 */

import { useQuery } from '@tanstack/react-query'
import { Card, CardTitle } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtRelative } from '../../i18n/format'
import { errMessage } from '../../utils/thunkError'
import { arcBoardReportKey, type ArcBoardArc, type ArcBoardMedianMax } from '../../api/client/arcBoard'
import { ENTRY_KINDS } from './arcBoardModel'
import { EntryLink, authorLabel } from './EntryRow'
import { C, KIND_COLOR } from './theme'

function mm(v: ArcBoardMedianMax): string {
  if (v.median === null) return '—'
  return i18nT('pages.arcBoard.median_max', { median: v.median, max: v.max ?? '—' })
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b py-1.5 text-[12px]" style={{ borderColor: C.border }}>
      <span style={{ color: C.dim }}>{label}</span>
      <span className="text-right tabular-nums" style={{ color: C.text }}>
        {value}
      </span>
    </div>
  )
}

export default function ReportTab({ arc }: { arc: ArcBoardArc }) {
  const report = useQuery({
    queryKey: arcBoardReportKey(arc.id),
    queryFn: () => api.arcBoardReport(arc.id),
    refetchInterval: 60_000,
  })
  if (report.isError) {
    return (
      <ErrorNotice
        title={i18nT('pages.arcBoard.load_failed')}
        message={errMessage(report.error) || ''}
        askAgent
        variant="inline"
      />
    )
  }
  const r = report.data
  if (!r) {
    return (
      <div className="text-[13px]" style={{ color: C.dim }}>
        {i18nT('pages.arcBoard.loading')}
      </div>
    )
  }
  const lanes = Object.entries(r.by_lane)
  const maxLane = Math.max(1, ...lanes.map(([, kinds]) => Object.values(kinds).reduce((a, b) => a + b, 0)))
  return (
    <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
      <Card className="p-3">
        <CardTitle>{i18nT('pages.arcBoard.report_coordination')}</CardTitle>
        <Row label={i18nT('pages.arcBoard.report_entries')} value={r.entries} />
        <Row label={i18nT('pages.arcBoard.report_since')} value={r.since ? fmtRelative(r.since) : '—'} />
        <Row label={i18nT('pages.arcBoard.report_reply')} value={mm(r.ask_age.time_to_reply_min)} />
        <Row label={i18nT('pages.arcBoard.report_open_asks')} value={r.ask_age.open_asks} />
        <Row
          label={i18nT('pages.arcBoard.report_oldest_ask')}
          value={r.ask_age.oldest_open_ask_min === null ? '—' : i18nT('pages.arcBoard.minutes', { n: r.ask_age.oldest_open_ask_min })}
        />
        <Row label={i18nT('pages.arcBoard.report_idle')} value={mm(r.idle_gaps.overall)} />
        <Row label={i18nT('pages.arcBoard.report_collisions')} value={r.collisions} />
        <Row label={i18nT('pages.arcBoard.report_repeated_fails')} value={r.repeated_fails} />
        <Row label={i18nT('pages.arcBoard.report_reading')} value={r.reading_load_words} />
      </Card>

      <Card className="p-3">
        <CardTitle>{i18nT('pages.arcBoard.report_by_lane')}</CardTitle>
        {lanes.length === 0 ? (
          <div className="text-[12px]" style={{ color: C.dim }}>
            {i18nT('pages.arcBoard.board_empty')}
          </div>
        ) : null}
        {lanes.map(([lane, kinds]) => {
          const idle = r.idle_gaps.per_member[lane]
          const open = r.idle_gaps.open_gaps_min[lane]
          return (
            <div key={lane} className="mb-2">
              <div className="flex items-baseline justify-between text-[12px]">
                <span style={{ color: C.text }}>{authorLabel(arc, lane)}</span>
                <span className="text-[10px]" style={{ color: C.dim }}>
                  {idle ? i18nT('pages.arcBoard.report_lane_idle', { v: mm(idle) }) : ''}
                  {open !== undefined ? ` · ${i18nT('pages.arcBoard.report_lane_open', { n: open })}` : ''}
                </span>
              </div>
              <div className="mt-1 flex h-2 overflow-hidden rounded-full" style={{ backgroundColor: C.border }}>
                {ENTRY_KINDS.map(k =>
                  kinds[k] ? (
                    <div
                      key={k}
                      title={`${k}: ${kinds[k]}`}
                      style={{ width: `${(kinds[k] / maxLane) * 100}%`, backgroundColor: KIND_COLOR[k] }}
                    />
                  ) : null,
                )}
              </div>
            </div>
          )
        })}
        <div className="mt-2 flex flex-wrap gap-2 text-[10px]" style={{ color: C.dim }}>
          {ENTRY_KINDS.map(k => (
            <span key={k} className="inline-flex items-center gap-1 font-mono">
              <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: KIND_COLOR[k] }} />
              {k} {r.by_kind[k] ?? 0}
            </span>
          ))}
        </div>
      </Card>

      <Card className="p-3 xl:col-span-2">
        <CardTitle>{i18nT('pages.arcBoard.report_outcomes')}</CardTitle>
        {r.done_links.length === 0 ? (
          <div className="text-[12px]" style={{ color: C.dim }}>
            {i18nT('pages.arcBoard.report_no_outcomes')}
          </div>
        ) : (
          r.done_links.map((l, i) => <EntryLink key={i} link={l} />)
        )}
        <div className="mt-3 text-[11px]" style={{ color: C.dim }}>
          {i18nT('pages.arcBoard.report_guards_note')}
        </div>
      </Card>
    </div>
  )
}
