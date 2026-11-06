import { describe, expect, it } from 'vitest'
import { askIdsOf, resolvedSince, staleAskIds, reconcileQuestions } from '../hooks/useWebSocket'
import { ACTIVE } from './UseWebSocketCoverageTestUtils'

describe('useWebSocket exported reconcile helpers', () => {
  it('collects only ask_ids, skipping legacy cards and an absent map', () => {
    expect(askIdsOf(undefined)).toEqual([])
    expect(askIdsOf({
      a: { ask_id: 'ask-1' },
      b: {},              // legacy card: no server-side record
      c: undefined,
      d: { ask_id: 'ask-2' },
    })).toEqual(['ask-1', 'ask-2'])
  })

  it('reports only resolutions recorded after the watermark', () => {
    const log = new Map([['ask-old', 1], ['ask-mid', 2], ['ask-new', 3]])
    expect(resolvedSince(log, 0)).toEqual(['ask-old', 'ask-mid', 'ask-new'])
    expect(resolvedSince(log, 2)).toEqual(['ask-new'])
    expect(resolvedSince(log, 3)).toEqual([])
  })

  it('reports a locally-held card the server no longer lists as pending', () => {
    const current = { a: { ask_id: 'ask-live' }, b: { ask_id: 'ask-dead' }, c: {} }
    expect(staleAskIds(current, [{ ask_id: 'ask-live' }])).toEqual(['ask-dead'])
    // Legacy card (no ask_id) is never reported stale.
    expect(staleAskIds(current, [])).toEqual(['ask-live', 'ask-dead'])
    expect(staleAskIds(undefined, [])).toEqual([])
  })

  it('drops stale cards and adds the server pending set', () => {
    const before = { x: { ask_id: 'ask-stale' } }
    const { drop, add } = reconcileQuestions(before, before, [
      { ask_id: 'ask-fresh', slot: ACTIVE, questions: [{ question: 'Which?' }] },
    ])
    expect(drop).toEqual(['ask-stale'])
    expect(add.map(q => q.ask_id)).toEqual(['ask-fresh'])
  })

  it('will not re-add a card the server still lists but this client already dropped', () => {
    // `ask-gone` was local before the fetch and vanished during it (a WS
    // resolution): re-adding it would resurrect a card whose submit can only 404.
    const before = { x: { ask_id: 'ask-gone' } }
    const { drop, add } = reconcileQuestions(before, {}, [
      { ask_id: 'ask-gone', slot: ACTIVE, questions: [{ question: 'Dead?' }] },
    ])
    // Still listed as pending, so it is not reported stale — but it is dead.
    expect(drop).toEqual([])
    expect(add).toEqual([])
  })

  it('skips a resolution observed for a card this client never held', () => {
    const { add } = reconcileQuestions({}, {}, [
      { ask_id: 'ask-never-held', slot: ACTIVE, questions: [{ question: 'Q' }] },
    ], ['ask-never-held'])
    expect(add).toEqual([])
  })

  it('refuses a pending entry with no slot or no questions', () => {
    const { add } = reconcileQuestions({}, {}, [
      { ask_id: 'ask-no-slot', questions: [{ question: 'Q' }] },
      { ask_id: 'ask-no-questions', slot: ACTIVE, questions: [] },
    ])
    expect(add).toEqual([])
  })
})
