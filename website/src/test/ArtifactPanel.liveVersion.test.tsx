import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ArtifactPanel from '../components/ArtifactPanel'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../components/DetailPanel', () => ({
  default: ({ title, footer, children }: { title: React.ReactNode; footer: React.ReactNode; children: React.ReactNode }) => (
    <section><header>{title}</header>{children}<footer>{footer}</footer></section>
  ),
}))
vi.mock('../components/ArtifactBody', () => ({
  ArtifactBodyNative: ({ content }: { content: string }) => <div>{content}</div>,
  ArtifactBodyIframe: () => <div>iframe artifact</div>,
}))
vi.mock('../components/FileArtifactComments', () => ({
  useFileArtifactComments: () => ({
    comments: [], sidebarOpen: false, sidebar: null, popovers: null,
    requestAnchoredComment: vi.fn(), toggleSidebar: vi.fn(),
    activateComment: vi.fn(), commentCount: 0, onIframeSelect: vi.fn(),
    onIframeOpenThread: vi.fn(), iframeScrollTarget: null, activeCommentId: null,
    unreadRootIds: new Set(), scrollNonce: 0,
  }),
}))
vi.mock('../components/SelectionToolbar', () => ({ default: () => null }))

const mkArtifact = (version: number, content: string): Artifact => ({
  slug: 'tour', name: 'PR lifecycle tour', kind: 'svg', source: 'chat',
  description: '', tags: [], version, content,
  created_at: '2026-08-14T00:00:00Z', updated_at: '2026-08-14T00:00:00Z',
})

function renderPanel(active: boolean, queryClient: QueryClient) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ArtifactPanel slug="tour" kind="svg" content="chat snapshot v1" isTabActive={active} onClose={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ArtifactPanel live version refresh', () => {
  it('refetches the canonical artifact and shows its live version when a kept-mounted tab becomes active', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } })
    queryClient.setQueryData(['artifact', 'tour'], mkArtifact(1, 'stored v1'))
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact(2, 'stored v2'))

    const view = renderPanel(false, queryClient)
    expect(screen.getByText('stored v1')).toBeInTheDocument()
    expect(api.artifact).not.toHaveBeenCalled()

    view.rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ArtifactPanel slug="tour" kind="svg" content="chat snapshot v1" isTabActive onClose={vi.fn()} />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    await waitFor(() => expect(screen.getByText('stored v2')).toBeInTheDocument())
    expect(screen.getByText('Live · v2')).toBeInTheDocument()
    expect(api.artifact).toHaveBeenCalledWith('tour')
  })
})
