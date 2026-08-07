import { useCallback, useEffect, useRef, useState } from 'react'
import { createUtteranceEndpointer, type EndpointerConfig } from './handsFreeVad'

/**
 * Hands-free ("car mode") dictation loop over the BATCH voice pipeline:
 * record → energy-based endpoint on end-of-speech silence → stop → transcribe
 * → the caller auto-sends the transcript → re-arm and listen for the next
 * utterance. Streaming STT is out of scope — its providers carry their own
 * semantic endpointer, which already auto-submits via onEndpoint.
 *
 * The hook owns arming state, the VAD polling loop and the re-arm cycle; the
 * caller supplies capture control (start / commit-stop / discard-cancel) and
 * consumes the auto-send flag when the transcript is delivered. Arming only
 * ever happens through an explicit user action (`arm`), never on mount — a
 * persisted preference must not surprise-open the mic on page load.
 */

export type HandsFreePhase = 'listening' | 'processing' | 'sent'

/**
 * VAD poll cadence. The meter updates the sample every animation frame, so
 * 50ms sampling bounds endpoint timing error to ±50ms against windows of
 * 500-1200ms — well under perception while staying cheap.
 */
const FEED_INTERVAL_MS = 50
/**
 * Gap between a finished cycle and the next capture. Lets MediaRecorder fully
 * release the device first — Android Chrome can hand back a dead audio session
 * when the mic is re-acquired in the same tick it was released.
 */
const REARM_DELAY_MS = 400
/**
 * Consecutive failed transcriptions before the loop gives up. One failure can
 * be a hiccup worth riding out hands-free; repeated failures mean the STT
 * backend is down and looping would record speech into a black hole.
 */
const MAX_TRANSCRIBE_FAILURES = 3
/** How long the "sent" confirmation phase shows before reverting to listening. */
const SENT_FLASH_MS = 1500

interface Opts {
  /**
   * Hands-free is currently usable: preference on, batch mode, STT enabled,
   * mic supported. When this drops mid-loop the loop disarms itself.
   */
  enabled: boolean
  recording: boolean
  transcribing: boolean
  error: string | null
  clearError: () => void
  /** The meter's per-frame envelope (useVoiceInput's sampleRef). */
  sampleRef: { current: { level: number } }
  /** Start a capture — the caller's gated start path. */
  start: () => void
  /** Stop the current capture and transcribe it (commit). */
  stopCommit: () => void
  /** Discard the current capture without transcribing. */
  cancelDiscard: () => void
  vadConfig?: Partial<EndpointerConfig>
}

export interface HandsFreeLoop {
  /** True while the listening loop is active. */
  armed: boolean
  /** Visual state for the composer strip; null when not armed. */
  phase: HandsFreePhase | null
  /** Start the loop (self-disarms on the next effect pass if not `enabled`). */
  arm: () => void
  /** Stop the loop without touching any capture in flight. */
  disarm: () => void
  /**
   * Stop the loop AND resolve the in-flight capture: 'commit' transcribes it
   * into the composer (nothing auto-sends after a manual act), 'discard'
   * drops the audio (the user has taken over by typing).
   */
  exit: (mode: 'commit' | 'discard') => void
  /**
   * Read-and-clear the auto-send flag. True exactly when the transcript being
   * delivered came from an endpointer-stopped capture, so the caller sends it
   * instead of inserting it into the composer.
   */
  consumeAutoSend: () => boolean
  /** Record that an auto-send happened, for the "sent" confirmation phase. */
  noteSent: () => void
}

