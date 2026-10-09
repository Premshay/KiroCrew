import { describe, it, expect, afterEach } from 'vitest'
import { describeEndpoint, recordSelectionDebug, selectionDebugEntries } from '../utils/selectionDebug'

describe('selection debug trace', () => {
  afterEach(() => { document.body.innerHTML = '' })

  it('describes where an endpoint sits relative to the transcript', () => {
    document.body.innerHTML = '<header id="h">title</header><div id="sc"><div data-display-index="7"><p id="r">row</p></div><div id="pad"></div></div><footer id="f">composer</footer>'
    const sc = document.getElementById('sc') as HTMLElement
    expect(describeEndpoint(sc, document.getElementById('r')!.firstChild)).toBe('row 7')
    expect(describeEndpoint(sc, document.getElementById('pad'))).toBe('in-scroller')
    expect(describeEndpoint(sc, document.getElementById('h')!.firstChild)).toBe('outside-before')
    expect(describeEndpoint(sc, document.getElementById('f')!.firstChild)).toBe('outside-after')
    const gone = document.createTextNode('x')
    expect(describeEndpoint(sc, gone)).toBe('detached')
  })

  it('keeps a bounded, de-duplicated history', () => {
    for (let i = 0; i < 12; i++) recordSelectionDebug({ anchor: `row ${i}`, focus: 'row 9', clamped: 'no', retained: 'keep' })
    recordSelectionDebug({ anchor: 'row 11', focus: 'row 9', clamped: 'no', retained: 'keep' })
    const entries = selectionDebugEntries()
    expect(entries.length).toBe(8)
    expect(entries[entries.length - 1].anchor).toBe('row 11')
  })
})
