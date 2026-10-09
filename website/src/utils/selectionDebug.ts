/** Opt-in trace of transcript selection handling (`?debugSelection=1`).
 *
 * Touch selection handles cannot be reproduced headless, so this records what
 * the page saw on a real device: where each endpoint sat, its offset, and what
 * range was retained. Off unless the URL asks for it.
 */

export interface SelectionDebugEntry {
  t: string
  anchor: string
  focus: string
  offsets: string
  retained: string
  /** The selection's first and last words and its length, e.g. `"found the"…"selection." 412ch`. */
  words?: string
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

/** Where `node` sits relative to the transcript, e.g. `row 42`, `in-scroller`,
 * `outside-before(chat-header)`; outside endpoints name their nearest test id. */
export function describeEndpoint(container: HTMLElement, node: Node | null): string {
  if (!node) return 'none'
  if (!node.isConnected) return 'detached'
  const element = node instanceof Element ? node : node.parentElement
  const row = element?.closest<HTMLElement>('[data-display-index]')
  if (row && container.contains(row)) return `row ${row.dataset.displayIndex} ${node.nodeName}`
  if (container.contains(node)) return 'in-scroller'
  const range = document.createRange()
  range.selectNodeContents(container)
  try {
    const p = range.comparePoint(node, 0)
    const side = p < 0 ? 'outside-before' : p > 0 ? 'outside-after' : 'outside'
    const id = element?.closest<HTMLElement>('[data-testid]')?.dataset.testid
    return id ? `${side}(${id})` : side
  } catch {
    return 'outside'
  }
}

// Replaced, never mutated: `useSyncExternalStore` redraws only when the
// snapshot's identity changes, and an in-place push left the panel frozen
// until something else re-rendered the chat.
let entries: readonly SelectionDebugEntry[] = []
const listeners = new Set<() => void>()

/** Record one selection change. `first` marks the first change of a new
 * selection: it clears the history and stays pinned at the top, so a long drag
 * does not push out where the selection began. */
export function recordSelectionDebug(entry: Omit<SelectionDebugEntry, 't'>, first = false): void {
  const last = entries[entries.length - 1]
  if (!first && last && last.anchor === entry.anchor && last.focus === entry.focus && last.offsets === entry.offsets && last.retained === entry.retained && last.words === entry.words) return
  const next = [...(first ? [] : entries), { t: new Date().toISOString().slice(11, 23), ...entry }]
  if (next.length > MAX_ENTRIES) next.splice(1, 1)
  entries = next
  for (const listener of listeners) listener()
}

export function selectionDebugEntries(): readonly SelectionDebugEntry[] {
  return entries
}

export function subscribeSelectionDebug(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** The selected text's opening and closing words and its length. */
export function describeSelectedText(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (flat.length <= 40) return `"${flat}" ${text.length}ch`
  return `"${flat.slice(0, 18)}"…"${flat.slice(-18)}" ${text.length}ch`
}
