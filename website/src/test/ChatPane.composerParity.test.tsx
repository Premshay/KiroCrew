import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, renderHook, screen, fireEvent, waitFor, within } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* chat-core P3-b (#9775): ChatPane mounts the same ChatInput as ChatPage but
 * had no microphone — dictation arrived at ChatInput as 23 host-wired props the
 * pane never passed. Voice is now the root-mounted Voice atom: the pane wraps its
 * ChatInput in a `<Composer>` root and the mic appears with no voice props at
 * all. These tests pin that: the REAL ChatInput renders inside the pane, the mic
 * button exists, and pressing it reaches the shared engine.
 *
 * P3-d (#9808): the `@` file/folder menu reaches the pane the same way, through
 * the root's Mention atom, with no picker props on the pane's ChatInput. */

const engine = {
  recording: false, transcribing: false, sessionOwner: null as string | null, streamEnabled: false,
  toggle: vi.fn(), start: vi.fn().mockResolvedValue(undefined), stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
  error: null as string | null, level: 0, deviceLabel: '', deviceId: '', clearError: vi.fn(), partial: '',
  download: null, sampleRef: { current: {} }, switchDevice: vi.fn(), deviceSwitchIsLive: false,
}
let voiceOptions: { onPartial?: (text: string, slot: string | null) => void; onEndpoint?: () => void } = {}
vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: (_onText: unknown, options: typeof voiceOptions) => { voiceOptions = options; return engine },
  voiceInputSupported: true,
}))
vi.mock('../hooks/usePushToTalk', () => ({ usePushToTalk: () => ({ config: { enabled: false, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }, phase: 'idle', holding: false }) }))
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockImplementation((_q: string, project?: string) => Promise.resolve({
      root: project || '/repo',
      results: [
        { path: '/repo/src/widgets', name: 'widgets', size: 0, mtime: 1750000000, kind: 'dir' },
        { path: '/repo/src/main.ts', name: 'main.ts', size: 10, mtime: 1750000000, kind: 'file' },
      ],
    })),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
    chatSlotModel: vi.fn(),
    dashboardConfig: vi.fn().mockResolvedValue({ quick_send: false, preference_advisor_enabled: true }),
    planAction: vi.fn().mockResolvedValue({ ok: true }),
    sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' }),
    getPreferenceAdvice: vi.fn().mockResolvedValue({ id: 'advice', current: '', model: 'suggested', reason: 'similar_preferences' }),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'
import { useNewSessionPicks, useSessionChoices } from '../pages/chat/page/sessionPicks'
import { store } from '../store'

const SLOT = 'chat-1-parity'

describe('new session model selection', () => {
  it('keeps explicit selections for the next session', async () => {
    const { result } = renderHook(() => {
      const picks = useNewSessionPicks()
      const choices = useSessionChoices({
        dispatch: store.dispatch, queryClient: new QueryClient(), activeSlot: null,
        codexPairModels: false, setModelDropdown: vi.fn(), modelPickerReturnsFocusRef: { current: false },
        setPendingAgent: picks.setPendingAgent, setPendingModel: picks.setPendingModel, setPendingProject: picks.setPendingProject,
      })
      return { picks, choices }
    })
    expect(result.current.picks.pendingModelRef.current).toBe('')
    await act(async () => { expect(await result.current.choices.switchModel('auto')).toBe('auto') })
    expect(result.current.picks.pendingModelRef.current).toBe('auto')
  })

  it('does not confirm a model rejected by the server', async () => {
    vi.mocked(api.chatSlotModel).mockRejectedValueOnce(new Error('refused'))
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { result } = renderHook(() => useSessionChoices({
      dispatch: store.dispatch, queryClient: new QueryClient(), activeSlot: SLOT,
      codexPairModels: false, setModelDropdown: vi.fn(), modelPickerReturnsFocusRef: { current: false },
      setPendingAgent: vi.fn(), setPendingModel: vi.fn(), setPendingProject: vi.fn(),
    }))
    try {
      await act(async () => { expect(await result.current.switchModel('chosen')).toBeUndefined() })
      expect(api.chatSlotModel).toHaveBeenCalledWith(SLOT, 'chosen')
    } finally {
      log.mockRestore()
    }
  })
})

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, project: '/repo', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

