import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'

/** Height the Apps list keeps before the secondary tiles give up their pins:
 *  four ~60px tiles (56px tile + 4px gap). Below it the app tiles -- the rail's
 *  most-used rows on a session screen -- become a two-row peephole. */
export const APPS_FLOOR_PX = 240

/** Whether the secondary tiles fold into the Apps scroller.
 *
 *  Pure, so the rule is testable without layout: they stay pinned while the
 *  rail still leaves the Apps list `APPS_FLOOR_PX` beside every pinned block,
 *  and fold once it would not. A zero rail height means "not measured yet"
 *  (jsdom, first paint) and keeps the pinned layout, which is the shipped one. */
export function shouldFoldSecondary(
  railPx: number, topPx: number, secondaryPx: number, bottomPx: number,
): boolean {
  if (railPx <= 0) return false
  return railPx - topPx - secondaryPx - bottomPx < APPS_FLOOR_PX
}

/**
 * The phone rail's body, split into four regions so each scrolls one way:
 *
 * - `top` (brand mark, Main rows) and `bottom` (Settings, Search) are always pinned.
 * - `apps` scrolls in its own frame.
 * - `secondary` (Developer, Terminal, Customize, Kiro Account) is pinned above
 *   `bottom` while there is room, and otherwise rides the Apps scroller after the
 *   apps, behind a divider. Placement follows the rail's measured height, so a
 *   tall screen keeps the pinned layout and a short phone gets room for its apps,
 *   with no nested scroll and no control to learn.
 *
 * The decision cannot feed back on itself: the blocks keep their heights wherever
 * the secondary tiles sit, and the rail's height comes from the viewport.
 */
export function AdaptiveMobileRail({ top, apps, secondary, bottom, className, ...navProps }: {
  top: ReactNode
  apps: ReactNode
  secondary: ReactNode
  bottom: ReactNode
  className: string
} & Omit<React.ComponentProps<'nav'>, 'children' | 'className'>) {
  const navRef = useRef<HTMLElement>(null)
  const topRef = useRef<HTMLDivElement>(null)
  const secondaryRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const [fold, setFold] = useState(false)

  useLayoutEffect(() => {
    const nav = navRef.current
    if (!nav) return
    const measure = () => {
      const h = (el: HTMLElement | null) => el?.getBoundingClientRect().height ?? 0
      setFold(shouldFoldSecondary(nav.clientHeight, h(topRef.current), h(secondaryRef.current), h(bottomRef.current)))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(nav)
    for (const el of [topRef.current, secondaryRef.current, bottomRef.current]) if (el) ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const block = 'w-full flex flex-col items-center gap-1 shrink-0'
  const secondaryBlock = (
    <div ref={secondaryRef} data-testid="mobile-nav-rail-secondary" className={block}>
      {secondary}
    </div>
  )
  return (
    <nav ref={navRef} className={className} data-secondary-folded={fold ? 'true' : 'false'} {...navProps}>
      <div ref={topRef} className={block}>{top}</div>
      {/* Apps list: scrolls in its OWN frame. It keeps a two-tile floor so a
          screen too short even for the folded layout still shows apps; the rail
          itself scrolls as the last resort, so every tile stays reachable. */}
      <div
        data-testid="mobile-nav-rail-apps"
        className="flex-1 min-h-[7.5rem] w-full flex flex-col items-center gap-1 overflow-y-auto overflow-x-hidden overscroll-y-none scrollbar-none"
        style={{ scrollbarWidth: 'none' }}
      >
        {apps}
        {fold && (
          <>
            <div role="separator" aria-hidden="true" className="w-10 shrink-0 border-t border-border my-1.5" />
            {secondaryBlock}
          </>
        )}
      </div>
      {!fold && secondaryBlock}
      <div ref={bottomRef} className={block}>{bottom}</div>
    </nav>
  )
}
