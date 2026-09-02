// Scoping a namespace from Settings: the migration warning, the three scope
// states, and what reaches the settings PUT.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
    settings: {
      model: null,
      effort: 'high',
      active_namespaces: ['default', 'service-rules'],
      namespace_bindings: {},
      max_concurrent: 3,
    },
    models: [],
    efforts: [],
    namespaces: ['default', 'service-rules'],
    pinned_repos: [{ owner: 'acme', repo: 'service' }],
    max_concurrent_max: 30,
    reviewer: null,
    ...overrides,
  }
}

/** Radix renders a `<button role="combobox">`; its options exist only while open. */
async function chooseScope(namespace: string, option: RegExp) {
  fireEvent.click(screen.getByRole('combobox', { name: `Rule scope for ${namespace}` }))
  fireEvent.click(await screen.findByRole('option', { name: option }))
}

describe('Code Review Sage namespace scope', () => {
  beforeEach(() => {
    settings.mockReset()
    putSettings.mockReset()
    putSettings.mockResolvedValue({ ok: true })
  })

  it('warns that active namespaces with no binding still apply everywhere', async () => {
    settings.mockResolvedValue(response())
    mount()

    const warning = await screen.findByTestId('sage-unscoped-namespaces')
    expect(warning).toHaveTextContent('default, service-rules')
  })

  it('drops the warning for namespaces that are already scoped', async () => {
    settings.mockResolvedValue(response({
      settings: {
        model: null,
        effort: 'high',
        active_namespaces: ['default', 'service-rules'],
        namespace_bindings: {
          default: { scope: 'global' },
          'service-rules': {
            scope: 'repository',
            repository: {
              provider: 'github', host: 'github.com', owner: 'acme', repository: 'service',
            },
          },
        },
        max_concurrent: 3,
      },
    }))
    mount()

    await screen.findByTestId('sage-namespace-scope-default')
    expect(screen.queryByTestId('sage-unscoped-namespaces')).toBeNull()
    expect(screen.getByRole('combobox', { name: 'Repository for service-rules' }))
      .toHaveTextContent('github.com/acme/service')
  })

  it('writes an explicit global binding without touching the other namespaces', async () => {
    settings.mockResolvedValue(response({
      settings: {
        model: null,
        effort: 'high',
        active_namespaces: ['default', 'service-rules'],
        namespace_bindings: { 'service-rules': { scope: 'global' } },
        max_concurrent: 3,
      },
    }))
    mount()
    await screen.findByTestId('sage-namespace-scope-default')

    await chooseScope('default', /all repositories/i)

    await waitFor(() => expect(putSettings).toHaveBeenCalledWith({
      namespace_bindings: {
        'service-rules': { scope: 'global' },
        default: { scope: 'global' },
      },
    }))
  })

  it('offers only pinned repositories when scoping a namespace', async () => {
    settings.mockResolvedValue(response())
    mount()
    await screen.findByTestId('sage-namespace-scope-default')

    await chooseScope('service-rules', /one repository/i)
    fireEvent.click(screen.getByRole('combobox', { name: 'Repository for service-rules' }))
    expect(await screen.findByRole('option', { name: 'github.com/acme/service' })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Repository for service-rules' })).toBeNull()
  })

  it('sends the selected pinned repository identity the backend stores', async () => {
    settings.mockResolvedValue(response())
    mount()
    await screen.findByTestId('sage-namespace-scope-default')

    await chooseScope('service-rules', /one repository/i)
    fireEvent.click(screen.getByRole('combobox', { name: 'Repository for service-rules' }))
    fireEvent.click(await screen.findByRole('option', { name: 'github.com/acme/service' }))

    await waitFor(() => expect(putSettings).toHaveBeenCalledWith({
      namespace_bindings: {
        'service-rules': {
          scope: 'repository',
          repository: {
            provider: 'github', host: 'github.com', owner: 'acme', repository: 'service',
          },
        },
      },
    }))
  })

  it('shows an unavailable saved binding without widening it', async () => {
    settings.mockResolvedValue(response({
      settings: {
        model: null,
        effort: 'high',
        active_namespaces: ['default'],
        namespace_bindings: {
          default: {
            scope: 'repository',
            repository: {
              provider: 'github', host: 'github.com', owner: 'former', repository: 'repo',
            },
          },
        },
        max_concurrent: 3,
      },
    }))
    mount()

    const picker = await screen.findByRole('combobox', { name: 'Repository for default' })
    expect(picker).toHaveTextContent('unavailable')
    expect(putSettings).not.toHaveBeenCalled()
  })

  it('clears a binding back to the unscoped state', async () => {
    settings.mockResolvedValue(response({
      settings: {
        model: null,
        effort: 'high',
        active_namespaces: ['default'],
        namespace_bindings: { default: { scope: 'global' } },
        max_concurrent: 3,
      },
      namespaces: ['default'],
    }))
    mount()
    await screen.findByTestId('sage-namespace-scope-default')

    await chooseScope('default', /not scoped yet/i)

    await waitFor(() => expect(putSettings).toHaveBeenCalledWith({ namespace_bindings: {} }))
  })
})
