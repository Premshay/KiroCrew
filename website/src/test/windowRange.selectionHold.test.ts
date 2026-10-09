import { describe, it, expect, afterEach } from 'vitest'
import { selectionHeldIn } from '../hooks/virtualizer/windowRange'

/**
 * While the reader holds a text selection inside the transcript the window only
 * grows: unmounting the row holding the selection's anchor re-roots the
 * selection at an ancestor and selects everything above it on touch.
 */
describe('selectionHeldIn', () => {
  afterEach(() => { document.getSelection()?.removeAllRanges(); document.body.innerHTML = '' })

  const setup = () => {
    document.body.innerHTML = '<div id="scroller"><p id="a">first row text</p><p id="b">second row</p></div><p id="out">outside</p>'
    return document.getElementById('scroller') as HTMLElement
  }
  const select = (id: string, from: number, to: number) => {
    const node = document.getElementById(id)!.firstChild!
    const range = document.createRange()
    range.setStart(node, from); range.setEnd(node, to)
    const sel = document.getSelection()!
    sel.removeAllRanges(); sel.addRange(range)
  }

  it('is true for a non-empty selection anchored in the scroller', () => {
    const el = setup(); select('a', 0, 5)
    expect(selectionHeldIn(el)).toBe(true)
  })
  it('is false with no selection or a collapsed caret', () => {
    const el = setup()
    expect(selectionHeldIn(el)).toBe(false)
    select('a', 2, 2)
    expect(selectionHeldIn(el)).toBe(false)
  })
  it('is false for a selection anchored outside the scroller', () => {
    const el = setup(); select('out', 0, 3)
    expect(selectionHeldIn(el)).toBe(false)
  })
})
