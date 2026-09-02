import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import LearningRail from '../apps/code-review-sage/components/LearningRail'
import LearningView from '../apps/code-review-sage/views/LearningView'
import { SageProvider } from '../apps/code-review-sage/context'
import { SageApiError, sageApi } from '../apps/code-review-sage/api'

vi.mock('../apps/code-review-sage/api', () => ({
  SageApiError: class SageApiError extends Error {
    code: string
    constructor(message: string, code: string) {
      super(message)
      this.code = code
    }
  },
  sageApi: {
    namespaces: vi.fn(),
    learnings: vi.fn(),
    requestConsolidationPreview: vi.fn(),
    consolidationPreviews: vi.fn(),
    consolidationPreview: vi.fn(),
    applyConsolidationPreview: vi.fn(),
    createNamespace: vi.fn(),
    deleteNamespace: vi.fn(),
    settings: vi.fn(),
    putSettings: vi.fn(),
    runs: vi.fn(),
    pinnedRepos: vi.fn(),
    recentRepos: vi.fn(),
    myRepos: vi.fn(),
    repoPrs: vi.fn(),
  },
}))

const mockApi = sageApi as unknown as Record<string, ReturnType<typeof vi.fn>>

function pattern(title: string, id = title) {
  return {
    id,
    title,
    guidance: `Guidance for ${title}`,
    scope: 'common',
    impact: 'high',
  }
}

const PREVIEW = {
  preview_id: 'preview-1',
  namespace: 'default',
  status: 'pending_confirmation',
  selected_candidate_ids: ['c1'],
  proposed_ruleset_markdown: '# Proposed rules\n\n- Check the boundary.',
  per_candidate_decisions: [
    { candidate_id: 'c1', action: 'merge', reason_code: 'candidate_merged' },
  ],
  budget_impact: {
    governed: true,
    archived_record_ids: [],
    selection: {
      usage: { global: { rules: 1, tokens: 12 } },
      budgets: { global: { max_rules: 60, max_tokens: 12000 } },
    },
  },
  state: {
    preview_id: 'preview-1',
    namespace: 'default',
    status: 'pending_confirmation',
    expired: false,
    stale: false,
    stale_reasons: [],
    expires_at_epoch: 9999999999,
  },
}

function mount() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SageProvider initialRunId={null}>
          <LearningRail />
          <LearningView />
        </SageProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

async function openNamespace() {
  await waitFor(() => expect(mockApi.learnings).toHaveBeenCalledWith('default'))
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.namespaces.mockResolvedValue({
    namespaces: [{ name: 'default', patterns: 1, candidate: 2, active: true }],
    active: ['default'],
  })
  mockApi.settings.mockResolvedValue({
    settings: { active_namespaces: ['default'] },
    pool: null,
    reviewer: null,
  })
  mockApi.learnings.mockResolvedValue({
    namespace: 'default',
    patterns: [pattern('Existing rule')],
    candidate: [pattern('Staged one', 'c1'), pattern('Staged two', 'c2')],
    consolidating: false,
    consolidate_error: null,
  })
  mockApi.consolidationPreviews.mockResolvedValue({
    code: 'preview_list',
    namespace: 'default',
    previews: [],
  })
  mockApi.consolidationPreview.mockResolvedValue({
    code: 'preview_detail',
    preview: PREVIEW,
  })
  mockApi.requestConsolidationPreview.mockResolvedValue({
    ok: true,
    code: 'preview_queued',
    namespace: 'default',
    candidate_ids: ['c1'],
    running: true,
  })
  mockApi.applyConsolidationPreview.mockResolvedValue({
    ok: true,
    code: 'preview_applied',
  })
  mockApi.runs.mockResolvedValue({ runs: [] })
  mockApi.pinnedRepos.mockResolvedValue({ repos: [] })
  mockApi.repoPrs.mockResolvedValue({ repo: '', prs: [], count: 0 })
})

