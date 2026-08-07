// Energy-based utterance endpointing for hands-free ("car mode") dictation.
//
// Batch STT records with MediaRecorder and transcribes only after the capture
// stops, so ending an utterance without a tap needs a client-side voice
// activity decision. This module is the pure, unit-testable half: a state
// machine fed the level meter's envelope-smoothed RMS (mic.ts writes it into
// `AudioSample.level` every animation frame) that decides when the speaker is
// done. The React half (useHandsFreeLoop) owns timers, capture control and
// re-arming.

export interface EndpointerConfig {
  /** Noise-floor sampling window at the start of a capture. */
  calibrationMs: number
  /** Continuous sub-threshold run that ends an utterance. */
  silenceMs: number
  /**
   * Cumulative speech below this is a blip (cough, door slam): discarded, and
   * listening continues instead of stopping for a transcript of nothing.
   */
  minUtteranceMs: number
  /**
   * Hard per-capture cap once speech has started — endpoint regardless. The
   * failsafe for a miscalibrated floor (constant loud background reading as
   * endless speech): without it the mic would stay open until the browser
   * kills the session, and everything said would be lost instead of sent long.
   */
  maxUtteranceMs: number
  /**
   * Give-up window with no speech at all. A loop left running in a pocket must
   * not hold the mic open indefinitely; the caller disarms on this event.
   */
  noSpeechMs: number
}

export const DEFAULT_ENDPOINTER_CONFIG: EndpointerConfig = {
  calibrationMs: 500,
  silenceMs: 1200,
  minUtteranceMs: 600,
  maxUtteranceMs: 30000,
  noSpeechMs: 45000,
}

// Threshold shaping over the meter's [0, 1] envelope (mic.ts applies a 2.2x
// RMS gain with ~50ms attack / ~250ms release). Speech in that scale typically
// lands at 0.15-0.6 and a quiet room under 0.05, so the absolute minimums keep
// the endpointer sane when calibration reports near-zero, and the
// factor/margin pair lifts both thresholds proportionally over a real noise
// floor (a moving car). Exit sits below enter (hysteresis) so the envelope's
// release tail doesn't flap the state machine on every syllable boundary.
const ENTER_ABS_MIN = 0.05
const EXIT_ABS_MIN = 0.035
const ENTER_FLOOR_FACTOR = 2.5
const ENTER_FLOOR_MARGIN = 0.04
const EXIT_FLOOR_FACTOR = 1.8
const EXIT_FLOOR_MARGIN = 0.025
/**
 * EMA time constant for tracking the floor between utterances, so a background
 * that changes mid-session (acceleration, HVAC) moves the thresholds with it.
 * Frozen during speech — adapting toward the speaker's own voice would raise
 * the floor until their speech read as silence.
 */
const FLOOR_ADAPT_TAU_MS = 3000
/**
 * Ceiling on the adapted floor. Keeps a pathological background from pushing
 * the enter threshold past any possible speech level; past this point the
 * maxUtteranceMs failsafe is the endpointer.
 */
const FLOOR_MAX = 0.3

export type EndpointerEvent = 'endpoint' | 'abandon'
export type EndpointerState = 'calibrating' | 'waiting' | 'speech' | 'trailing'

export interface UtteranceEndpointer {
  /** Current phase, exposed for tests and debugging. */
  readonly state: EndpointerState
  /** Adapted noise floor, exposed for tests. */
  readonly noiseFloor: number
  /**
   * Advance the machine with one level sample. `now` is a monotonic-enough
   * clock in ms (Date.now); `level` is the meter envelope in [0, 1]. Returns an
   * event at most once per instance — after 'endpoint' or 'abandon' the
   * instance is spent and further feeds are no-ops.
   */
  feed(now: number, level: number): EndpointerEvent | null
}

export function createUtteranceEndpointer(overrides: Partial<EndpointerConfig> = {}): UtteranceEndpointer {
  const cfg = { ...DEFAULT_ENDPOINTER_CONFIG, ...overrides }
  let startAt: number | null = null
  let lastT = 0
  let calibMin = Infinity
  let floor = 0
  let state: EndpointerState = 'calibrating'
  let speechMs = 0
  let firstSpeechAt: number | null = null
  let silenceStart = 0
  let done = false

  const enterThreshold = () => Math.max(ENTER_ABS_MIN, floor * ENTER_FLOOR_FACTOR, floor + ENTER_FLOOR_MARGIN)
  const exitThreshold = () => Math.max(EXIT_ABS_MIN, floor * EXIT_FLOOR_FACTOR, floor + EXIT_FLOOR_MARGIN)

  return {
    get state() { return state },
    get noiseFloor() { return floor },
    feed(now: number, level: number): EndpointerEvent | null {
      if (done) return null
      if (startAt === null) { startAt = now; lastT = now }
      const dt = Math.max(0, now - lastT)
      lastT = now

      if (state === 'calibrating') {
        // Floor = MIN over the window, not the mean: hands-free users start
        // talking right after arming, so the window often contains speech. Any
        // brief pre-speech gap yields a floor uncontaminated by their voice; a
        // window with no quiet moment at all falls back to the absolute
        // minimum thresholds, which still endpoint correctly in a quiet room.
        if (level < calibMin) calibMin = level
        if (now - startAt >= cfg.calibrationMs) {
          floor = Math.min(FLOOR_MAX, calibMin === Infinity ? 0 : calibMin)
          state = 'waiting'
        }
        return null
      }

      if (state !== 'speech' && dt > 0) {
        const a = 1 - Math.exp(-dt / FLOOR_ADAPT_TAU_MS)
        floor = Math.min(FLOOR_MAX, floor + (Math.min(level, FLOOR_MAX) - floor) * a)
      }

      // Failsafe cap, checked in every post-calibration state: a noisy capture
      // can oscillate speech<->trailing forever without a full silenceMs run.
      if (firstSpeechAt !== null && now - firstSpeechAt >= cfg.maxUtteranceMs) {
        done = true
        return 'endpoint'
      }

      if (state === 'waiting') {
        if (level >= enterThreshold()) {
          state = 'speech'
          if (firstSpeechAt === null) firstSpeechAt = now
        } else if (firstSpeechAt === null && now - startAt >= cfg.noSpeechMs) {
          done = true
          return 'abandon'
        }
      } else if (state === 'speech') {
        speechMs += dt
        if (level < exitThreshold()) {
          state = 'trailing'
          silenceStart = now
        }
      } else if (state === 'trailing') {
        if (level >= enterThreshold()) {
          state = 'speech'
        } else if (now - silenceStart >= cfg.silenceMs) {
          if (speechMs >= cfg.minUtteranceMs) {
            done = true
            return 'endpoint'
          }
          // Blip: too little cumulative speech to be an utterance. Reset and
          // keep listening — firstSpeechAt clears too, so the noSpeechMs
          // give-up clock (measured from capture start) still runs.
          speechMs = 0
          firstSpeechAt = null
          state = 'waiting'
        }
      }
      return null
    },
  }
}

/**
 * Auto-send gate for a hands-free transcript. Two or fewer characters after
 * trimming is noise (a stray "uh" or punctuation mark the model emitted for a
 * marginal capture) — the loop re-arms without sending.
 */
export function isSendableTranscript(text: string): boolean {
  return text.trim().length > 2
}

/** localStorage key for the persisted hands-free preference (default off). */
export const HANDS_FREE_LS_KEY = 'mc-handsfree-voice'
