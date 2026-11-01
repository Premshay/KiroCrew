/**
 * Text cut to one line (or `lines` lines) with an ellipsis. When the text is
 * actually cut, a click shows it in full and a second click cuts it again;
 * text that fits is inert, so it never steals a click from its row.
 */

import { useLayoutEffect, useRef, useState } from 'react'
import type React from 'react'
import { i18nT } from '../../i18n/t'

export default function ClampText({
  text,
  lines = 1,
  className = '',
  style,
}: {
  text: string
  lines?: 1 | 2
  className?: string
  style?: React.CSSProperties
}) {
  const ref = useRef<HTMLElement>(null)
  const [open, setOpen] = useState(false)
  const [cut, setCut] = useState(false)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el || open) return
    setCut(el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1)
  }, [text, open, lines])

  const expandable = cut || open
  const clampCls = open ? 'whitespace-pre-wrap break-words' : lines === 1 ? 'truncate' : 'line-clamp-2'
  const toggle = (e: React.SyntheticEvent) => {
    if (!expandable) return
    e.stopPropagation()
    e.preventDefault()
    setOpen(o => !o)
  }

  const cls = `block min-w-0 ${clampCls} ${className}`
  if (!expandable) {
    return (
      <span ref={ref} className={cls} style={style}>
        {text}
      </span>
    )
  }
  return (
    <button
      ref={ref as React.RefObject<HTMLButtonElement>}
      type="button"
      className={`${cls} w-full cursor-pointer text-left`}
      style={style}
      title={open ? i18nT('pages.arcBoard.show_less') : i18nT('pages.arcBoard.show_more')}
      aria-expanded={open}
      onClick={toggle}
    >
      {text}
    </button>
  )
}
