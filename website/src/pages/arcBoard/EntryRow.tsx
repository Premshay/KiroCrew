import { Badge } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { fmtRelative } from '../../i18n/format'
import type { ArcBoardArc, ArcBoardEntry } from '../../api/client/arcBoard'
import { splitLink } from './arcBoardModel'
import ClampText from './ClampText'
import { C, kindVariant } from './theme'

/** The owner is an author like any lane; the others are named by their role. */
export function authorLabel(arc: ArcBoardArc, author: string): string {
  if (author === 'owner') return i18nT('pages.arcBoard.owner')
  if (author === 'github') return i18nT('pages.arcBoard.github')
  return arc.members[author]?.role ?? author
}

/** An entry's link: only a leading http(s) URL is clickable; the rest is text. */
export function EntryLink({ link }: { link: string }) {
  const { href, text } = splitLink(link)
  return (
    <div className="mt-1 break-words text-[11px]" style={{ color: C.dim }}>
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noreferrer noopener"
          className="underline-offset-2 hover:underline"
          style={{ color: C.accent }}
        >
          {href}
        </a>
      ) : null}
      {/* Free evidence text can run long (a lane's options for the owner):
          two lines, a click shows the rest. */}
      {text ? <ClampText lines={2} text={(href ? '— ' : '') + text} /> : null}
    </div>
  )
}

export function EntryRow({
  arc,
  entry,
  highlight,
}: {
  arc: ArcBoardArc
  entry: ArcBoardEntry
  highlight?: boolean
}) {
  return (
    <div
      className="border-b px-3 py-2"
      style={{ borderColor: C.border, backgroundColor: highlight ? C.cardHl : undefined }}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <Badge variant={kindVariant(entry.kind)} className="px-1.5 text-[10px]">
          {entry.kind}
        </Badge>
        <span className="text-[12px] font-medium" style={{ color: C.text }}>
          {authorLabel(arc, entry.author)}
        </span>
        {entry.to ? (
          <span className="text-[11px]" style={{ color: C.dim }}>
            {i18nT('pages.arcBoard.to', { to: authorLabel(arc, entry.to) })}
          </span>
        ) : null}
        {highlight ? (
          <Badge variant="err" className="px-1.5 text-[10px]">
            {i18nT('pages.arcBoard.collision')}
          </Badge>
        ) : null}
        <span className="ml-auto shrink-0 text-[11px]" style={{ color: C.dim }}>
          {fmtRelative(entry.ts)}
        </span>
      </div>
      {entry.text ? (
        <div className="mt-1 whitespace-pre-wrap break-words text-[12px] leading-5" style={{ color: C.text }}>
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
          {i18nT('pages.arcBoard.checked')}: {entry.checked.join(' · ')}
        </div>
      ) : null}
      {entry.link ? <EntryLink link={entry.link} /> : null}
      {entry.author === 'github' ? (
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px]" style={{ color: C.dim }}>
          {entry.lane ? <span>{authorLabel(arc, entry.lane)}</span> : null}
          {entry.head ? <span className="font-mono">{entry.head.slice(0, 10)}</span> : null}
          {entry.verified ? (
            <Badge variant="ok" className="px-1.5 text-[10px]">
              {i18nT('pages.arcBoard.verified_by', { who: (entry.reviewers ?? []).join(', ') })}
            </Badge>
          ) : (
            <Badge variant="warn" className="px-1.5 text-[10px]">
              {i18nT('pages.arcBoard.not_verified')}
            </Badge>
          )}
        </div>
      ) : null}
      {entry.signoff ? (
        <Badge variant="ok" className="mt-1 px-1.5 text-[10px]">
          {i18nT('pages.arcBoard.signed_off')}
        </Badge>
      ) : null}
    </div>
  )
}
