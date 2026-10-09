import { describe, expect, it } from 'vitest'
import {
  nextRetainedRange,
  selectedRowRange,
  selectionTouchesContainer,
} from '../utils/selectionRetention'

function selection(anchorNode: Node, focusNode: Node): Selection {
  // DOM Ranges normalize start/end and so cannot model a backward mobile-handle
  // selection. The helper only reads these public Selection fields.
  return {
    anchorNode,
    anchorOffset: 0,
    focusNode,
    focusOffset: 0,
    isCollapsed: false,
    setBaseAndExtent(anchor, anchorOffset, focus, focusOffset) {
      this.anchorNode = anchor
      this.anchorOffset = anchorOffset
      this.focusNode = focus
      this.focusOffset = focusOffset
    },
  } as Selection
}

describe('selectionRetention', () => {
  it('keeps every transcript row between reverse selection endpoints', () => {
    const container = document.createElement('div')
    container.innerHTML = '<div data-display-index="4">first</div><div data-display-index="5">second</div><div data-display-index="6">third</div>'
    document.body.append(container)
    const rows = container.querySelectorAll('[data-display-index]')

    const selected = selection(rows[2].firstChild!, rows[0].firstChild!)

    expect(selectedRowRange(container, selected)).toEqual({ start: 4, end: 7 })
    expect(selectionTouchesContainer(container, selected)).toBe(true)
  })

  it('does not treat a document-spanning selection as a transcript range', () => {
    const container = document.createElement('div')
    container.innerHTML = '<div data-display-index="4">chat text</div>'
    const outside = document.createElement('p')
    outside.textContent = 'dashboard chrome'
    document.body.append(container, outside)

    const selected = selection(container.firstChild!.firstChild!, outside.firstChild!)

    expect(selectedRowRange(container, selected)).toBeNull()
    expect(selectionTouchesContainer(container, selected)).toBe(true)
  })

  describe('nextRetainedRange', () => {
    // A handle over the scroller's padding under the composer sits inside the
    // transcript but on no row. Releasing there let the start row unmount and
    // re-rooted the selection at the top of the transcript.
    const build = () => {
      document.body.innerHTML = '<div id="sc"><div data-display-index="4"><p id="r4">start row</p></div><div id="pad"></div></div><p id="out">outside</p>'
      return document.getElementById('sc') as HTMLElement
    }
    const text = (id: string) => document.getElementById(id)!.firstChild ?? document.getElementById(id)!

    it('keeps the last retained span while an endpoint sits on no row inside the transcript', () => {
      const sc = build()
      expect(nextRetainedRange(sc, selection(text('r4'), document.getElementById('pad')!))).toBe('keep')
    })
    it('replaces the span when both endpoints sit on rows', () => {
      const sc = build()
      expect(nextRetainedRange(sc, selection(text('r4'), text('r4')))).toEqual({ start: 4, end: 5 })
    })
    // A fresh Android long-press can report an endpoint outside the rows; the
    // selection itself must come through untouched.
    it('never rewrites the selection', () => {
      const sc = build()
      const selected = selection(text('r4'), text('out'))
      selected.setBaseAndExtent = () => { throw new Error('selection rewritten') }
      expect(nextRetainedRange(sc, selected)).toBe('keep')
      expect(selected.focusNode).toBe(text('out'))
    })
    it('releases when the selection collapses or leaves the transcript', () => {
      const sc = build()
      expect(nextRetainedRange(sc, null)).toBeNull()
      expect(nextRetainedRange(sc, { ...selection(text('r4'), text('r4')), isCollapsed: true } as Selection)).toBeNull()
      expect(nextRetainedRange(sc, selection(text('out'), text('out')))).toBeNull()
    })
  })
})
