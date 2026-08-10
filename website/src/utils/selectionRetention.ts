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

type TranscriptEdge = 'start' | 'end'

function transcriptEdgeFor(
  container: HTMLElement,
  node: Node | null,
  offset: number,
): TranscriptEdge | null {
  if (!node) return null
  const range = document.createRange()
  range.selectNodeContents(container)
  try {
    const position = range.comparePoint(node, offset)
    return position < 0 ? 'start' : position > 0 ? 'end' : null
  } catch {
    return null
  }
}

/** Keep a transcript-originated selection out of sibling dashboard chrome.
 *
 * Mobile browsers may move a native selection handle from the scroller onto an
 * absolute title bar or composer. Neither belongs to copied transcript content,
 * so the endpoint is clamped to a supplied transcript edge instead.
 */
export function clampSelectionToTranscript(
  container: HTMLElement,
  selection: Selection,
  startBoundary: Node | null,
  endBoundary: Node | null,
  itemCount: number,
): RetainedVirtualRange | null {
  if (selection.isCollapsed || !selectionTouchesContainer(container, selection)) return null
  const anchor = rowIndexFor(container, selection.anchorNode)
  const focus = rowIndexFor(container, selection.focusNode)
  if (anchor !== null && focus !== null) {
    return { start: Math.min(anchor, focus), end: Math.max(anchor, focus) + 1 }
  }

  const insideIndex = anchor ?? focus
  if (insideIndex === null) return null
  const outsideNode = anchor === null ? selection.anchorNode : selection.focusNode
  const outsideOffset = anchor === null ? selection.anchorOffset : selection.focusOffset
  const edge = transcriptEdgeFor(container, outsideNode, outsideOffset)
  const boundary = edge === 'start' ? startBoundary : endBoundary
  if (!edge || !boundary || !selection.anchorNode || !selection.focusNode) return null

  if (anchor === null) {
    selection.setBaseAndExtent(boundary, 0, selection.focusNode, selection.focusOffset)
  } else {
    selection.setBaseAndExtent(selection.anchorNode, selection.anchorOffset, boundary, 0)
  }
  return edge === 'start'
    ? { start: 0, end: insideIndex + 1 }
    : { start: insideIndex, end: itemCount }
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
