/** The row-delivery watchdog: a turn that is still running while its transcript
 *  has stopped moving must be re-hydrated from the server. Nothing else on the
 *  client can bring it back -- a live socket never trips the reconnect path,
 *  and the health probe only polls while `dashboard.connected === false`. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import {
  useWebSocket,
  ROW_STALL_MS,
  ROW_STALL_TICK_MS,
} from '../hooks/useWebSocket'
import { api } from '../api/client'
import chatReducer from '../store/chatSlice'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi
      .fn()
      .mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()
}

describe('row-delivery stall watchdog', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.useFakeTimers()
    vi.stubGlobal('WebSocket', MockWebSocket)
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' },
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(
      Provider,
      { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  const detailCalls = () => vi.mocked(api.chatSlotDetail).mock.calls.length

  it('re-hydrates the active slot when a running turn stops delivering rows', async () => {
    testStore.dispatch({ type: 'chat/startRemoteTurn', payload: 'chat-active' })
    expect(testStore.getState().chat.slotRunning).toBe(true)

    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS * 2)
    })
    const before = detailCalls()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS * 2)
    })

    expect(detailCalls()).toBeGreaterThan(before)
    unmount()
  })

  it('leaves an idle slot alone, and asks the server nothing, however long its rows sit still', async () => {
    /* The watchdog's steady state must cost nothing: its tick reads app state
     * and issues no request at all unless a slot it believes is RUNNING has
     * stopped moving. A slot this client believes idle is not its business --
     * re-checking that belief needs a server round trip per visible tab, which
     * this fix deliberately does not charge. */
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS * 2)
    })
    const before = detailCalls()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_MS * 3)
    })

    expect(detailCalls()).toBe(before)
    expect(api.chatSlots).not.toHaveBeenCalled()
    unmount()
  })
})
