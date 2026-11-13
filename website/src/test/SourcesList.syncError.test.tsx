import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

// A source whose last sync failed keeps its red Error badge and says so under the row.

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

function renderWith(source: Partial<Source>) {
  const sources: Source[] = [{
    id: 's1', name: 'notes.md', source_type: 'local_file', uri: '/tmp/notes.md', item_count: 0, sync_status: 'synced', ...source,
  }]
  vi.mocked(api.knowledgeApi).mockImplementation(async (path: string) => {
    if (path === '/sources') return sources as unknown as never
    if (path === '/sources/s1/files') return { total: 3, done: 1, failed: 1, skipped: 0, files: [
      { file_path: '/tmp/bad.md', status: 'failed', error_message: 'Read failed', item_count: 0 },
      { file_path: '/tmp/next.md', status: 'scanning', item_count: 0 },
    ] } as unknown as never
    return {} as unknown as never
  })
  return render(
    <SourcesList onIngest={() => {}} uploadNamespace="" setUploadNamespace={() => {}} namespaces={[]} ingestionJobs={[]} supportedFormatsDisplay="" />,
    { wrapper },
  )
}

beforeEach(() => {
  queryClient.clear()
  vi.mocked(api.knowledgeApi).mockReset()
})

describe('SourcesList — a source whose sync failed', () => {
  it('keeps the Error badge and shows the failure under the row', async () => {
    renderWith({ sync_status: 'error' })
    const notice = await screen.findByTestId('source-sync-error')
    expect(notice).toHaveTextContent('The last sync of this source failed.')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(screen.getByText('Error')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('shows no notice on a synced row', async () => {
    renderWith({ sync_status: 'synced' })
    await screen.findByText('notes.md')
    expect(screen.queryByTestId('source-sync-error')).toBeNull()
  })
})

describe('SourcesList — a folder scan in which one file failed', () => {
  afterEach(() => { vi.useRealTimers() })

  const sourcesCalls = () => vi.mocked(api.knowledgeApi).mock.calls.filter(([p]) => p === '/sources').length

  it('shows failure and progress while polling, then keeps the final error', async () => {
    vi.useFakeTimers()
    const source: Partial<Source> = { source_type: 'local_folder', uri: '/tmp/notes', sync_status: 'error', scanning: true, spend: { files_total: 3, files_done: 1, files_failed: 1 } }
    renderWith(source)
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(screen.getByText('Syncing')).toBeInTheDocument()
    expect(screen.getByTestId('source-sync-error')).toHaveTextContent('The last sync of this source failed.')
    fireEvent.click(screen.getByRole('button', { name: '1 failed' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(screen.getByText('1/3 (33%)')).toBeInTheDocument()
    expect(screen.getByText('bad.md')).toBeInTheDocument()
    expect(screen.getByText('Read failed')).toBeInTheDocument()
    expect(screen.getByText('next.md')).toBeInTheDocument()
    expect(screen.queryByText('Error')).toBeNull()
    const before = sourcesCalls()
    vi.mocked(api.knowledgeApi).mockImplementation(async (path: string) => {
      if (path === '/sources') return [{ id: 's1', name: 'notes.md', ...source, scanning: false }] as unknown as never
      if (path === '/sources/s1/files') return { total: 3, done: 1, failed: 1, skipped: 0, files: [] } as unknown as never
      return {} as unknown as never
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(sourcesCalls()).toBeGreaterThan(before)
    expect(screen.getByText('Error')).toBeInTheDocument()
    expect(screen.getByTestId('source-sync-error')).toHaveTextContent('The last sync of this source failed.')
  })
})
