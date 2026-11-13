import { containedSelectionRange } from './selectionContainment'

/** Resolve only the SVG viewer's own open shadow root, never a sibling drawing. */
export function svgSelectionRange(sel: Selection, container: Element): Range | null {
  const root = container.firstElementChild?.shadowRoot
  const svg = root?.querySelector('svg')
  if (!root || !svg) return null
  let range: Range | null = null
  if (typeof sel.getComposedRanges === 'function') {
    try {
      const [composed] = sel.getComposedRanges({ shadowRoots: [root] })
      if (composed && svg.contains(composed.startContainer) && svg.contains(composed.endContainer)) {
        range = document.createRange()
        range.setStart(composed.startContainer, composed.startOffset)
        range.setEnd(composed.endContainer, composed.endOffset)
      }
    } catch {
      // Older implementations may reject the options; native shadow ranges still work.
    }
  }
  if (!range && !sel.isCollapsed && sel.rangeCount) range = containedSelectionRange(sel.getRangeAt(0), svg)
  return range && !range.collapsed ? range : null
}

/** SVG anchors use the selected labels' context, without XML/source offsets or stylesheet text. */
export function svgSelectionAnchor(range: Range) {
  const ancestor = range.commonAncestorContainer
  const element = ancestor.nodeType === Node.ELEMENT_NODE ? ancestor as Element : ancestor.parentElement
  const svg = element?.closest('svg')
  if (!svg) return null
  const walker = document.createTreeWalker(svg, NodeFilter.SHOW_TEXT)
  let full = ''
  let raw = ''
  let start = -1
  let node: Node | null
  while ((node = walker.nextNode())) {
    const parent = node.parentElement
    if (!parent?.closest('text') || parent.closest('defs, style, title, desc')) continue
    const text = node.textContent ?? ''
    const offset = full.length
    full += text
    if (!range.intersectsNode(node)) continue
    const from = node === range.startContainer ? range.startOffset : 0
    const to = node === range.endContainer ? range.endOffset : text.length
    if (from === to) continue
    if (start < 0) start = offset + from
    raw += text.slice(from, to)
  }
  const quote = raw.trim()
  if (!quote) return null
  start += raw.length - raw.trimStart().length
  return {
    quote,
    prefix: full.slice(Math.max(0, start - 32), start),
    suffix: full.slice(start + quote.length, start + quote.length + 32),
  }
}
