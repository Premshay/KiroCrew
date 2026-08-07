/**
 * The streaming-TTS sentence cutter: terminal punctuation only, incremental
 * from the already-spoken offset, and a minimum span so one-word confirmations
 * don't spend a synthesis round trip.
 */
import { describe, it, expect } from 'vitest'
import { lastSentenceCut, nextSpeakableSpan, MIN_TTS_CHARS } from '../hooks/sentenceCutter'

describe('lastSentenceCut', () => {
  it('cuts one past the last terminal followed by whitespace', () => {
    const s = 'First sentence. Second sentence! Third trailing'
    expect(lastSentenceCut(s, 0)).toBe(s.indexOf('!') + 1)
  })

  it('cuts at a terminal at end-of-text', () => {
    const s = 'A finished question?'
    expect(lastSentenceCut(s, 0)).toBe(s.length)
  })

  it('returns -1 when no new terminal exists past the offset', () => {
    const s = 'First sentence. Still being typed'
    const afterFirst = s.indexOf('.') + 1
    expect(lastSentenceCut(s, afterFirst)).toBe(-1)
  })

  it('does not cut inside a decimal number', () => {
    expect(lastSentenceCut('The value is 3.14159 so far', 0)).toBe(-1)
  })

  it('does not treat commas, colons, or newlines as boundaries', () => {
    expect(lastSentenceCut('first, second: third\nfourth', 0)).toBe(-1)
  })

  it('does not cut after an abbreviation', () => {
    expect(lastSentenceCut('Use the queue, e.g. an ordered list', 0)).toBe(-1)
    expect(lastSentenceCut('Ask Dr. Smith about it', 0)).toBe(-1)
  })

  it('does not cut after a list enumerator, but does cut at the item end', () => {
    const s = '1. Check the logs first.\n2. Restart'
    // The enumerator's dot is guarded; the item's real terminal is the cut.
    expect(lastSentenceCut(s, 0)).toBe(s.indexOf('first.') + 'first.'.length)
  })

  it('cuts after a number that ends a real sentence mid-line', () => {
    const s = 'The answer is 42. Next question'
    expect(lastSentenceCut(s, 0)).toBe(s.indexOf('42.') + 3)
  })

  it('holds everything inside an unbalanced code fence', () => {
    const open = 'Here is the fix. ```py\n# step one. run it\n'
    // The prose sentence before the fence still cuts…
    expect(lastSentenceCut(open, 0)).toBe(open.indexOf('fix.') + 4)
    // …but a terminal inside the still-open fence is held back.
    expect(lastSentenceCut(open, open.indexOf('fix.') + 4)).toBe(-1)
    // Once the fence closes, a terminal after it is a boundary again.
    const closed = open + '``` That fixes it. '
    expect(lastSentenceCut(closed, open.indexOf('fix.') + 4)).toBe(closed.indexOf('it.') + 3)
  })
})

describe('nextSpeakableSpan', () => {
  it('returns the unspoken completed sentences and the advanced counter', () => {
    const s = 'One done. Two done. Three in prog'
    const span = nextSpeakableSpan(s, 0)
    expect(span).not.toBeNull()
    expect(span!.text).toBe('One done. Two done.')
    // The counter advances to one past the terminal (the space before
    // "Three" is left for the next span's trim).
    expect(span!.nextSpokenLen).toBe(s.indexOf(' Three'))
    // Nothing new: the remaining text has no terminal yet.
    expect(nextSpeakableSpan(s, span!.nextSpokenLen)).toBeNull()
  })

  it('is incremental: a later flush speaks only text past the counter', () => {
    const first = 'Alpha sentence here. '
    const full = first + 'Beta sentence lands later. '
    const spanA = nextSpeakableSpan(first, 0)!
    const spanB = nextSpeakableSpan(full, spanA.nextSpokenLen)!
    expect(spanA.text).toBe('Alpha sentence here.')
    expect(spanB.text).toBe('Beta sentence lands later.')
  })

  it('withholds spans under the minimum speakable length', () => {
    expect(nextSpeakableSpan('Ok. ', 0)).toBeNull()
    expect('Ok.'.length).toBeLessThan(MIN_TTS_CHARS)
  })
})
