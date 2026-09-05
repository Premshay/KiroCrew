import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const settings = vi.fn()
const putSettings = vi.fn()

vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: {
    settings: (...args: unknown[]) => settings(...args),
    putSettings: (...args: unknown[]) => putSettings(...args),
  },
}))

const SettingsView = (await import('../apps/code-review-sage/views/SettingsView')).default

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <SettingsView />
    </QueryClientProvider>,
  )
}

function response(overrides: Record<string, unknown> = {}) {
  return {
    settings: { model: null, effort: 'high', active_namespaces: ['default'], max_concurrent: 3 },
    models: ['gpt-5.3-codex'],
    efforts: ['low', 'high'],
    namespaces: ['default'],
    max_concurrent_max: 30,
    reviewer: {
      engine: 'kiro-cli', provider: 'acp', agent: 'code-review-sage-reviewer',
      model: 'gpt-5.3-codex', model_source: 'agent-default',
      model_override_supported: true, effort_override_supported: true,
    },
    ...overrides,
  }
}

describe('Code Review Sage settings', () => {
  beforeEach(() => {
    settings.mockReset()
    putSettings.mockReset()
  })

  it('separates the active reviewer binding from the optional override controls', async () => {
    settings.mockResolvedValue(response())
    mount()

    expect(await screen.findByTestId('sage-reviewer-binding')).toHaveTextContent(
      'kiro-cli / acp / code-review-sage-reviewer · gpt-5.3-codex',
    )
    expect(screen.getByRole('combobox', { name: /review model/i })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /reasoning effort/i })).toBeInTheDocument()
  })

  it('does not expose controls the resolved runtime cannot honor', async () => {
    settings.mockResolvedValue(response({
      models: [], efforts: [],
      reviewer: {
        engine: 'kiro-cli', provider: 'acp', agent: 'local-reviewer', model: 'local',
        model_source: 'agent-default', model_override_supported: false,
        effort_override_supported: false,
      },
    }))
    mount()

    await waitFor(() => expect(screen.getByTestId('sage-reviewer-binding')).toHaveTextContent('local'))
    expect(screen.queryByRole('combobox', { name: /review model/i })).toBeNull()
    expect(screen.queryByRole('combobox', { name: /reasoning effort/i })).toBeNull()
    expect(screen.getByText(/system default/i)).toBeInTheDocument()
  })
})
