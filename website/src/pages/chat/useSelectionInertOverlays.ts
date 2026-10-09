import { useEffect, type RefObject } from 'react'
import { useIsTouchDevice } from '../../hooks/useIsTouchDevice'

/** Marks an element that overlays the transcript (header, composer dock). */
export const SELECTION_INERT_ATTR = 'data-selection-inert'

/** Whether a non-empty text selection is anchored inside `scroller`. */
export function transcriptSelectionHeld(scroller: HTMLElement | null): boolean {
  const sel = typeof document !== 'undefined' ? document.getSelection() : null
  if (!scroller || !sel || sel.isCollapsed || sel.rangeCount === 0) return false
  const anchor = sel.anchorNode
  return !!anchor && scroller.contains(anchor)
}

/**
 * While a touch selection is held in the transcript, make the overlays `inert`.
 *
 * The transcript scrolls UNDER the header and the composer dock, so extending a
 * selection to the bottom edge puts the handle over the dock. Hit-testing there
 * resolved into the composer's draft mirror, and the selection jumped to it,
 * taking everything in between. `user-select: none` does not change where the
 * caret lands; `inert` removes the overlay from hit-testing, so the handle lands
 * on the transcript beneath and the scroller's own edge auto-scroll takes over.
 *
 * Touch only: on desktop a selection outlives the drag, and an inert composer
 * would cost an extra click before typing.
 */
export function useSelectionInertOverlays(scrollerRef: RefObject<HTMLElement | null>): void {
  const isTouch = useIsTouchDevice()
  useEffect(() => {
    if (!isTouch) return
    const overlays = () => document.querySelectorAll<HTMLElement>(`[${SELECTION_INERT_ATTR}]`)
    const sync = () => {
      const held = transcriptSelectionHeld(scrollerRef.current)
      for (const el of overlays()) if (el.inert !== held) el.inert = held
    }
    document.addEventListener('selectionchange', sync)
    return () => {
      document.removeEventListener('selectionchange', sync)
      for (const el of overlays()) el.inert = false
    }
  }, [isTouch, scrollerRef])
}
