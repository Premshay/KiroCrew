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

import { useAvailableModels, useAvailableModelsQuery } from '../hooks/useAvailableModels'

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

  it('does not fall back to the generic catalog for an unresolved crew', async () => {
    const { result } = renderHook(
      () => useAvailableModels({
        agent: { name: 'unresolved', runtime_policy: { model: 'managed' } },
        fallback: 'none',
      }),
      { wrapper: queryWrapper },
    )

    expect(result.current).toEqual([])
    expect(mocks.discover).not.toHaveBeenCalled()
    expect(mocks.generic).not.toHaveBeenCalled()
  })
  it('labels a composite-id agent catalog with the advertised model names', async () => {
    // dsh advertises its models as `["provider","model"]` route pairs. The id
    // is the wire value and stays `name`; the readable name the catalog sends
    // rides `label`, which is what a picker row renders.
    mocks.discover.mockResolvedValue({
      models: [
        { modelId: '["deepseek-official","deepseek-flash"]', name: 'DeepSeek-V41-Flash', description: '' },
        { modelId: '["deepseek-official","deepseek-v4-pro"]', name: 'DeepSeek-V4-Pro', description: 'Harder tasks.' },
      ],
      effort_levels: ['off', 'low', 'high', 'max'],
    })

    const { result } = renderHook(
      () => useAvailableModels({
        agent: { name: 'crew-deepseek-atlas', runtime_policy: { model: 'selectable' } },
      }),
      { wrapper: queryWrapper },
    )

    await waitFor(() =>
      expect(result.current.map(m => [m.name, m.label])).toEqual([
        ['auto', undefined],
        ['["deepseek-official","deepseek-flash"]', 'DeepSeek-V41-Flash'],
        ['["deepseek-official","deepseek-v4-pro"]', 'DeepSeek-V4-Pro'],
      ]),
    )
  })

  it('exposes a crew discovery failure without consulting another runtime catalog', async () => {
    mocks.discover.mockRejectedValue(new Error('Crew discovery unavailable'))
    const { result } = renderHook(
      () => useAvailableModelsQuery({
        agent: { name: 'crew-codex', runtime_policy: { model: 'selectable' } },
        fallback: 'none',
      }),
      { wrapper: queryWrapper },
    )
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.isDegraded).toBe(true)
    expect(result.current.data).toEqual([])
    expect(mocks.generic).not.toHaveBeenCalled()
  })

  it('surfaces the crew runtime effort selector for the composer to gate on', async () => {
    // The effort control is gated on this: a harness whose ids the frontend
    // family allowlist cannot parse (dsh's route pairs) still reports its own
    // selector, and dropping it here is what blanked the control.
    mocks.discover.mockResolvedValue({
      models: [{ modelId: '["deepseek-official","deepseek-flash"]', name: 'DeepSeek-V41-Flash', description: '' }],
      effort_levels: ['off', 'low', 'high', 'max'],
    })

    const { result } = renderHook(
      () => useAvailableModelsQuery({ agent: { name: 'crew-deepseek', runtime_policy: { model: 'selectable' } } }),
      { wrapper: queryWrapper },
    )

    await waitFor(() => expect(result.current.effortLevels).toEqual(['off', 'low', 'high', 'max']))
  })

  it('leaves the effort selector unknown for the generic catalog', async () => {
    mocks.generic.mockResolvedValue([{ modelId: 'claude-sonnet-5', name: 'Claude Sonnet 5', description: '' }])
    const { result } = renderHook(() => useAvailableModelsQuery(), { wrapper: queryWrapper })
    await waitFor(() => expect(result.current.data.length).toBeGreaterThan(0))
    expect(result.current.effortLevels).toBeUndefined()
  })

})
