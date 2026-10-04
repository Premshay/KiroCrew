import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import RuntimeSelector from '../components/RuntimeSelector'
import dashboardReducer from '../store/dashboardSlice'
import { api } from '../api/client'

afterEach(() => vi.restoreAllMocks())

function mount(running = false, slot = 'vernier') {
  const store = configureStore({ reducer: { dashboard: dashboardReducer } })
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = (key: string) => <Provider store={store}><QueryClientProvider client={cache}><RuntimeSelector slot={key} running={running} /></QueryClientProvider></Provider>
  const { rerender } = render(view(slot))
  return (next: string) => rerender(view(next))
}

const choices = {
  runtime_agent: '',
  effective_runtime_agent: '',
  choices: [
    { name: 'codex', label: 'Codex', supported: true, reason: '' },
    { name: 'deepseek', label: 'DeepSeek', supported: false, reason: 'Capabilities unverified' },
  ],
}

describe('RuntimeSelector', () => {
  it('changes execution independently of the member identity', async () => {
    vi.spyOn(api, 'chatSlotRuntimes').mockResolvedValue(choices)
    const runtime = vi.spyOn(api, 'chatSlotRuntime').mockResolvedValue({ ok: true, runtime_agent: 'codex', model: '', reasoning_effort: '' })
    const identity = vi.spyOn(api, 'chatSlotAgent')
    mount()
    await waitFor(() => expect(screen.getByRole('combobox')).not.toBeDisabled())
    fireEvent.click(await screen.findByRole('combobox'))
    expect(await screen.findByRole('option', { name: 'DeepSeek — Capabilities unverified' })).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(await screen.findByRole('option', { name: 'Codex' }))
    await waitFor(() => expect(runtime).toHaveBeenCalledWith('vernier', 'codex'))
    expect(identity).not.toHaveBeenCalled()
  })

  it('shows the backend already in force instead of a bare Default', async () => {
    // An unset runtime_agent is not "no backend": the conversation runs on the
    // seat its agent is configured for, and the picker must say which.
    vi.spyOn(api, 'chatSlotRuntimes').mockResolvedValue({ ...choices, effective_runtime_agent: 'codex' })
    mount()
    await waitFor(() => expect(screen.getByRole('combobox')).toHaveTextContent('Codex'))
  })

  it('disables switching during a turn', async () => {
    vi.spyOn(api, 'chatSlotRuntimes').mockResolvedValue(choices)
    mount(true)
    expect(await screen.findByRole('combobox')).toBeDisabled()
  })

  it('keeps a failed switch on its own conversation', async () => {
    // One selector stays mounted while the active conversation changes; the
    // error from one conversation's refused switch must not follow the user.
    vi.spyOn(api, 'chatSlotRuntimes').mockResolvedValue(choices)
    vi.spyOn(api, 'chatSlotRuntime').mockRejectedValue(new Error('session changed during execution switch'))
    const show = mount()
    await waitFor(() => expect(screen.getByRole('combobox')).not.toBeDisabled())
    fireEvent.click(await screen.findByRole('combobox'))
    fireEvent.click(await screen.findByRole('option', { name: 'Codex' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('session changed during execution switch')
    show('derrick')
    await waitFor(() => expect(screen.getByRole('combobox')).not.toBeDisabled())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('reports runtime discovery failures', async () => {
    vi.spyOn(api, 'chatSlotRuntimes').mockRejectedValue(new Error('Runtime catalog unavailable'))
    mount()
    expect(await screen.findByRole('alert')).toHaveTextContent('Runtime catalog unavailable')
  })

  it('keeps a labelled disabled control visible when the catalog is empty', async () => {
    vi.spyOn(api, 'chatSlotRuntimes').mockResolvedValue({ ...choices, choices: [] })
    mount()
    expect(await screen.findByRole('combobox', { name: 'Backend' })).toBeDisabled()
    expect(screen.getByText('Backend')).toBeVisible()
  })
})