export function useHandsFreeLoop(opts: Opts): HandsFreeLoop {
  const [armed, setArmed] = useState(false)
  const [sentFlash, setSentFlash] = useState(false)
  // Set when the endpointer (not the user) stops a capture; consumed by the
  // caller when the transcript lands. A ref, not state: it must be readable
  // synchronously inside the transcript-delivery callback.
  const autoSendRef = useRef(false)
  // True once the current arm cycle actually reached `recording`. Splits the
  // two error cases below: an error with no recording is a mic/permission
  // failure (disarm — retrying would loop the permission prompt), an error
  // after a recording is a transcription failure (retry up to the cap).
  const cycleRecordedRef = useRef(false)
  // True once this arm cycle has actually called start(). An error observed
  // BEFORE any start attempt is a leftover from an earlier session (the mic
  // strip still showing a failed capture) — the arm is an explicit retry, so
  // that error is cleared rather than treated as this cycle's failure.
  const startAttemptedRef = useRef(false)
  const failuresRef = useRef(0)
  // Callbacks and the sample ref are read through this so effects depend only
  // on the primitive inputs — an inline-closure prop can't retrigger them.
  const optsRef = useRef(opts)
  optsRef.current = opts

  const disarm = useCallback(() => {
    setArmed(false)
    setSentFlash(false)
    autoSendRef.current = false
    failuresRef.current = 0
    cycleRecordedRef.current = false
    startAttemptedRef.current = false
  }, [])

  // Deliberately not gated on `enabled` here: the caller may flip the
  // preference and arm in the same event handler, before the new prop lands in
  // optsRef. The cycle effect below sees the settled value and disarms if the
  // loop is genuinely unusable.
  const arm = useCallback(() => {
    autoSendRef.current = false
    failuresRef.current = 0
    cycleRecordedRef.current = false
    startAttemptedRef.current = false
    setArmed(true)
  }, [])

  const exit = useCallback((mode: 'commit' | 'discard') => {
    const o = optsRef.current
    const wasRecording = o.recording
    disarm()
    if (wasRecording) {
      if (mode === 'commit') o.stopCommit()
      else o.cancelDiscard()
    }
  }, [disarm])

  // Arm / re-arm cycle. Runs whenever the loop is idle: after arm(), and after
  // each capture+transcription finishes (sent, empty, or failed alike).
  useEffect(() => {
    if (!armed) return
    if (!opts.enabled) { disarm(); return }
    if (opts.error) {
      // Predates this cycle's first start attempt: a leftover from an earlier
      // failed capture, not this loop's failure. Arming was an explicit
      // retry, so clear it (the re-run with error null schedules the start).
      if (!startAttemptedRef.current) { opts.clearError(); return }
      if (!cycleRecordedRef.current) { disarm(); return }
      failuresRef.current += 1
      if (failuresRef.current >= MAX_TRANSCRIBE_FAILURES) { disarm(); return }
      // Clearing the error re-runs this effect with error null, which
      // schedules the retry capture below.
      opts.clearError()
      return
    }
    if (opts.recording || opts.transcribing) return
    const t = setTimeout(() => {
      const o = optsRef.current
      if (!o.recording && !o.transcribing && !o.error) {
        cycleRecordedRef.current = false
        startAttemptedRef.current = true
        o.start()
      }
    }, REARM_DELAY_MS)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- callbacks are read via optsRef (see its comment)
  }, [armed, opts.enabled, opts.recording, opts.transcribing, opts.error, disarm])

  // VAD polling while a hands-free capture is live. One endpointer per
  // capture: it calibrates its noise floor from that capture's opening window.
  useEffect(() => {
    if (!armed || !opts.recording) return
    cycleRecordedRef.current = true
    const ep = createUtteranceEndpointer(optsRef.current.vadConfig)
    let fired = false
    const iv = setInterval(() => {
      if (fired) return
      const ev = ep.feed(Date.now(), optsRef.current.sampleRef.current.level)
      if (ev === 'endpoint') {
        fired = true
        autoSendRef.current = true
        optsRef.current.stopCommit()
      } else if (ev === 'abandon') {
        // Nobody spoke for the whole give-up window: stop holding the mic
        // open. Discard (not commit) — transcribing silence yields junk.
        fired = true
        disarm()
        optsRef.current.cancelDiscard()
      }
    }, FEED_INTERVAL_MS)
    return () => clearInterval(iv)
  }, [armed, opts.recording, disarm])

  const consumeAutoSend = useCallback(() => {
    const v = autoSendRef.current
    autoSendRef.current = false
    // A delivered transcript proves the pipeline works again.
    if (v) failuresRef.current = 0
    return v
  }, [])

  const noteSent = useCallback(() => { setSentFlash(true) }, [])
  useEffect(() => {
    if (!sentFlash) return
    const t = setTimeout(() => setSentFlash(false), SENT_FLASH_MS)
    return () => clearTimeout(t)
  }, [sentFlash])

  // 'listening' covers the between-cycles gaps too (re-arm delay, capture
  // startup) so the strip doesn't flicker through idle several times a minute.
  const phase: HandsFreePhase | null = !armed
    ? null
    : opts.transcribing
      ? 'processing'
      : sentFlash
        ? 'sent'
        : 'listening'

  return { armed, phase, arm, disarm, exit, consumeAutoSend, noteSent }
}
