import { describe, it, expect } from 'vitest'
import { createUtteranceEndpointer, isSendableTranscript, DEFAULT_ENDPOINTER_CONFIG } from '../hooks/handsFreeVad'
import type { EndpointerEvent } from '../hooks/handsFreeVad'

/**
 * Endpointing state machine, driven with synthetic RMS sequences. Real audio
 * cannot be emulated in this environment, so these pin the decision logic the
 * live mic feeds: calibration, the silence hangover, the blip filter, the
 * hysteresis pair, floor adaptation and both failsafes.
 */

const STEP_MS = 50

/** Feed a level for `ms`, returning the first event fired (if any) and the
 *  timestamp it fired at. Time starts where the previous run left off. */
function run(
  ep: ReturnType<typeof createUtteranceEndpointer>,
  clock: { now: number },
  level: number,
  ms: number,
): { event: EndpointerEvent | null; at: number } {
  const end = clock.now + ms
  while (clock.now < end) {
    clock.now += STEP_MS
    const ev = ep.feed(clock.now, level)
    if (ev) return { event: ev, at: clock.now }
  }
  return { event: null, at: clock.now }
}

const QUIET = 0.01
const SPEECH = 0.3

describe('createUtteranceEndpointer', () => {
  it('endpoints after speech followed by the full silence hangover', () => {
    const ep = createUtteranceEndpointer()
    const clock = { now: 0 }
    expect(run(ep, clock, QUIET, 500).event).toBeNull() // calibration
    expect(run(ep, clock, SPEECH, 900).event).toBeNull() // speaking
    const silenceFrom = clock.now
    const r = run(ep, clock, QUIET, 2000)
    expect(r.event).toBe('endpoint')
    // Fires once the 1200ms hangover elapses — not earlier, not much later.
    expect(r.at - silenceFrom).toBeGreaterThanOrEqual(DEFAULT_ENDPOINTER_CONFIG.silenceMs)
    expect(r.at - silenceFrom).toBeLessThan(DEFAULT_ENDPOINTER_CONFIG.silenceMs + 3 * STEP_MS)
  })

  it('does not endpoint while the silence run is shorter than the hangover', () => {
    const ep = createUtteranceEndpointer()
    const clock = { now: 0 }
    run(ep, clock, QUIET, 500)
    run(ep, clock, SPEECH, 900)
    expect(run(ep, clock, QUIET, 1000).event).toBeNull() // 1.0s < 1.2s
  })

  it('resumed speech during the hangover cancels the pending endpoint', () => {
    const ep = createUtteranceEndpointer()
    const clock = { now: 0 }
    run(ep, clock, QUIET, 500)
    run(ep, clock, SPEECH, 900)
    expect(run(ep, clock, QUIET, 1000).event).toBeNull()
    expect(run(ep, clock, SPEECH, 500).event).toBeNull() // took a breath, kept going
    expect(run(ep, clock, QUIET, 1000).event).toBeNull() // hangover restarts in full
    expect(run(ep, clock, QUIET, 400).event).toBe('endpoint')
  })

  it('ignores a sub-minimum blip and keeps listening', () => {
    const ep = createUtteranceEndpointer()
    const clock = { now: 0 }
    run(ep, clock, QUIET, 500)
    expect(run(ep, clock, SPEECH, 300).event).toBeNull() // 0.3s < 0.6s minimum
    expect(run(ep, clock, QUIET, 3000).event).toBeNull() // discarded, no endpoint
    expect(ep.state).toBe('waiting')
    // A real utterance afterwards still endpoints.
    run(ep, clock, SPEECH, 900)
    expect(run(ep, clock, QUIET, 1400).event).toBe('endpoint')
  })

  it('accumulates interrupted speech toward the minimum across a blip boundary', () => {
    const ep = createUtteranceEndpointer()
    const clock = { now: 0 }
    run(ep, clock, QUIET, 500)
    // Two 400ms bursts inside one hangover window: 800ms cumulative > 600ms.
    run(ep, clock, SPEECH, 400)
    run(ep, clock, QUIET, 600)
    run(ep, clock, SPEECH, 400)
    expect(run(ep, clock, QUIET, 1400).event).toBe('endpoint')
  })

  it('calibrates the floor so speech-level background raises the enter threshold', () => {
    const ep = createUtteranceEndpointer()
    const clock = { now: 0 }
    // One step past the window: the clock starts at the first feed, so the
    // floor settles on the feed AFTER calibrationMs elapses.
    run(ep, clock, 0.08, 600)
    expect(ep.noiseFloor).toBeCloseTo(0.08, 5)
    // 0.15 < 0.08 * 2.5: still background, never becomes speech.
    expect(run(ep, clock, 0.15, 2000).event).toBeNull()
    expect(ep.state).toBe('waiting')
    // 0.35 clears the scaled threshold even after the floor drifted up a bit
    // while the 0.15 background played.
    run(ep, clock, 0.35, 900)
    expect(ep.state).toBe('speech')
  })

  it('adapts the floor upward when the background gets louder after calibration', () => {
    const ep = createUtteranceEndpointer()
    const clock = { now: 0 }
    run(ep, clock, QUIET, 500)
    // Background rises to 0.04 — under the initial 0.05 enter threshold — for
    // 10s (well past the 3s adaptation constant). The floor converges toward
    // it, lifting the enter threshold to ~0.1, so a 0.07 hum that WOULD have
    // read as speech against the calibrated floor no longer does.
    run(ep, clock, 0.04, 10000)
    expect(ep.noiseFloor).toBeGreaterThan(0.035)
    expect(run(ep, clock, 0.07, 2000).event).toBeNull()
    expect(ep.state).toBe('waiting')
  })

  it('failsafe-endpoints a capture that never goes silent', () => {
    const ep = createUtteranceEndpointer()
    const clock = { now: 0 }
    run(ep, clock, QUIET, 500)
    const r = run(ep, clock, SPEECH, 40000)
    expect(r.event).toBe('endpoint')
    expect(r.at).toBeLessThanOrEqual(500 + DEFAULT_ENDPOINTER_CONFIG.maxUtteranceMs + 2 * STEP_MS)
  })

  it('abandons when nobody speaks for the give-up window', () => {
    const ep = createUtteranceEndpointer()
    const clock = { now: 0 }
    const r = run(ep, clock, QUIET, 60000)
    expect(r.event).toBe('abandon')
    expect(r.at).toBeLessThanOrEqual(DEFAULT_ENDPOINTER_CONFIG.noSpeechMs + 2 * STEP_MS)
  })

  it('is spent after firing: further feeds return null', () => {
    const ep = createUtteranceEndpointer()
    const clock = { now: 0 }
    run(ep, clock, QUIET, 500)
    run(ep, clock, SPEECH, 900)
    expect(run(ep, clock, QUIET, 1400).event).toBe('endpoint')
    expect(run(ep, clock, SPEECH, 2000).event).toBeNull()
    expect(run(ep, clock, QUIET, 5000).event).toBeNull()
  })

  it('honors config overrides', () => {
    const ep = createUtteranceEndpointer({ silenceMs: 400, minUtteranceMs: 100 })
    const clock = { now: 0 }
    run(ep, clock, QUIET, 500)
    run(ep, clock, SPEECH, 200)
    const r = run(ep, clock, QUIET, 600)
    expect(r.event).toBe('endpoint')
  })
})

describe('isSendableTranscript', () => {
  it('rejects empty, whitespace and up to two characters', () => {
    expect(isSendableTranscript('')).toBe(false)
    expect(isSendableTranscript('   ')).toBe(false)
    expect(isSendableTranscript(' a ')).toBe(false)
    expect(isSendableTranscript('ok')).toBe(false)
  })

  it('accepts three or more non-space characters', () => {
    expect(isSendableTranscript('yes')).toBe(true)
    expect(isSendableTranscript('  turn left here  ')).toBe(true)
  })
})
