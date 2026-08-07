import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useHandsFreeLoop } from '../hooks/useHandsFreeLoop'

/**
 * The hands-free loop around the batch capture pipeline: arm → start capture →
 * endpoint on synthetic silence → commit-stop with the auto-send flag armed →
 * re-arm. Capture itself is stubbed (the loop only orchestrates), and the VAD
 * is driven by writing levels into the shared sample ref under fake timers.
 */

const QUIET = 0.01
const SPEECH = 0.3

function makeHarness() {
  const sampleRef = { current: { level: QUIET } }
  const controls = {
    start: vi.fn(),
    stopCommit: vi.fn(),
    cancelDiscard: vi.fn(),
    clearError: vi.fn(),
  }
  const props = {
    enabled: true,
    recording: false,
    transcribing: false,
    error: null as string | null,
  }
  const hook = renderHook(
    (p: typeof props) =>
      useHandsFreeLoop({
        ...p,
        sampleRef,
        ...controls,
        // Short windows keep the fake-timer advances readable; the ratios
        // (silence > min utterance, calibration shortest) mirror production.
        vadConfig: { calibrationMs: 100, silenceMs: 300, minUtteranceMs: 150, noSpeechMs: 2000 },
      }),
    { initialProps: props },
  )
  return { hook, sampleRef, controls, props }
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('useHandsFreeLoop', () => {
  it('arms, starts a capture after the re-arm delay, and endpoints into a committed stop', () => {
    const { hook, sampleRef, controls, props } = makeHarness()
    act(() => hook.result.current.arm())
    expect(hook.result.current.armed).toBe(true)
    expect(hook.result.current.phase).toBe('listening')

    act(() => { vi.advanceTimersByTime(500) })
    expect(controls.start).toHaveBeenCalledTimes(1)

    // Capture begins.
    hook.rerender({ ...props, recording: true })
    act(() => { vi.advanceTimersByTime(200) })   // calibration on quiet
    act(() => { sampleRef.current.level = SPEECH })
    act(() => { vi.advanceTimersByTime(400) })   // speaking
    act(() => { sampleRef.current.level = QUIET })
    act(() => { vi.advanceTimersByTime(600) })   // silence past the hangover
    expect(controls.stopCommit).toHaveBeenCalledTimes(1)
    // The transcript about to arrive is marked for auto-send.
    expect(hook.result.current.consumeAutoSend()).toBe(true)
    // consumeAutoSend is read-and-clear.
    expect(hook.result.current.consumeAutoSend()).toBe(false)
  })

  it('re-arms a new capture after the cycle completes', () => {
    const { hook, controls, props } = makeHarness()
    act(() => hook.result.current.arm())
    act(() => { vi.advanceTimersByTime(500) })
    expect(controls.start).toHaveBeenCalledTimes(1)
    hook.rerender({ ...props, recording: true })
    hook.rerender({ ...props, recording: false, transcribing: true })
    expect(hook.result.current.phase).toBe('processing')
    hook.rerender({ ...props, recording: false, transcribing: false })
    act(() => { vi.advanceTimersByTime(500) })
    expect(controls.start).toHaveBeenCalledTimes(2)
    expect(hook.result.current.phase).toBe('listening')
  })

  it('shows the sent flash, then returns to listening', () => {
    const { hook } = makeHarness()
    act(() => hook.result.current.arm())
    act(() => hook.result.current.noteSent())
    expect(hook.result.current.phase).toBe('sent')
    act(() => { vi.advanceTimersByTime(2000) })
    expect(hook.result.current.phase).toBe('listening')
  })

  it('abandons and discards when nobody speaks for the give-up window', () => {
    const { hook, controls, props } = makeHarness()
    act(() => hook.result.current.arm())
    act(() => { vi.advanceTimersByTime(500) })
    hook.rerender({ ...props, recording: true })
    act(() => { vi.advanceTimersByTime(2500) }) // > noSpeechMs, all quiet
    expect(controls.cancelDiscard).toHaveBeenCalledTimes(1)
    expect(controls.stopCommit).not.toHaveBeenCalled()
    expect(hook.result.current.armed).toBe(false)
  })

  it('exit("commit") stops the capture without auto-send; exit("discard") cancels it', () => {
    const { hook, controls, props } = makeHarness()
    act(() => hook.result.current.arm())
    act(() => { vi.advanceTimersByTime(500) })
    hook.rerender({ ...props, recording: true })
    act(() => hook.result.current.exit('commit'))
    expect(controls.stopCommit).toHaveBeenCalledTimes(1)
    expect(hook.result.current.armed).toBe(false)
    // Committed manually — the transcript must land in the composer, not send.
    expect(hook.result.current.consumeAutoSend()).toBe(false)

    act(() => hook.result.current.arm())
    act(() => { vi.advanceTimersByTime(500) })
    hook.rerender({ ...props, recording: true })
    act(() => hook.result.current.exit('discard'))
    expect(controls.cancelDiscard).toHaveBeenCalledTimes(1)
    expect(hook.result.current.armed).toBe(false)
  })

  it('a stale error from before the arm is cleared, not treated as this cycle failing', () => {
    const { hook, controls, props } = makeHarness()
    // A failure from an earlier session is still displayed when the user arms
    // again: the loop must clear it and start, not instantly disarm on it.
    hook.rerender({ ...props, error: 'Microphone permission denied.' })
    act(() => hook.result.current.arm())
    expect(hook.result.current.armed).toBe(true)
    expect(controls.clearError).toHaveBeenCalledTimes(1)
    // The caller's clearError lands as a prop change (state cleared).
    hook.rerender({ ...props, error: null })
    expect(hook.result.current.armed).toBe(true)
    act(() => { vi.advanceTimersByTime(500) })
    expect(controls.start).toHaveBeenCalledTimes(1)
  })

  it('disarms on a mic failure (error before any recording)', () => {
    const { hook, controls, props } = makeHarness()
    act(() => hook.result.current.arm())
    act(() => { vi.advanceTimersByTime(500) })
    expect(controls.start).toHaveBeenCalledTimes(1)
    hook.rerender({ ...props, error: 'Microphone permission denied.' })
    expect(hook.result.current.armed).toBe(false)
    expect(controls.clearError).not.toHaveBeenCalled()
  })

  it('retries a transcription failure, then gives up after the cap', () => {
    const { hook, controls, props } = makeHarness()
    act(() => hook.result.current.arm())
    for (let attempt = 1; attempt <= 2; attempt++) {
      act(() => { vi.advanceTimersByTime(500) })
      expect(controls.start).toHaveBeenCalledTimes(attempt)
      hook.rerender({ ...props, recording: true })
      hook.rerender({ ...props, recording: false, error: 'Transcription failed.' })
      // The loop clears the error to schedule the retry.
      expect(controls.clearError).toHaveBeenCalledTimes(attempt)
      expect(hook.result.current.armed).toBe(true)
      hook.rerender({ ...props, error: null })
    }
    // Third consecutive failure hits the cap and disarms.
    act(() => { vi.advanceTimersByTime(500) })
    hook.rerender({ ...props, recording: true })
    hook.rerender({ ...props, recording: false, error: 'Transcription failed.' })
    expect(hook.result.current.armed).toBe(false)
  })

  it('disarms itself when enabled drops mid-loop', () => {
    const { hook, props } = makeHarness()
    act(() => hook.result.current.arm())
    expect(hook.result.current.armed).toBe(true)
    hook.rerender({ ...props, enabled: false })
    expect(hook.result.current.armed).toBe(false)
    expect(hook.result.current.phase).toBeNull()
  })

  it('a delivered transcript resets the failure count', () => {
    const { hook, sampleRef, controls, props } = makeHarness()
    act(() => hook.result.current.arm())
    // Two failures, then a success, then two more failures: the loop must
    // still be armed (the success reset the streak below the cap of 3).
    for (const outcome of ['fail', 'fail', 'ok', 'fail', 'fail'] as const) {
      act(() => { vi.advanceTimersByTime(500) })
      hook.rerender({ ...props, recording: true })
      if (outcome === 'fail') {
        hook.rerender({ ...props, recording: false, error: 'Transcription failed.' })
        hook.rerender({ ...props, error: null })
      } else {
        // Full endpoint cycle: speak, go silent, transcript delivered.
        act(() => { vi.advanceTimersByTime(150) })
        act(() => { sampleRef.current.level = SPEECH })
        act(() => { vi.advanceTimersByTime(300) })
        act(() => { sampleRef.current.level = QUIET })
        act(() => { vi.advanceTimersByTime(500) })
        expect(hook.result.current.consumeAutoSend()).toBe(true)
        hook.rerender({ ...props, recording: false, transcribing: true })
        hook.rerender({ ...props, transcribing: false })
      }
    }
    expect(hook.result.current.armed).toBe(true)
    expect(controls.start).toHaveBeenCalledTimes(5)
  })
})
