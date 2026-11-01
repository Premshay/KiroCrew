/**
 * The live stage: lanes sit around the board, the owner below it. A working
 * lane pulses; each entry that arrived since the last poll flies from its
 * author to the board in its kind's colour, and an ASK addressed to the owner
 * flies on to the owner node. Colliding claims are joined by a dashed line.
 *
 * Motion carries information here (who just wrote what), so it is on by
 * default and off under prefers-reduced-motion; nothing on the stage depends
 * on seeing an animation, since the feed lists every entry.
 */

import { useEffect, useRef, useState } from 'react'
import type React from 'react'
import { i18nT } from '../../i18n/t'
import type { ArcBoardEntry } from '../../api/client/arcBoard'
import type { LaneView } from './arcBoardModel'
import { C, KIND_COLOR, prefersReducedMotion } from './theme'

const W = 640
const H = 330
const BOARD = { x: W / 2, y: 150 }
const OWNER = { x: W / 2, y: 296 }
const FLIGHT_MS = 1400

type Point = { x: number; y: number }

/** Lanes on an upper arc around the board, left to right in join order. */
export function lanePositions(n: number): Point[] {
  if (n === 0) return []
  const rx = 270
  const ry = 120
  return Array.from({ length: n }, (_, i) => {
    const t = n === 1 ? 0.5 : i / (n - 1)
    const angle = Math.PI * (1 + t) // 180° → 360°: the upper half
    return { x: BOARD.x + rx * Math.cos(angle), y: BOARD.y + 10 + ry * Math.sin(angle) * 0.95 }
  })
}

interface Flight {
  id: string
  from: Point
  to: Point
  color: string
}

