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

/** Pull a dragged endpoint off the title or composer onto the nearest visible row.
 *
 * Mobile browsers can move a touch handle out of the scroller onto the chat
 * chrome, and the selection then takes the title or the composer with it. Only
 * a selection already ESTABLISHED on rows is touched: a fresh Android
 * long-press can report an off-row endpoint while it is still being built, and
 * rewriting it there selected the whole chat or cancelled the press. The
 * endpoint goes to the first or last row visible in the scroller, never the
 * transcript's edge, which grew the selection to everything above it.
 * Returns whether the selection was changed.
 */
export function pullEndpointFromChrome(
  container: HTMLElement,
  selection: Selection,
  established: boolean,
): boolean {
  if (!established || selection.isCollapsed) return false
  const { anchorNode, anchorOffset, focusNode, focusOffset } = selection
  const anchorRow = rowIndexFor(container, anchorNode)
  const focusRow = rowIndexFor(container, focusNode)
  if ((anchorRow === null) === (focusRow === null)) return false
  const outside = anchorRow === null ? anchorNode : focusNode
  if (!outside || container.contains(outside)) return false
  const outsideEl = outside instanceof Element ? outside : outside.parentElement
  if (!outsideEl) return false
  const box = container.getBoundingClientRect()
  const at = outsideEl.getBoundingClientRect()
  const above = at.top + at.height / 2 < box.top + box.height / 2
  const rows = Array.from(container.querySelectorAll<HTMLElement>('[data-display-index]'))
  const visible = rows.filter((row) => {
    const r = row.getBoundingClientRect()
    return r.bottom > box.top && r.top < box.bottom
  })
  const row = above ? visible[0] : visible[visible.length - 1]
  if (!row) return false
  const [node, offset] = above ? [row, 0] : [row, row.childNodes.length]
  if (anchorRow === null) selection.setBaseAndExtent(node, offset, focusNode!, focusOffset)
  else selection.setBaseAndExtent(anchorNode!, anchorOffset, node, offset)
  return true
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

/** What the transcript's retained range should do after a selection change.
 *
 * `null` releases the retention; `'keep'` leaves the last retained span in
 * place; a range replaces it. An endpoint that is inside the transcript but on
 * no row (the scroller's padding under the composer, a spacer, a sentinel) is
 * a transient handle position, not the end of the selection: releasing there
 * let the virtualizer unmount the row holding the selection's start, and the
 * browser then re-rooted the selection at the top of the transcript.
 *
 * Read-only: the selection is never rewritten. Snapping an off-row endpoint to
 * a transcript edge stretched a fresh Android long-press to the whole chat, or
 * cancelled it outright. The overlays are inert while a selection is held
 * (`useSelectionInertOverlays`), so a handle no longer lands in the chrome.
 */
export function nextRetainedRange(
  container: HTMLElement,
  selection: Selection | null,
): RetainedVirtualRange | 'keep' | null {
  if (!selection || selection.isCollapsed) return null
  if (!selectionTouchesContainer(container, selection)) return null
  return selectedRowRange(container, selection) ?? 'keep'
}
