/**
 * Tests for rewindWithRollback — the helper that wraps api.rewind with a
 * rollback callback. Covers both the success and failure paths so the catch
 * branch is exercised in unit tests rather than requiring a full ChatPage
 * mount + click + reject simulation.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { rewindWithRollback } from '../lib/rewindCall'

describe('rewindWithRollback', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  let warnSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    fetchSpy?.mockRestore()
    warnSpy.mockRestore()
  })

  it('does not invoke rollback when api.rewind resolves', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const rollback = vi.fn()
    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)
    expect(rollback).not.toHaveBeenCalled()
    expect(warnSpy).not.toHaveBeenCalled()
  })

  it('invokes rollback and warns when api.rewind rejects', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'slot is running' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const rollback = vi.fn()
    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)
    expect(rollback).toHaveBeenCalledOnce()
    expect(warnSpy).toHaveBeenCalledWith('rewind failed', expect.any(Error))
  })

  it('invokes rollback when fetch itself throws (network error)', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('network down'))
    const rollback = vi.fn()
    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)
    expect(rollback).toHaveBeenCalledOnce()
    expect(warnSpy).toHaveBeenCalled()
  })
})

describe('rewindWithRollback — the reason reaches the caller', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  let warnSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    fetchSpy?.mockRestore()
    warnSpy.mockRestore()
  })

  it('passes the server message to rollback instead of swallowing it', async () => {
    // A signed-out install answers 503 with exactly what is wrong. Discarding
    // it turned a five-second fix into a week of believing the edit button was
    // broken: the edit reverted with nothing on screen.
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          error: 'Kiro CLI setup or sign-in is required before starting a session.',
          code: 'kiro_prerequisite_required',
        }),
        { status: 503, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    const rollback = vi.fn()

    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)

    expect(rollback).toHaveBeenCalledTimes(1)
    const reason = rollback.mock.calls[0][0]
    expect(typeof reason).toBe('string')
    expect(reason.length).toBeGreaterThan(0)
  })
})
