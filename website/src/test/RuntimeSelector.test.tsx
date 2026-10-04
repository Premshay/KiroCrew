import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import RuntimeSelector from '../components/RuntimeSelector'
import dashboardReducer from '../store/dashboardSlice'
import { api } from '../api/client'

afterEach(() => vi.restoreAllMocks())

function mount(running = false) {
  const store = configureStore({ reducer: { dashboard: dashboardReducer } })
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<Provider store={store}><QueryClientProvider client={cache}><RuntimeSelector slot="vernier" running={running} /></QueryClientProvider></Provider>)
}

const choices = {
  runtime_agent: '',
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

  it('disables switching during a turn', async () => {
    vi.spyOn(api, 'chatSlotRuntimes').mockResolvedValue(choices)
    mount(true)
    expect(await screen.findByRole('combobox')).toBeDisabled()
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
