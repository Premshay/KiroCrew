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

/** Where both endpoints of a selection last sat on transcript rows. */
export interface SelectionEndpoints {
  anchorNode: Node
  anchorOffset: number
  focusNode: Node
  focusOffset: number
}

/** Snapshot the endpoints when both sit on rows, else null. */
export function rowEndpoints(container: HTMLElement, selection: Selection): SelectionEndpoints | null {
  if (selection.isCollapsed || !selectedRowRange(container, selection)) return null
  const { anchorNode, anchorOffset, focusNode, focusOffset } = selection
  return anchorNode && focusNode ? { anchorNode, anchorOffset, focusNode, focusOffset } : null
}

/** Put an endpoint that left the transcript back where it last sat on a row.
 *
 * A touch handle dragged over the title or the composer can land in their
 * text, and the selection then takes everything in between. The handle goes
 * back to its own last transcript position: not a row's start or the
 * transcript's edge, which grew the selection to everything above. Only a
 * selection that has already sat on rows (`last`) is touched, so a fresh
 * long-press is never rewritten. Returns whether the selection was changed.
 */
export function restoreEndpointToTranscript(
  container: HTMLElement,
  selection: Selection,
  last: SelectionEndpoints | null,
): boolean {
  if (!last || selection.isCollapsed) return false
  const { anchorNode, anchorOffset, focusNode, focusOffset } = selection
  if (!anchorNode || !focusNode) return false
  const anchorIn = container.contains(anchorNode)
  const focusIn = container.contains(focusNode)
  if (anchorIn === focusIn) return false
  if (!anchorIn) {
    if (!last.anchorNode.isConnected) return false
    selection.setBaseAndExtent(last.anchorNode, last.anchorOffset, focusNode, focusOffset)
  } else {
    if (!last.focusNode.isConnected) return false
    selection.setBaseAndExtent(anchorNode, anchorOffset, last.focusNode, last.focusOffset)
  }
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
