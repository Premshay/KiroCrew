import React from 'react'
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { store } from '../../store'
import { usePreferenceAdvisor } from './usePreferenceAdvisor'
import { createComposerDraftStore } from '../../chat-core/composer/draftStore'

function Wrapper({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(() => new QueryClient({ defaultOptions: { mutations: { retry: false } } }))
  return <Provider store={store}><QueryClientProvider client={client}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider></Provider>
}

const suggestion = { id: 'advice-1', model: 'smaller', current: 'current', budget: 'fast' as const, reason: 'similar_preferences', examples: ['a', 'b'] }
const options = { enabled: true, slot: 'test-advice', model: 'current', eligible: true, models: ['current', 'smaller'], readDraft: () => 'classify rules', openModelPicker: () => {}, applyModel: async (model: string) => (await api.chatSlotModel('test-advice', model)).model ?? model }

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

describe('task-start preference adviser', () => {
  it('does not query advice without explicit enablement', async () => {
    const get = vi.spyOn(api, 'getPreferenceAdvice')
    const { result } = renderHook(() => usePreferenceAdvisor({ ...options, enabled: false }), { wrapper: Wrapper })
    expect(await result.current.beforeSend('classify rules')).toBe(true)
    expect(get).not.toHaveBeenCalled()
  })
  it.each(['advice', 'feedback'])('bounds the %s transport and aborts a stalled fetch', async kind => {
    vi.useFakeTimers()
    let signal!: AbortSignal
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => {
      signal = init!.signal!
      return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }))
    })
    const response = (kind === 'advice' ? api.getPreferenceAdvice('s', 'task', []) : api.sendPreferenceFeedback('id', 'keep', 'current')).catch(error => error)
    await vi.advanceTimersByTimeAsync(5001)
    expect((await response).name).toBe('TimeoutError')
    expect(signal.aborted).toBe(true)
  })
  it('leaves ordinary sending available to a non-owner without requesting feedback', async () => {
    vi.spyOn(api, 'getPreferenceAdvice').mockRejectedValue(
      new ApiError(403, 'dashboard owner required', JSON.stringify({ code: 'dashboard_owner_required' })),
    )
    const feedback = vi.spyOn(api, 'sendPreferenceFeedback')
    const { result } = renderHook(() => usePreferenceAdvisor(options), { wrapper: Wrapper })
    await act(async () => { expect(await result.current.beforeSend('classify rules')).toBe(true) })
    expect(feedback).not.toHaveBeenCalled()
    expect(result.current.card).toBeNull()
  })

  it.each([
    new ApiError(403, 'sign in again', '', true),
    new ApiError(503, 'service unavailable'),
  ])('keeps other advice failures visible and leaves the draft unsent: %s', async error => {
    vi.spyOn(api, 'getPreferenceAdvice').mockRejectedValue(error)
    let current: ReturnType<typeof usePreferenceAdvisor>
    function Harness() { current = usePreferenceAdvisor(options); return current.card }
    render(<Harness />, { wrapper: Wrapper })
    await act(async () => { expect(await current.beforeSend('classify rules')).toBe(false) })
    await waitFor(() => expect(screen.getByText(error.message)).toBeInTheDocument())
  })

  it('passes through when no example supports a recommendation', async () => {
    vi.spyOn(api, 'getPreferenceAdvice').mockResolvedValue({ reason: 'no_examples' })
    const { result } = renderHook(() => usePreferenceAdvisor(options), { wrapper: Wrapper })
    let allowed = false
    await act(async () => { allowed = await result.current.beforeSend('classify rules') })
    expect(allowed).toBe(true)
  })

  it('does not consult preferences for continuing or restricted sessions', async () => {
    const get = vi.spyOn(api, 'getPreferenceAdvice')
    const { result } = renderHook(() => usePreferenceAdvisor({ ...options, eligible: false }), { wrapper: Wrapper })
    expect(await result.current.beforeSend('yes')).toBe(true)
    expect(get).not.toHaveBeenCalled()
  })

  it('keeps the draft gated until an explicit choice and never treats silence as feedback', async () => {
    vi.spyOn(api, 'getPreferenceAdvice').mockResolvedValue(suggestion)
    const feedback = vi.spyOn(api, 'sendPreferenceFeedback').mockResolvedValue({ ok: true })
    let current: ReturnType<typeof usePreferenceAdvisor>
    function Harness() { current = usePreferenceAdvisor(options); return current.card }
    render(<Harness />, { wrapper: Wrapper })
    let allowed = true
    await act(async () => { allowed = await current.beforeSend('classify rules') })
    expect(allowed).toBe(false)
    expect(feedback).not.toHaveBeenCalled()
    expect(screen.getByText('Suggested for this task: smaller')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Keep current' }))
    await waitFor(() => expect(feedback).toHaveBeenCalledWith('advice-1', 'keep', 'current', expect.any(AbortSignal)))
    await waitFor(() => expect(screen.getByText('Preference saved. Press Send when ready.')).toBeInTheDocument())
    expect(await current!.beforeSend('classify rules')).toBe(true)
  })

  it('does not accept a result for a draft edited while retrieval was running', async () => {
    let resolve!: (value: typeof suggestion) => void
    vi.spyOn(api, 'getPreferenceAdvice').mockImplementation(() => new Promise(r => { resolve = r }))
    let draft = 'classify rules'
    const { result } = renderHook(() => usePreferenceAdvisor({ ...options, readDraft: () => draft }), { wrapper: Wrapper })
    let request!: Promise<boolean>
    act(() => { request = result.current.beforeSend(draft) })
    await waitFor(() => expect(resolve).toBeDefined())
    draft = 'different task'
    await act(async () => { resolve(suggestion); expect(await request).toBe(false) })
  })

  it('a manual model change wins over an open recommendation', async () => {
    vi.spyOn(api, 'getPreferenceAdvice').mockResolvedValue(suggestion)
    const { result, rerender } = renderHook(({ model }) => usePreferenceAdvisor({ ...options, model }), { initialProps: { model: 'current' }, wrapper: Wrapper })
    await act(async () => { await result.current.beforeSend('classify rules') })
    rerender({ model: 'manually-chosen' })
    let allowed = false
    await act(async () => { allowed = await result.current.beforeSend('classify rules') })
    expect(allowed).toBe(true)
  })

  it('never records acceptance when applying the suggested model fails', async () => {
    vi.spyOn(api, 'getPreferenceAdvice').mockResolvedValue(suggestion)
    vi.spyOn(api, 'chatSlotModel').mockRejectedValue(new Error('Model unavailable'))
    const feedback = vi.spyOn(api, 'sendPreferenceFeedback').mockResolvedValue({ ok: true })
    let current: ReturnType<typeof usePreferenceAdvisor>
    function Harness() { current = usePreferenceAdvisor(options); return current.card }
    render(<Harness />, { wrapper: Wrapper })
    await act(async () => { await current.beforeSend('classify rules') })
    fireEvent.click(screen.getByRole('button', { name: 'Use suggested' }))
    await waitFor(() => expect(screen.getByText('Model unavailable')).toBeInTheDocument())
    expect(feedback).not.toHaveBeenCalled()
    expect(await current!.beforeSend('classify rules')).toBe(false)
  })

  it('allows bypass during retrieval and discards a late recommendation', async () => {
    let resolve!: (value: typeof suggestion) => void
    let signal!: AbortSignal
    vi.spyOn(api, 'getPreferenceAdvice').mockImplementation((_slot, _task, _models, abort) => {
      signal = abort!
      return new Promise(r => { resolve = r })
    })
    let current: ReturnType<typeof usePreferenceAdvisor>
    function Harness() { current = usePreferenceAdvisor(options); return current.card }
    render(<Harness />, { wrapper: Wrapper })
    let request!: Promise<boolean>
    act(() => { request = current.beforeSend('classify rules') })
    fireEvent.click(await screen.findByRole('button', { name: 'Continue without advice' }))
    expect(signal.aborted).toBe(true)
    expect(await current!.beforeSend('classify rules')).toBe(true)
    await act(async () => { resolve(suggestion); expect(await request).toBe(false) })
    expect(screen.queryByText('Suggested for this task: smaller')).not.toBeInTheDocument()
  })

  it.each(['draft', 'model'])('invalidates an open card immediately on a changed %s', async field => {
    vi.spyOn(api, 'getPreferenceAdvice').mockResolvedValue(suggestion)
    const pick = vi.spyOn(api, 'chatSlotModel')
    let current: ReturnType<typeof usePreferenceAdvisor>
    function Harness({ model, draft }: { model: string; draft: string }) {
      current = usePreferenceAdvisor({ ...options, model, readDraft: () => draft })
      return current.card
    }
    const { rerender } = render(<Harness model="current" draft="classify rules" />, { wrapper: Wrapper })
    await act(async () => { await current.beforeSend('classify rules') })
    rerender(<Harness model={field === 'model' ? 'manual' : 'current'} draft={field === 'draft' ? 'new task' : 'classify rules'} />)
    expect(screen.queryByRole('button', { name: 'Use suggested' })).not.toBeInTheDocument()
    expect(pick).not.toHaveBeenCalled()
  })

  it('subscribes to main-composer draft validity without waiting for a host render', async () => {
    const draft = createComposerDraftStore('classify rules')
    vi.spyOn(api, 'getPreferenceAdvice').mockResolvedValue(suggestion)
    let current: ReturnType<typeof usePreferenceAdvisor>
    function Harness() {
      current = usePreferenceAdvisor({ ...options, readDraft: draft.get, subscribeDraft: draft.subscribe })
      return current.card
    }
    render(<Harness />, { wrapper: Wrapper })
    await act(async () => { await current.beforeSend(draft.get()) })
    expect(screen.getByRole('button', { name: 'Use suggested' })).toBeInTheDocument()
    act(() => { draft.set('changed task') })
    expect(screen.queryByRole('button', { name: 'Use suggested' })).not.toBeInTheDocument()
    act(() => { draft.set('classify rules') })
    expect(screen.queryByRole('button', { name: 'Use suggested' })).not.toBeInTheDocument()
  })

  it.each(['broadcast-first', 'response-first'])('records a picker choice with %s delivery', async order => {
    vi.spyOn(api, 'getPreferenceAdvice').mockResolvedValue(suggestion)
    const feedback = vi.spyOn(api, 'sendPreferenceFeedback').mockResolvedValue({ ok: true })
    const openModelPicker = vi.fn()
    let current: ReturnType<typeof usePreferenceAdvisor>
    function Harness({ model }: { model: string }) {
      current = usePreferenceAdvisor({ ...options, model, openModelPicker })
      return current.card
    }
    const { rerender } = render(<Harness model="current" />, { wrapper: Wrapper })
    await act(async () => { await current.beforeSend('classify rules') })
    fireEvent.click(screen.getByRole('button', { name: 'Choose another' }))
    expect(openModelPicker).toHaveBeenCalledOnce()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    let finish!: (model?: string) => void
    act(() => { finish = current.beginModelPick('manual') })
    if (order === 'broadcast-first') rerender(<Harness model="manual" />)
    await act(async () => { finish('manual') })
    if (order === 'response-first') rerender(<Harness model="manual" />)
    await waitFor(() => expect(feedback).toHaveBeenCalledWith('advice-1', 'choose', 'manual', expect.any(AbortSignal)))
  })

  it('retries failed feedback without reapplying the model or allowing Send', async () => {
    vi.spyOn(api, 'getPreferenceAdvice').mockResolvedValue(suggestion)
    const feedback = vi.spyOn(api, 'sendPreferenceFeedback').mockRejectedValueOnce(new Error('Write failed')).mockResolvedValue({ ok: true })
    let current: ReturnType<typeof usePreferenceAdvisor>
    let setModel!: (model: string) => void
    function Harness() {
      const [model, update] = React.useState('current')
      setModel = update
      current = usePreferenceAdvisor({ ...options, model })
      return current.card
    }
    const pick = vi.spyOn(api, 'chatSlotModel').mockImplementation(async () => { setModel('smaller'); return { model: 'smaller' } })
    render(<Harness />, { wrapper: Wrapper })
    await act(async () => { await current.beforeSend('classify rules') })
    fireEvent.click(screen.getByRole('button', { name: 'Use suggested' }))
    await screen.findByText('Write failed')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(await current!.beforeSend('classify rules')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByText('Preference saved. Press Send when ready.')
    expect(pick).toHaveBeenCalledOnce()
    expect(feedback).toHaveBeenCalledTimes(2)
    expect(await current!.beforeSend('classify rules')).toBe(true)
  })

  it('lets the owner skip a pending feedback write and ignores its late completion', async () => {
    vi.spyOn(api, 'getPreferenceAdvice').mockResolvedValue(suggestion)
    let resolve!: (result: { ok: boolean }) => void
    let signal!: AbortSignal
    vi.spyOn(api, 'sendPreferenceFeedback').mockImplementation((_id, _choice, _model, abort) => {
      signal = abort!
      return new Promise(r => { resolve = r })
    })
    let current: ReturnType<typeof usePreferenceAdvisor>
    function Harness() { current = usePreferenceAdvisor(options); return current.card }
    render(<Harness />, { wrapper: Wrapper })
    await act(async () => { await current.beforeSend('classify rules') })
    fireEvent.click(screen.getByRole('button', { name: 'Keep current' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Continue without advice' }))
    expect(signal.aborted).toBe(true)
    expect(await current!.beforeSend('classify rules')).toBe(true)
    await act(async () => { resolve({ ok: true }) })
    expect(screen.queryByText('Preference saved. Press Send when ready.')).not.toBeInTheDocument()
  })

  it('waits for the owning picker transaction and does not record a refused effort', async () => {
    vi.spyOn(api, 'getPreferenceAdvice').mockResolvedValue(suggestion)
    const rawPick = vi.spyOn(api, 'chatSlotModel')
    const feedback = vi.spyOn(api, 'sendPreferenceFeedback')
    let reject!: (error: Error) => void
    const applyModel = vi.fn(() => new Promise<string>((_resolve, fail) => { reject = fail }))
    let current: ReturnType<typeof usePreferenceAdvisor>
    function Harness() { current = usePreferenceAdvisor({ ...options, applyModel }); return current.card }
    render(<Harness />, { wrapper: Wrapper })
    await act(async () => { await current.beforeSend('classify rules') })
    fireEvent.click(screen.getByRole('button', { name: 'Use suggested' }))
    await waitFor(() => expect(applyModel).toHaveBeenCalledWith('smaller'))
    expect(rawPick).not.toHaveBeenCalled()
    expect(feedback).not.toHaveBeenCalled()
    await act(async () => { reject(new Error('Effort refused')) })
    await screen.findByText('Effort refused')
    expect(rawPick).not.toHaveBeenCalled()
    expect(feedback).not.toHaveBeenCalled()
  })
})
