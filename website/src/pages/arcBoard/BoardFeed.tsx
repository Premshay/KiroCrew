/**
 * The board feed: newest first, filtered by entry kind and (optionally) the
 * selected lane, with a history scrubber that replays the arc from its first
 * entry. LIVE follows every new entry; dragging the scrubber pins the view at
 * that point until LIVE is pressed again. The parent keys this component by
 * arc id, so a cursor never carries over to a different arc.
 */

import { useState } from 'react'
import { Btn, Card, CardTitle } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import type { ArcBoardArc, ArcBoardEntry } from '../../api/client/arcBoard'
import { ENTRY_KINDS, feedView } from './arcBoardModel'
import { EntryRow } from './EntryRow'
import { C, KIND_COLOR } from './theme'

export default function BoardFeed({
  arc,
  entries,
  lane,
  laneLabel,
  collisionIds,
}: {
  arc: ArcBoardArc
  entries: ArcBoardEntry[]
  /** The selected lane's key; the feed narrows to it only when the owner asks. */
  lane: string | null
  laneLabel: string
  collisionIds: ReadonlySet<string>
}) {
  const [kinds, setKinds] = useState<Set<string>>(() => new Set(ENTRY_KINDS))
  const [onlyLane, setOnlyLane] = useState(false)
  const [cursor, setCursor] = useState<number | null>(null)
  const total = entries.length
  const shown = feedView(entries, { kinds, lane: onlyLane ? lane : null, cursor })
  const at = cursor === null ? total : Math.min(cursor, total)

  const toggle = (k: string) =>
    setKinds(prev => {
      const next = new Set(prev)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })

  return (
    <Card className="p-0">
      <div className="flex flex-wrap items-center gap-1.5 px-3 pt-3">
        <CardTitle className="mb-0 mr-2">
          {i18nT('pages.arcBoard.board')}
          <span className="ml-1 font-normal" style={{ color: C.dim }}>
            {i18nT('pages.arcBoard.entries_n', { n: total })}
          </span>
        </CardTitle>
        {ENTRY_KINDS.map(k => (
          <button
            key={k}
            type="button"
            aria-pressed={kinds.has(k)}
            onClick={() => toggle(k)}
            className="rounded-full border px-2 py-[1px] font-mono text-[10px]"
            style={
              kinds.has(k)
                ? { borderColor: KIND_COLOR[k], color: KIND_COLOR[k] }
                : { borderColor: C.border, color: C.dim, opacity: 0.6 }
            }
          >
            {k}
          </button>
        ))}
        {lane ? (
          <button
            type="button"
            aria-pressed={onlyLane}
            onClick={() => setOnlyLane(v => !v)}
            className="rounded-full border px-2 py-[1px] text-[11px]"
            style={onlyLane ? { borderColor: C.accent, color: C.accent } : { borderColor: C.border, color: C.dim }}
          >
            {i18nT('pages.arcBoard.only_lane', { lane: laneLabel })}
          </button>
        ) : null}
      </div>

      {total === 0 ? (
        <div className="px-3 py-3 text-[12px]" style={{ color: C.dim }}>
          {i18nT('pages.arcBoard.board_empty')}
        </div>
      ) : (
        <>
          <div className="mt-2 max-h-[48vh] overflow-y-auto border-t" style={{ borderColor: C.border }}>
            {shown.length === 0 ? (
              <div className="px-3 py-3 text-[12px]" style={{ color: C.dim }}>
                {i18nT('pages.arcBoard.feed_filtered_empty')}
              </div>
            ) : (
              shown.map(e => <EntryRow key={e.id} arc={arc} entry={e} highlight={collisionIds.has(e.id)} />)
            )}
          </div>
          <div className="flex items-center gap-2 px-3 pb-3 pt-2">
            <span className="shrink-0 text-[11px]" style={{ color: C.dim }}>
              {i18nT('pages.arcBoard.history')}
            </span>
            <input
              type="range"
              min={0}
              max={total}
              value={at}
              onChange={e => {
                const v = Number(e.target.value)
                setCursor(v >= total ? null : v)
              }}
              className="w-full"
              aria-label={i18nT('pages.arcBoard.history')}
            />
            <span className="shrink-0 text-[11px] tabular-nums" style={{ color: C.dim }}>
              {at}/{total}
            </span>
            <Btn
              type="button"
              primary={cursor === null}
              onClick={() => setCursor(null)}
              className="shrink-0 px-2 py-0.5 text-[10px]"
            >
              {i18nT('pages.arcBoard.live')}
            </Btn>
          </div>
        </>
      )}
    </Card>
  )
}
