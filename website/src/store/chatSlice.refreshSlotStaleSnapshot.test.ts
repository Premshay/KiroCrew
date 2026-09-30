/** A `refreshSlot` snapshot must not overwrite a transcript the user changed
 *  while it was in flight.
 *
 *  `refreshSlot.fulfilled` REPLACES `messages` with the server page. A page the
 *  server built before a local change lands after it and erases that change:
 *  the user's just-sent bubble vanishes while the turn it started runs, a queued
 *  bubble vanishes before its delivery, and after stop -> edit -> resend the
 *  pre-edit tail (old prompt, `[Stopped]`) comes back until the next refresh.
 *  Nothing is lost server-side -- the next refresh (`chat_done`) repairs the
 *  view -- but for the rest of the turn the page shows the wrong conversation.
 *
 *  The mid-turn refresh is routine, not rare: the row-stall watchdog fires one
 *  after 100 s without a new row (any long tool call), and so does every
 *  reconnect. On a long transcript the fetch itself takes seconds, which is the
 *  window a send or an edit lands in.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

type Row = { role: string; content: string; cls: string; ts: string; meta?: Record<string, unknown> }

const row = (i: number, role = i % 2 === 0 ? 'user' : 'assistant'): Row => ({
  role,
  content: `m${i}`,
  cls: 'msg',
  ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString(),
  meta: { mid: `mid-${i}` },
})

/** The server's transcript as of the moment it builds the page. */
let HISTORY: Row[] = []
/** Fired once, inside the next `chatSlotDetail` call: a local write that lands
 *  after the thunk read the store and before its page is reduced. */
let DURING_FETCH: (() => void) | null = null

vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn((slot: string, limit?: number) => {
      const snapshot = [...HISTORY]
      if (DURING_FETCH) {
        const fire = DURING_FETCH
        DURING_FETCH = null
        fire()
      }
      const start = limit === undefined ? 0 : Math.max(0, snapshot.length - limit)
      return Promise.resolve({
        key: slot,
        messages: snapshot.slice(start),
        has_more: start > 0,
        next_before: start,
        total: snapshot.length,
        running: true,
        queue: [],
      })
    }),
  },
}))

import chatReducer, {
  appendMessage,
  appendQueuedMessage,
  refreshSlot,
  truncateAfterIndex,
} from './chatSlice'

const SLOT = 'slot-1'

function makeStore(messages: Row[]) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, activeSlot: SLOT, messages, slotHasMore: false } },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

const contents = (store: ReturnType<typeof makeStore>) =>
  store.getState().chat.messages.map((m: { content: string }) => m.content)

describe('refreshSlot never lands a snapshot older than a local transcript change', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    HISTORY = [row(0), row(1), row(2), row(3)]
    DURING_FETCH = null
  })

  it('keeps a user bubble sent while the refresh was in flight', async () => {
    const store = makeStore([...HISTORY])
    DURING_FETCH = () =>
      store.dispatch(appendMessage({
        role: 'user', content: 'just sent', cls: 'msg msg-u', ts: '', meta: { sendId: 's-1' },
      }))

    await store.dispatch(refreshSlot(SLOT) as never)

    expect(contents(store)).toContain('just sent')
  })

  it('keeps a queued bubble added while the refresh was in flight', async () => {
    const store = makeStore([...HISTORY])
    DURING_FETCH = () =>
      store.dispatch(appendQueuedMessage({
        slot: SLOT, content: 'queued behind the turn', ts: '', queueId: 'q-1',
      }))

    await store.dispatch(refreshSlot(SLOT) as never)

    expect(contents(store)).toContain('queued behind the turn')
  })

  it('does not bring back the pre-edit tail after stop -> edit -> resend', async () => {
    HISTORY = [row(0), row(1), { ...row(2), content: 'I merged 2905' }, row(3, 'system')]
    const store = makeStore([...HISTORY])
    DURING_FETCH = () => {
      store.dispatch(truncateAfterIndex(2))
      store.dispatch(appendMessage({
        role: 'user', content: 'I merged 2908', cls: 'msg msg-u', ts: '', meta: { sendId: 's-2' },
      }))
    }

    await store.dispatch(refreshSlot(SLOT) as never)

    expect(contents(store)).toContain('I merged 2908')
    expect(contents(store)).not.toContain('I merged 2905')
  })

  it('still applies a snapshot when nothing changed locally meanwhile', async () => {
    const store = makeStore([row(0), row(1)])

    await store.dispatch(refreshSlot(SLOT) as never)

    expect(contents(store)).toEqual(['m0', 'm1', 'm2', 'm3'])
  })
})
