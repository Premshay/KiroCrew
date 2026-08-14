/**
 * A file tab remains mounted while another sidebar tab is active. It must
 * refresh from disk when it becomes visible again, otherwise an SVG opened
 * from chat can keep an obsolete visual snapshot indefinitely.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@monaco-editor/react', () => ({
  default: () => <div data-testid="monaco" />,
  DiffEditor: () => <div data-testid="monaco-diff" />,
  loader: { config: () => {} },
}))
vi.mock('monaco-editor', () => ({}))
vi.mock('../utils/monacoLocal', () => ({ ensureMonacoLocal: async () => {} }))
vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    fileDiff: vi.fn().mockResolvedValue({ diff: '' }),
    revealPath: vi.fn(),
  },
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter><QueryClientProvider client={qc}>{children}</QueryClientProvider></MemoryRouter>
)

class FakeEventSource {
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  close() {}
}

beforeEach(() => {
  qc.clear()
  vi.stubGlobal('EventSource', FakeEventSource)
})

describe('MarkdownPanel live file refresh', () => {
  it('re-reads a watched file when its kept-mounted tab becomes visible', async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined)
    const props = {
      embedded: true,
      filePath: '/workspace/images/tour.svg',
      content: '<svg>old</svg>',
      onContentChange: vi.fn(),
      onSave: async () => {},
      onClose: () => {},
      liveWatch: true,
      onRefresh,
    }
    const { rerender } = render(<MarkdownPanel {...props} isTabActive={false} />, { wrapper })
    expect(onRefresh).not.toHaveBeenCalled()

    rerender(<MarkdownPanel {...props} isTabActive />, { wrapper })
    await waitFor(() => expect(onRefresh).toHaveBeenCalledWith('/workspace/images/tour.svg'))
    expect(onRefresh).toHaveBeenCalledTimes(1)
  })

  it('does not replace an unsaved buffer on activation', async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined)
    render(
      <MarkdownPanel
        embedded
        filePath="/workspace/notes.md"
        content="draft edit"
        savedBaseline="on disk"
        onContentChange={() => {}}
        onSave={async () => {}}
        onClose={() => {}}
        liveWatch
        isTabActive
        onRefresh={onRefresh}
      />,
      { wrapper },
    )

    await new Promise(resolve => setTimeout(resolve, 0))
    expect(onRefresh).not.toHaveBeenCalled()
  })
})