export default function ArcStage({
  lanes,
  arrivals,
  ownerAsks,
  collisions,
  selected,
  onSelect,
  onClear,
  onOwner,
}: {
  lanes: LaneView[]
  /** Entries that arrived since the previous poll. */
  arrivals: ArcBoardEntry[]
  ownerAsks: number
  /** Pairs of lane keys whose open claims collide. */
  collisions: [string, string][]
  selected: string | null
  onSelect: (key: string) => void
  /** A click on empty stage clears the lane selection. */
  onClear: () => void
  onOwner: () => void
}) {
  const pos = lanePositions(lanes.length)
  const reduced = prefersReducedMotion()
  const at = new Map(lanes.map((l, i) => [l.key, pos[i]]))
  const [flights, setFlights] = useState<Flight[]>([])
  const timers = useRef<number[]>([])

  useEffect(() => {
    if (arrivals.length === 0 || prefersReducedMotion()) return
    const next: Flight[] = []
    for (const e of arrivals) {
      const color = KIND_COLOR[e.kind] ?? C.dim
      const from = e.author === 'owner' ? OWNER : at.get(e.author)
      if (from) next.push({ id: e.id, from, to: BOARD, color })
      const target = e.kind === 'ASK' ? (e.to === 'owner' ? OWNER : at.get(e.to ?? '')) : undefined
      if (target) next.push({ id: e.id + ':out', from: BOARD, to: target, color })
    }
    if (next.length === 0) return
    setFlights(f => [...f, ...next])
    const ids = new Set(next.map(f => f.id))
    timers.current.push(window.setTimeout(() => setFlights(f => f.filter(x => !ids.has(x.id))), FLIGHT_MS * 2))
    // `at` is derived from `lanes`; arrivals is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [arrivals])

  useEffect(() => () => timers.current.forEach(t => window.clearTimeout(t)), [])

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="h-auto w-full"
      role="img"
      aria-label={i18nT('pages.arcBoard.stage_label')}
    >
      <style>{`
        @keyframes arcPulse { 0%,100% { opacity: .9 } 50% { opacity: .25 } }
        @keyframes arcFly {
          0% { transform: translate(var(--fx), var(--fy)); opacity: 0 }
          15% { opacity: 1 }
          85% { opacity: 1 }
          100% { transform: translate(var(--tx), var(--ty)); opacity: 0 }
        }

      `}</style>

      {/* Empty stage: a click here clears the lane selection. */}
      <rect x={0} y={0} width={W} height={H} fill="transparent" onClick={onClear} />

      {/* Spokes: each lane to the board. */}
      {lanes.map((l, i) => (
        <line
          key={'s' + l.key}
          x1={pos[i].x}
          y1={pos[i].y}
          x2={BOARD.x}
          y2={BOARD.y}
          stroke={C.border}
          strokeWidth={1}
        />
      ))}
      <line x1={OWNER.x} y1={OWNER.y} x2={BOARD.x} y2={BOARD.y} stroke={C.border} strokeWidth={1} />

      {/* Collisions: two lanes on one step. */}
      {collisions.map(([a, b]) => {
        const pa = at.get(a)
        const pb = at.get(b)
        if (!pa || !pb) return null
        return (
          <line
            key={'c' + a + b}
            x1={pa.x}
            y1={pa.y}
            x2={pb.x}
            y2={pb.y}
            stroke={C.danger}
            strokeWidth={2}
            strokeDasharray="6 4"
          />
        )
      })}

      {/* The board. */}
      <rect
        x={BOARD.x - 70}
        y={BOARD.y - 26}
        width={140}
        height={52}
        rx={10}
        fill={C.bg}
        stroke={C.accent}
        strokeWidth={1.5}
      />
      <text x={BOARD.x} y={BOARD.y + 5} textAnchor="middle" fontSize={14} fontWeight={600} fill={C.text}>
        {i18nT('pages.arcBoard.board')}
      </text>

      {/* Flights. */}
      {flights.map(f => (
        <circle
          key={f.id}
          r={6}
          fill={f.color}
          style={
            {
              opacity: 0,
              animationName: 'arcFly',
              animationTimingFunction: 'ease-in-out',
              animationFillMode: 'both',
              '--fx': `${f.from.x}px`,
              '--fy': `${f.from.y}px`,
              '--tx': `${f.to.x}px`,
              '--ty': `${f.to.y}px`,
              animationDuration: `${FLIGHT_MS}ms`,
              animationDelay: f.id.endsWith(':out') ? `${FLIGHT_MS * 0.6}ms` : '0ms',
            } as React.CSSProperties
          }
        />
      ))}

      {/* Lanes. */}
      {lanes.map((l, i) => {
        const p = pos[i]
        const ring = l.state === 'working' ? C.ok : l.state === 'gone' ? C.danger : C.dim
        const isSel = selected === l.key
        return (
          <g
            key={l.key}
            role="button"
            tabIndex={0}
            aria-label={l.role}
            onClick={() => onSelect(l.key)}
            onKeyDown={e => {
              if (e.key === 'Enter' || e.key === ' ') onSelect(l.key)
            }}
            style={{ cursor: 'pointer' }}
          >
            {l.state === 'working' ? (
              <circle
                cx={p.x}
                cy={p.y}
                r={27}
                fill="none"
                stroke={C.ok}
                strokeWidth={3}
                style={reduced ? undefined : { animation: 'arcPulse 1.6s ease-in-out infinite' }}
              />
            ) : null}
            <circle
              cx={p.x}
              cy={p.y}
              r={21}
              fill={isSel ? C.cardHl : C.bg}
              stroke={isSel ? C.accent : ring}
              strokeWidth={isSel ? 3 : 2}
            />
            <text x={p.x} y={p.y + 5} textAnchor="middle" fontSize={13} fontWeight={600} fill={C.text}>
              {l.role.slice(0, 2)}
            </text>
            <text x={p.x} y={p.y + 40} textAnchor="middle" fontSize={11} fill={C.dim}>
              {l.role.length > 16 ? l.role.slice(0, 15) + '…' : l.role}
            </text>
            {l.nudge ? <circle cx={p.x + 16} cy={p.y - 16} r={4} fill={C.aim} /> : null}
          </g>
        )
      })}

      {/* The owner. */}
      <g role="button" tabIndex={0} aria-label={i18nT('pages.arcBoard.owner')} onClick={onOwner}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') onOwner() }} style={{ cursor: 'pointer' }}>
        <circle cx={OWNER.x} cy={OWNER.y} r={20} fill={C.bg} stroke={C.accent} strokeWidth={2} />
        <text x={OWNER.x} y={OWNER.y + 5} textAnchor="middle" fontSize={12} fontWeight={600} fill={C.text}>
          {i18nT('pages.arcBoard.you')}
        </text>
        {ownerAsks > 0 ? (
          <>
            <circle cx={OWNER.x + 18} cy={OWNER.y - 16} r={10} fill={C.warn} />
            <text x={OWNER.x + 18} y={OWNER.y - 12} textAnchor="middle" fontSize={11} fontWeight={700} fill={C.bg}>
              {ownerAsks}
            </text>
          </>
        ) : null}
      </g>
    </svg>
  )
}