async function renderPane(streaming = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // The STT config is what lets the mic START (an unloaded config opens the
  // setup modal instead); seed it so the press below reaches the engine.
  qc.setQueryData(['sttConfig'], { enabled: true, available: true, streaming, dictation_panel: true, provider: 'local' })
  engine.streamEnabled = streaming
  const store = makeStore(SLOT)
  await act(async () => {
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  await waitFor(() => expect(screen.getAllByRole('textbox').length).toBeGreaterThan(0))
  return store
}

const mic = () => screen.getByRole('button', { name: /^Voice input/ })

beforeEach(() => {
  localStorage.clear()
  engine.recording = false
  engine.start.mockClear().mockImplementation(async () => { engine.recording = true })
  engine.stop.mockImplementation(() => { engine.recording = false })
  vi.mocked(api.sendChat).mockClear()
  vi.mocked(api.getPreferenceAdvice).mockClear()
})

describe('ChatPane composer parity (chat-core P3-b): voice through the Composer root', () => {
  it('disarms a first manual Send before delayed advice can receive another partial', async () => {
    let resolve!: (value: { reason: string }) => void
    vi.mocked(api.getPreferenceAdvice).mockImplementationOnce(() => new Promise(r => { resolve = r }))
    await renderPane(true)
    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.click(mic()) })
    await act(async () => { voiceOptions.onPartial?.('first utterance', SLOT) })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' }) })
    await waitFor(() => expect(resolve).toBeDefined())
    await act(async () => { voiceOptions.onPartial?.('late utterance', SLOT); resolve({ reason: 'no_examples' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    expect(vi.mocked(api.sendChat).mock.calls[0][0]).toContain('first utterance')
    expect(vi.mocked(api.sendChat).mock.calls[0][0]).not.toContain('late utterance')
    expect(vi.mocked(api.sendChat).mock.calls[0][1]).toBe(SLOT)
    expect(input.value).toBe('')
  })

  it('delivers the first semantic voice endpoint without querying manual advice', async () => {
    await renderPane(true)
    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.click(mic()) })
    expect(typeof voiceOptions.onEndpoint).toBe('function')
    await act(async () => { voiceOptions.onPartial?.('classify these rules', SLOT) })
    expect(input.value).toBe('classify these rules')
    await act(async () => { voiceOptions.onEndpoint?.() })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    expect(api.getPreferenceAdvice).not.toHaveBeenCalled()
    expect(input.value).toBe('')
    await act(async () => { voiceOptions.onPartial?.('late transcript', SLOT) })
    expect(input.value).toBe('')
  })

  it('renders the microphone with no voice props on the pane', async () => {
    await renderPane()
    expect(mic()).toBeTruthy()
  })

  it('pressing the mic starts the shared engine (the atom, not a dead prop)', async () => {
    await renderPane()
    await act(async () => { fireEvent.click(mic()) })
    await waitFor(() => expect(engine.start).toHaveBeenCalledTimes(1))
  })
})

describe('ChatPane composer parity (chat-core P3-d): @ mentions through the Composer root', { timeout: 15_000 }, () => {
  const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
  /** Type an @-token and wait out the search debounce for the row named `name`. */
  async function search(query: string, name: string) {
    fireEvent.change(box(), { target: { value: query } })
    return screen.findByText(name, undefined, { timeout: 3000 })
  }

  it('a picked file stages a chip whose remove takes its @token out of the text', async () => {
    await renderPane()
    const row = await search('@mai', 'main.ts')
    // A pane-width menu cuts each row's folder from the start, so the end that
    // tells sibling rows apart stays in view.
    const folders = screen.getAllByTestId('file-picker-row-folder')
    expect(folders.map(f => [f.textContent, f.getAttribute('dir')])).toEqual([['src/', 'rtl'], ['src/', 'rtl']])
    fireEvent.mouseDown(row)
    await waitFor(() => expect(box().value).toBe('@src/main.ts '))
    fireEvent.change(box(), { target: { value: 'see @src/main.ts now' } })
    const chip = screen.getByRole('group', { name: '/repo/src/main.ts' })
    fireEvent.click(within(chip).getByRole('button', { name: 'Remove' }))
    expect(screen.queryByRole('group', { name: '/repo/src/main.ts' })).toBeNull()
    expect(box().value).toBe('see now')
  })

  it('a picked folder serializes against the pane project on send', async () => {
    vi.mocked(api.getPreferenceAdvice).mockResolvedValueOnce({
      id: '', current: '', model: '', reason: 'disabled',
    })
    await renderPane()
    fireEvent.mouseDown(await search('@wid', 'widgets/'))
    await screen.findByLabelText('Remove folder')
    fireEvent.change(box(), { target: { value: `${box().value}summarize it` } })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wire, , , , meta] = vi.mocked(api.sendChat).mock.calls[0]
    expect(wire).toContain('[attached_dir 1] /repo/src/widgets')
    expect(wire).not.toContain('@src/widgets/')
    expect(meta?.dirs).toEqual(['/repo/src/widgets'])
  })
})
