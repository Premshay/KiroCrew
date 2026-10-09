/** Opt-in trace of transcript selection handling (`?debugSelection=1`).
 *
 * Touch selection handles cannot be reproduced headless, so this records what
 * the page saw on a real device: where each endpoint sat, whether the clamp
 * moved one, and what range was retained. Off unless the URL asks for it.
 */

export interface SelectionDebugEntry {
  t: string
  anchor: string
  focus: string
  clamped: string
  retained: string
}

const MAX_ENTRIES = 8

export function selectionDebugEnabled(): boolean {
  if (typeof window === 'undefined') return false
  try {
    return new URLSearchParams(window.location.search).get('debugSelection') === '1'
  } catch {
    return false
  }
}

/** Where `node` sits relative to the transcript, e.g. `row 42`, `in-scroller`, `outside`. */
export function describeEndpoint(container: HTMLElement, node: Node | null): string {
  if (!node) return 'none'
  if (!node.isConnected) return 'detached'
  const element = node instanceof Element ? node : node.parentElement
  const row = element?.closest<HTMLElement>('[data-display-index]')
  if (row && container.contains(row)) return `row ${row.dataset.displayIndex}`
  if (container.contains(node)) return 'in-scroller'
  const range = document.createRange()
  range.selectNodeContents(container)
  try {
    const p = range.comparePoint(node, 0)
    return p < 0 ? 'outside-before' : p > 0 ? 'outside-after' : 'outside'
  } catch {
    return 'outside'
  }
}

const entries: SelectionDebugEntry[] = []
const listeners = new Set<() => void>()

export function recordSelectionDebug(entry: Omit<SelectionDebugEntry, 't'>): void {
  const last = entries[entries.length - 1]
  if (last && last.anchor === entry.anchor && last.focus === entry.focus && last.clamped === entry.clamped && last.retained === entry.retained) return
  entries.push({ t: new Date().toISOString().slice(11, 23), ...entry })
  if (entries.length > MAX_ENTRIES) entries.shift()
  for (const listener of listeners) listener()
}

export function selectionDebugEntries(): readonly SelectionDebugEntry[] {
  return entries
}

export function subscribeSelectionDebug(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
