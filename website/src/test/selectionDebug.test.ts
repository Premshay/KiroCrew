import { describe, it, expect, afterEach } from 'vitest'
import { describeEndpoint, describeSelectedText, recordSelectionDebug, selectionDebugEntries } from '../utils/selectionDebug'

describe('selection debug trace', () => {
  afterEach(() => { document.body.innerHTML = '' })

  it('describes where an endpoint sits relative to the transcript', () => {
    document.body.innerHTML = '<header id="h">title</header><div id="sc"><div data-display-index="7"><p id="r">row</p></div><div id="pad"></div></div><footer id="f">composer</footer>'
    const sc = document.getElementById('sc') as HTMLElement
    expect(describeEndpoint(sc, document.getElementById('r')!.firstChild)).toBe('row 7 #text')
    expect(describeEndpoint(sc, document.getElementById('pad'))).toBe('in-scroller')
    expect(describeEndpoint(sc, document.getElementById('h')!.firstChild)).toBe('outside-before')
    expect(describeEndpoint(sc, document.getElementById('f')!.firstChild)).toBe('outside-after')
    document.getElementById('h')!.dataset.testid = 'chat-header'
    expect(describeEndpoint(sc, document.getElementById('h')!.firstChild)).toBe('outside-before(chat-header)')
    const gone = document.createTextNode('x')
    expect(describeEndpoint(sc, gone)).toBe('detached')
  })

  it('keeps a bounded, de-duplicated history', () => {
    for (let i = 0; i < 12; i++) recordSelectionDebug({ anchor: `row ${i}`, focus: 'row 9', offsets: '0:0', retained: 'keep' })
    recordSelectionDebug({ anchor: 'row 11', focus: 'row 9', offsets: '0:0', retained: 'keep' })
    const entries = selectionDebugEntries()
    expect(entries.length).toBe(8)
    expect(entries[entries.length - 1].anchor).toBe('row 11')
  })

  it('pins the first change of a selection through a long drag', () => {
    recordSelectionDebug({ anchor: 'row 3 #text', focus: 'row 3 #text', offsets: '5:9', retained: '3-4' }, true)
    for (let i = 10; i < 30; i++) recordSelectionDebug({ anchor: 'row 3 DIV', focus: 'row 3 #text', offsets: `0:${i}`, retained: '3-4' })
    const entries = selectionDebugEntries()
    expect(entries.length).toBe(8)
    expect(entries[0].offsets).toBe('5:9')
    expect(entries[entries.length - 1].offsets).toBe('0:29')
  })

  it('shows where a selection begins and ends', () => {
    expect(describeSelectedText('found the')).toBe('"found the" 9ch')
    const long = 'reached the title, they switched off\n\nand the title joined the selection.'
    expect(describeSelectedText(long)).toBe(`"reached the title,"…"ned the selection." ${long.length}ch`)
  })
})