describe('curated Sage consolidation', () => {
  it('keeps every pending candidate retained until an operator selects it', async () => {
    mount()
    await openNamespace()
    expect(await screen.findAllByText(/Retained \(not selected\)/)).toHaveLength(2)
    expect(screen.getByRole('button', { name: /Create preview/i })).toBeDisabled()
  })

  it('requests a preview for exactly the selected candidates', async () => {
    const user = userEvent.setup()
    mount()
    await openNamespace()
    await user.click(await screen.findByRole('checkbox', { name: /Select Staged one/i }))
    expect(screen.getByText(/1 selected/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Create preview/i }))
    await waitFor(() =>
      expect(mockApi.requestConsolidationPreview).toHaveBeenCalledWith('default', ['c1']),
    )
  })

  it('renders the proposal in one bounded preview pane', async () => {
    mockApi.consolidationPreviews.mockResolvedValue({
      code: 'preview_list',
      namespace: 'default',
      previews: [
        {
          preview_id: 'preview-1',
          namespace: 'default',
          created_at: '2026-09-02T00:00:00Z',
          selected_candidate_ids: ['c1'],
          state: PREVIEW.state,
        },
      ],
    })
    mount()
    await openNamespace()
    await waitFor(() => expect(mockApi.consolidationPreviews).toHaveBeenCalledWith('default'))
    await waitFor(() =>
      expect(mockApi.consolidationPreview).toHaveBeenCalledWith('default', 'preview-1'),
    )
    const proposedRules = await screen.findByText(/Check the boundary/)
    expect(proposedRules.closest('section')?.querySelector('[class*="max-h"]')).toHaveClass(
      'overflow-y-auto',
    )
    expect(screen.getByText('Decisions')).toBeInTheDocument()
    expect(screen.getByText('Merge')).toBeInTheDocument()
    expect(screen.getByText(/1\/60 rules/)).toBeInTheDocument()
  })

  it('puts preview status before the long ruleset', async () => {
    mockApi.consolidationPreviews.mockResolvedValue({
      code: 'preview_list',
      namespace: 'default',
      previews: [
        {
          preview_id: 'preview-1',
          namespace: 'default',
          created_at: '2026-09-02T00:00:00Z',
          selected_candidate_ids: ['c1'],
          state: PREVIEW.state,
        },
      ],
    })
    mount()
    await openNamespace()
    const history = await screen.findByText('Consolidation previews')
    const rule = screen.getByText('Existing rule')
    expect(history.compareDocumentPosition(rule) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
  })

  it('follows an already-running preview instead of leaving a retry error', async () => {
    const user = userEvent.setup()
    mockApi.requestConsolidationPreview.mockRejectedValue(
      new SageApiError('busy', 'consolidation_in_progress'),
    )
    mockApi.learnings.mockResolvedValue({
      namespace: 'default',
      patterns: [pattern('Existing rule')],
      candidate: [pattern('Staged one', 'c1'), pattern('Staged two', 'c2')],
      consolidating: true,
      consolidate_error: null,
    })
    mount()
    await openNamespace()
    await user.click(await screen.findByRole('checkbox', { name: /Select Staged one/i }))
    await user.click(screen.getByRole('button', { name: /Create preview/i }))
    expect(await screen.findByText('Preparing preview…')).toBeInTheDocument()
    expect(screen.queryByText(/already being prepared/i)).toBeNull()
  })

  it('requires a second explicit confirmation before it applies a fresh preview', async () => {
    mockApi.consolidationPreviews.mockResolvedValue({
      code: 'preview_list',
      namespace: 'default',
      previews: [
        {
          preview_id: 'preview-1',
          namespace: 'default',
          created_at: '2026-09-02T00:00:00Z',
          selected_candidate_ids: ['c1'],
          state: PREVIEW.state,
        },
      ],
    })
    const user = userEvent.setup()
    mount()
    await openNamespace()
    await waitFor(() =>
      expect(mockApi.consolidationPreview).toHaveBeenCalledWith('default', 'preview-1'),
    )
    await user.click(await screen.findByRole('button', { name: /^Apply preview$/i }))
    expect(mockApi.applyConsolidationPreview).not.toHaveBeenCalled()
    expect(screen.getByText(/Apply this preview now\?/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /^Apply preview$/i }))
    await waitFor(() =>
      expect(mockApi.applyConsolidationPreview).toHaveBeenCalledWith('default', 'preview-1'),
    )
  })

  it('blocks apply for stale previews and gives the safe next action', async () => {
    const stale = {
      ...PREVIEW,
      state: {
        ...PREVIEW.state,
        stale: true,
        stale_reasons: ['candidate_snapshot_changed'],
      },
    }
    mockApi.consolidationPreviews.mockResolvedValue({
      code: 'preview_list',
      namespace: 'default',
      previews: [
        {
          preview_id: 'preview-1',
          namespace: 'default',
          created_at: '2026-09-02T00:00:00Z',
          selected_candidate_ids: ['c1'],
          state: stale.state,
        },
      ],
    })
    mockApi.consolidationPreview.mockResolvedValue({
      code: 'preview_detail',
      preview: stale,
    })
    mount()
    await openNamespace()
    expect(await screen.findByText(/Preview is stale. Create a new preview./)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Apply preview$/i })).toBeNull()
  })
})
