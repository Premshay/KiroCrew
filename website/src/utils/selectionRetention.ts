/** Selection helpers for virtualized transcript rows. */

import type { RetainedVirtualRange } from '../hooks/virtualizer/types'

function rowIndexFor(container: HTMLElement, node: Node | null): number | null {
  const element = node instanceof Element ? node : node?.parentElement
  const row = element?.closest<HTMLElement>('[data-display-index]')
  if (!row || !container.contains(row)) return null
  const index = Number(row.dataset.displayIndex)
  return Number.isInteger(index) && index >= 0 ? index : null
}

/** True when either native selection endpoint still belongs to `container`. */
export function selectionTouchesContainer(container: HTMLElement, selection: Selection): boolean {
  return container.contains(selection.anchorNode) || container.contains(selection.focusNode)
}

/** Return the exclusive row span containing both selection endpoints.
 *
 * A range is intentionally returned only when both endpoints are transcript
 * rows. A transient WebKit endpoint outside the scroller leaves the last safe
 * retained span in place instead of replacing it with a document-wide range.
 */
export function selectedRowRange(
  container: HTMLElement,
  selection: Selection,
): RetainedVirtualRange | null {
  if (selection.isCollapsed) return null
  const anchor = rowIndexFor(container, selection.anchorNode)
  const focus = rowIndexFor(container, selection.focusNode)
  if (anchor === null || focus === null) return null
  return { start: Math.min(anchor, focus), end: Math.max(anchor, focus) + 1 }
}
