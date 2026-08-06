/**
 * The pack manifest gate.
 *
 * `parseManifest` is what stands between a hand-edited or downloaded pack and the
 * rest of the app, so what it REJECTS matters as much as what it accepts: every
 * early return here is a shape that would otherwise reach the renderer. The format
 * sniffers matter for the same reason — a pack claiming `lottie` whose file is not
 * Lottie would reach `loadAnimation` as arbitrary JSON.
 *
 * These are pure functions with no DOM and no network, so the tests assert real
 * behaviour rather than mocking around it.
 */
import { describe, it, expect } from 'vitest'
import {
  parseManifest,
  serializeManifest,
  isValidSvg,
  isValidLottie,
  REQUIRED_STATES,
  ALL_MOODS,
  STATUS_STATES,
  RANDOM_STATES,
} from '../apps/crew-companion/appearanceTypes'
import type { PackManifest } from '../apps/crew-companion/appearanceTypes'

const META = {
  id: 'robot',
  name: 'Robot',
  author: 'someone',
  type: 'custom',
  format: 'svg',
  thumbnail: 'idle.svg',
}

const good = (over: Record<string, unknown> = {}) =>
  JSON.stringify({ meta: META, states: { idle: 'idle.svg' }, moods: {}, ...over })

describe('parseManifest accepts a well-formed pack', () => {
  it('returns the manifest', () => {
    const r = parseManifest(good())
    expect(r.ok).toBe(true)
    if (r.ok) expect(r.value.meta.id).toBe('robot')
  })

  it('carries a sprite config through when one is present', () => {
    const r = parseManifest(good({
      sprite: { frameWidth: 64, frameHeight: 64, fps: 12 },
    }))
    expect(r.ok).toBe(true)
    if (r.ok) expect(r.value.sprite?.frameWidth).toBe(64)
  })

  it('leaves sprite undefined when it is not an object', () => {
    const r = parseManifest(good({ sprite: 'nope' }))
    expect(r.ok).toBe(true)
    if (r.ok) expect(r.value.sprite).toBeUndefined()
  })
})

describe('parseManifest rejects what should never reach the renderer', () => {
  it('refuses text that is not JSON', () => {
    const r = parseManifest('{ not json')
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toBe('Invalid JSON')
  })

  it.each([
    ['an array', '[]'],
    ['a bare string', '"hello"'],
    ['null', 'null'],
    ['a number', '42'],
  ])('refuses %s at the top level', (_label, json) => {
    expect(parseManifest(json).ok).toBe(false)
  })

  it('refuses a missing meta block', () => {
    const r = parseManifest(JSON.stringify({ states: { idle: 'i.svg' }, moods: {} }))
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toContain('meta')
  })

  it.each(['id', 'name', 'author', 'type', 'format', 'thumbnail'])(
    'refuses a meta block missing "%s"',
    (field) => {
      const meta: Record<string, unknown> = { ...META }
      delete meta[field]
      const r = parseManifest(JSON.stringify({ meta, states: { idle: 'i.svg' }, moods: {} }))
      expect(r.ok).toBe(false)
      if (!r.ok) expect(r.error).toContain(field)
    },
  )

  it('refuses a meta field that is present but not a string', () => {
    const r = parseManifest(JSON.stringify({
      meta: { ...META, name: 7 }, states: { idle: 'i.svg' }, moods: {},
    }))
    expect(r.ok).toBe(false)
  })

  it('refuses a pack with no states block', () => {
    const r = parseManifest(JSON.stringify({ meta: META, moods: {} }))
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toContain('states')
  })

  it.each([...REQUIRED_STATES])('refuses a pack missing the required "%s" state', (state) => {
    const states: Record<string, unknown> = { idle: 'idle.svg' }
    delete states[state]
    const r = parseManifest(JSON.stringify({ meta: META, states, moods: {} }))
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toContain(state)
  })

  it('refuses a missing moods block, even though it may be empty', () => {
    // Empty is fine; ABSENT is not — the editor iterates it.
    const r = parseManifest(JSON.stringify({ meta: META, states: { idle: 'i.svg' } }))
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toContain('moods')
  })

  it('refuses an array where an object is required', () => {
    expect(parseManifest(JSON.stringify({ meta: META, states: [], moods: {} })).ok).toBe(false)
    expect(parseManifest(JSON.stringify({ meta: META, states: { idle: 'i' }, moods: [] })).ok)
      .toBe(false)
  })
})

describe('serializeManifest round-trips', () => {
  it('produces JSON that parses back to the same manifest', () => {
    const parsed = parseManifest(good())
    expect(parsed.ok).toBe(true)
    if (!parsed.ok) return
    const again = parseManifest(serializeManifest(parsed.value as PackManifest))
    expect(again.ok).toBe(true)
    if (again.ok) expect(again.value.meta).toEqual(parsed.value.meta)
  })
})

describe('the format sniffers', () => {
  it.each([
    '<svg viewBox="0 0 1 1"/>',
    '  <SVG>',
    '<?xml version="1.0"?><svg/>',
  ])('accepts %s as svg', (c) => expect(isValidSvg(c)).toBe(true))

  it.each(['', 'not markup', '<div/>'])('rejects %s as svg', (c) =>
    expect(isValidSvg(c)).toBe(false))

  it('accepts a Lottie document carrying every required field', () => {
    expect(isValidLottie(JSON.stringify({
      v: '5.7.4', fr: 30, ip: 0, op: 60, layers: [],
    }))).toBe(true)
  })

  it.each(['v', 'fr', 'ip', 'op', 'layers'])(
    'rejects a Lottie document missing "%s"',
    (field) => {
      const doc: Record<string, unknown> = { v: '5', fr: 30, ip: 0, op: 60, layers: [] }
      delete doc[field]
      expect(isValidLottie(JSON.stringify(doc))).toBe(false)
    },
  )

  it('rejects content that is not JSON at all', () => {
    expect(isValidLottie('<svg/>')).toBe(false)
  })
})

describe('the slot vocabularies the editor and gallery share', () => {
  it('names the five moods the pet can be in', () => {
    expect([...ALL_MOODS]).toEqual(['happy', 'sleepy', 'curious', 'busy', 'scared'])
  })

  it('keeps the categories disjoint, so a slot belongs to exactly one section', () => {
    const groups = [REQUIRED_STATES, STATUS_STATES, RANDOM_STATES, ALL_MOODS].map(
      (g) => [...g] as string[],
    )
    const all = groups.flat()
    expect(new Set(all).size).toBe(all.length)
  })
})
