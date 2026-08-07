/**
 * Auto-speak's streaming voice pipeline, pinned at the WebSocket hook:
 *
 * - segment-reset guard: `chat_segment` resets the spoken-length counter, and
 *   the turn-completion pass must not re-speak the whole reply from 0 — while
 *   a reply that never streamed is still spoken in full;
 * - ordered playback: queued voice chunks play strictly in arrival order, one
 *   at a time, never overlapping;
 * - voiceBusy: covers the whole pipeline (synthesis in flight, queued audio,
 *   playing audio), not just the playing span;
 * - barge-in: `voice-stop` halts the active clip, flushes the queue, aborts
 *   in-flight synthesis, and mutes chunks that arrive afterwards.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store } from '../store'
import { setActiveSlot, clearMessages } from '../store/chatSlice'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: true }),
    voiceSynthesize: vi.fn().mockResolvedValue({}),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []

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

  constructor() { WS_INSTANCES.push(this) }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

const AUDIO_INSTANCES: MockAudio[] = []

class MockAudio {
  src: string
  onended: (() => void) | null = null
  onerror: (() => void) | null = null
  pause = vi.fn()
  play = vi.fn().mockResolvedValue(undefined)

  constructor(src: string) {
    this.src = src
    AUDIO_INSTANCES.push(this)
  }
}

const SENTENCE = 'This is the first spoken sentence. '
/** Any third-arg shape enqueueSynth sends (the abort signal wrapper). */
const SYNTH_OPTS = expect.objectContaining({ signal: expect.anything() })

function voiceChunk(slot: string, index: number) {
  return { type: 'voice_chunk', data: { slot, index, sentence: `s${index}`, audio: btoa(`audio-${index}`) } }
}

