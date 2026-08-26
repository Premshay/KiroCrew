import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  discover: vi.fn(),
  generic: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: { kirocrewAgentModels: mocks.discover },
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    fetchAvailableModels: mocks.generic,
    getContextWindow: () => 200_000,
  }),
}))

import { useAvailableModels } from '../hooks/useAvailableModels'

function queryWrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

describe('useAvailableModels — selectable crew', () => {
  beforeEach(() => {
    mocks.discover.mockReset()
    mocks.generic.mockReset()
  })

  it('uses the named Codex crew catalog instead of the generic Kiro catalog', async () => {
    mocks.discover.mockResolvedValue({
      models: [{ modelId: 'gpt-5.6-sol', name: 'GPT-5.6 Sol', description: 'Subscription' }],
      effort_levels: ['low', 'high'],
    })

    const { result } = renderHook(
      () => useAvailableModels({
        agent: { name: 'crew-codex', runtime_policy: { model: 'selectable' } },
      }),
      { wrapper: queryWrapper },
    )

    await waitFor(() => expect(result.current.map(model => model.name)).toEqual(['auto', 'gpt-5.6-sol']))
    expect(mocks.discover).toHaveBeenCalledWith('crew-codex')
    expect(mocks.generic).not.toHaveBeenCalled()
  })

  it('keeps the workspace alias when discovering an Atlas-scoped Claude crew', async () => {
    mocks.discover.mockResolvedValue({
      models: [{ modelId: 'claude-sonnet-5', name: 'Claude Sonnet 5', description: '' }],
      effort_levels: ['low', 'high'],
    })

    const { result } = renderHook(
      () => useAvailableModels({
        agent: { name: 'crew-claude-atlas', runtime_policy: { model: 'selectable' } },
      }),
      { wrapper: queryWrapper },
    )

    await waitFor(() => expect(result.current.map(model => model.name)).toEqual(['auto', 'claude-sonnet-5']))
    expect(mocks.discover).toHaveBeenCalledWith('crew-claude-atlas')
  })
})