describe('useWebSocket auto-speak voice pipeline', () => {
  let queryClient: QueryClient
  let urlCounter: number

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    AUDIO_INSTANCES.length = 0
    urlCounter = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('Audio', MockAudio)
    URL.createObjectURL = vi.fn(() => `blob:mock-${++urlCounter}`)
    URL.revokeObjectURL = vi.fn()
    // The hook reads streamed messages and the active slot off the singleton
    // store, so the Provider must hand it that same store.
    store.dispatch(setActiveSlot('slot-1'))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    store.dispatch(clearMessages())
    store.dispatch(setActiveSlot(null))
  })

  async function mount() {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    // Let the onopen voiceConfig() promise resolve so autoSpeak is cached.
    await act(async () => {})
    return { hook, ws }
  }

  it('does not re-speak the whole reply when chat_segment reset the counter', async () => {
    const { hook, ws } = await mount()

    // Stream a full sentence, then a segment boundary: the flush speaks the
    // sentence, the segment finalizes it and resets the spoken counter.
    act(() => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'slot-1', content: SENTENCE, seq: 1 } })
      ws.simulateMessage({ type: 'chat_segment', data: { slot: 'slot-1' } })
    })
    await act(async () => {})
    expect(api.voiceSynthesize).toHaveBeenCalledTimes(1)
    expect(api.voiceSynthesize).toHaveBeenCalledWith('slot-1', SENTENCE.trim(), SYNTH_OPTS)

    // Turn completion: everything streamed was already spoken, so nothing may
    // be synthesized again — slicing from the reset counter would repeat the
    // entire reply.
    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-1' } }) })
    await act(async () => {})
    expect(api.voiceSynthesize).toHaveBeenCalledTimes(1)

    hook.unmount()
  })

  it('still speaks a reply that never went through the streaming path', async () => {
    const { hook, ws } = await mount()

    // A short or non-streamed reply arrives as a plain assistant message: the
    // completion pass is its only chance to be spoken, and the counter is 0
    // because nothing streamed — that must not be mistaken for a segment reset.
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: 'slot-1', role: 'assistant', content: 'A reply that never streamed at all.', ts: '10.0' } })
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-1' } })
    })
    await act(async () => {})

    expect(api.voiceSynthesize).toHaveBeenCalledTimes(1)
    expect(api.voiceSynthesize).toHaveBeenCalledWith('slot-1', 'A reply that never streamed at all.', SYNTH_OPTS)

    hook.unmount()
  })

  it('plays queued chunks strictly in order, one at a time', async () => {
    const { hook, ws } = await mount()

    act(() => {
      ws.simulateMessage(voiceChunk('slot-1', 0))
      ws.simulateMessage(voiceChunk('slot-1', 1))
    })
    await act(async () => {})

    // The first clip is playing; the second waits its turn.
    expect(AUDIO_INSTANCES).toHaveLength(1)
    expect(AUDIO_INSTANCES[0].src).toBe('blob:mock-1')
    expect(store.getState().chat.voicePlaying).toBe(true)
    expect(store.getState().chat.voiceBusy).toBe(true)

    act(() => { AUDIO_INSTANCES[0].onended?.() })
    await act(async () => {})
    expect(AUDIO_INSTANCES).toHaveLength(2)
    expect(AUDIO_INSTANCES[1].src).toBe('blob:mock-2')

    act(() => { AUDIO_INSTANCES[1].onended?.() })
    await act(async () => {})
    expect(store.getState().chat.voicePlaying).toBe(false)
    expect(store.getState().chat.voiceBusy).toBe(false)

    hook.unmount()
  })

  it('keeps voiceBusy true through the synthesis-in-flight gap', async () => {
    // Nothing is queued or playing while the POST is pending — exactly the
    // gap the hands-free hold needs voiceBusy to cover.
    let resolveSynth: (v: object) => void = () => {}
    vi.mocked(api.voiceSynthesize).mockImplementationOnce(
      () => new Promise((res) => { resolveSynth = res }),
    )
    const { hook, ws } = await mount()

    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: 'slot-1', role: 'assistant', content: 'A reply that speaks after the turn.', ts: '11.0' } })
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-1' } })
    })
    await act(async () => {})
    expect(store.getState().chat.voiceBusy).toBe(true)
    expect(store.getState().chat.voicePlaying).toBe(false)

    await act(async () => { resolveSynth({}) })
    expect(store.getState().chat.voiceBusy).toBe(false)

    hook.unmount()
  })

  it('voice-stop barges in: pauses playback, flushes the queue, aborts synthesis, mutes stragglers', async () => {
    // A pending synthesis stands in for the next sentence still being
    // synthesized when the user barges in; like a real fetch it rejects when
    // its signal aborts.
    let capturedSignal: AbortSignal | undefined
    vi.mocked(api.voiceSynthesize).mockImplementationOnce(
      (_slot: string, _text: string, opts?: { signal?: AbortSignal }) => {
        capturedSignal = opts?.signal
        return new Promise((_res, rej) => {
          opts?.signal?.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')))
        })
      },
    )
    const { hook, ws } = await mount()

    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: 'slot-1', role: 'assistant', content: 'A reply the user is about to interrupt.', ts: '12.0' } })
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-1' } })
      ws.simulateMessage(voiceChunk('slot-1', 0))
      ws.simulateMessage(voiceChunk('slot-1', 1))
    })
    await act(async () => {})
    expect(AUDIO_INSTANCES).toHaveLength(1)
    expect(capturedSignal?.aborted).toBe(false)

    act(() => { window.dispatchEvent(new Event('voice-stop')) })
    await act(async () => {})

    expect(AUDIO_INSTANCES[0].pause).toHaveBeenCalled()
    expect(capturedSignal?.aborted).toBe(true)
    // The queued (never-played) clip is released, and the pipeline is idle.
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-2')
    expect(store.getState().chat.voicePlaying).toBe(false)
    expect(store.getState().chat.voiceBusy).toBe(false)

    // A chunk arriving after the barge-in is muted, not queued.
    act(() => { ws.simulateMessage(voiceChunk('slot-1', 2)) })
    await act(async () => {})
    expect(AUDIO_INSTANCES).toHaveLength(1)

    hook.unmount()
  })
})
