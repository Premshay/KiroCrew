/**
 * The arc board console end to end against a mocked API: the three panes
 * render from one detail payload, the lane picker lists mates and kits, an
 * accept goes out with the milestone index the owner saw, and a lane's
 * script link is never rendered as an href.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

vi.mock('../api/client', () => ({
  api: {
    arcBoardArcs: vi.fn(),
    arcBoardDetail: vi.fn(),
    arcBoardAccept: vi.fn(),
    arcBoardAddMember: vi.fn(),
    arcBoardReport: vi.fn(),
    chatSlots: vi.fn(),
    autonudgeList: vi.fn(),
  },
}))

import { api } from '../api/client'
import ArcBoardPage from './ArcBoardPage'

const ARC = 'arc_' + 'a'.repeat(32)
const arc = {
  id: ARC,
  title: 'Classics references',
  goal: 'field convention',
  reason: 'pilot',
  lineage: 'classicist',
  source: '',
  autonomy: 2,
  milestones: [
    { name: 'M1 short form', criteria: 'VI.366 L.' },
    { name: 'M2 editor', criteria: 'composite places' },
  ],
  decisions: [],
  rules: [],
  reuse: [],
  channel_id: 'ch',
  members: { 'chat-1': { role: 'Tenon', joined_at: '2026-11-01T10:00:00+00:00' } },
  milestone_index: 0,
  created_at: '2026-11-01T10:00:00+00:00',
}
const entries = [
  { id: 'ae_1', ts: '2026-11-01T10:01:00+00:00', author: 'chat-1', kind: 'FACT', text: 'rendered', link: 'javascript:alert(1)' },
  { id: 'ae_2', ts: '2026-11-01T10:02:00+00:00', author: 'chat-1', kind: 'DONE', text: 'm1 ready', link: 'https://github.com/o/r/pull/9 — done' },
  { id: 'ae_3', ts: '2026-11-01T10:03:00+00:00', author: 'chat-1', kind: 'ASK', text: 'Short form or Siglum?', to: 'owner' },
]

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ArcBoardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(api.arcBoardArcs).mockResolvedValue({
    arcs: [
      {
        id: ARC, title: arc.title, goal: arc.goal, reason: arc.reason, lineage: arc.lineage, autonomy: 2,
        milestone_index: 0, milestone: 'M1 short form', milestones_total: 2, members: 1, open_asks: 1,
        open_claims: 0, created_at: arc.created_at,
      },
    ],
  } as never)
  vi.mocked(api.arcBoardDetail).mockResolvedValue({
    arc,
    lanes: [{ key: 'chat-1', role: 'Tenon', joined_at: arc.created_at, entries: 3, last_ts: entries[2].ts, last_kind: 'ASK', claim: null }],
    briefs: { 'chat-1': 'BRIEF for Tenon' },
    entries,
    open_claims: [],
    open_asks: [{ ...entries[2], age_secs: 60, seen_by: [] }],
    collisions: [],
    milestone_reports: [entries[1]],
  } as never)
  vi.mocked(api.chatSlots).mockResolvedValue([
    { key: 'dashboard:chat-1', running: true, agent: 'tenon', agent_kind: 'member', title: 'Tenon', messages: 1 },
    { key: 'dashboard:chat-2', running: false, agent: 'baton', agent_kind: 'member', title: 'Baton', messages: 1 },
    { key: 'dashboard:chat-3', running: false, agent: 'kit-evaluation', agent_kind: 'template', title: 'Gate', messages: 1 },
  ] as never)
  vi.mocked(api.autonudgeList).mockResolvedValue({ enabled: true, loops: [] } as never)
  vi.mocked(api.arcBoardAccept).mockResolvedValue({ arc: { ...arc, milestone_index: 1 } } as never)
})

describe('ArcBoardPage', () => {
  it('renders the arc, its lane, the owner ask and the selected lane brief', async () => {
    renderPage()
    expect(await screen.findAllByText('Classics references')).not.toHaveLength(0)
    expect((await screen.findAllByText('Short form or Siglum?')).length).toBe(2)
    expect(screen.getByText('BRIEF for Tenon')).toBeTruthy()
    expect(screen.getAllByText('Tenon').length).toBeGreaterThan(0)
  })

  it('never renders a lane script link as an href', async () => {
    const { container } = renderPage()
    await screen.findByText('BRIEF for Tenon')
    const hrefs = Array.from(container.querySelectorAll('a')).map(a => a.getAttribute('href'))
    expect(hrefs.some(h => h?.toLowerCase().startsWith('javascript'))).toBe(false)
    expect(hrefs).toContain('https://github.com/o/r/pull/9')
  })

  it('accepts only after confirming, with the index the owner saw', async () => {
    renderPage()
    await screen.findByText('BRIEF for Tenon')
    const accept = screen.getAllByRole('button', { name: /accept/i })[0]
    fireEvent.click(accept)
    expect(api.arcBoardAccept).not.toHaveBeenCalled()
    const confirm = screen.getAllByRole('button', { name: /accept/i })[0]
    fireEvent.click(confirm)
    await waitFor(() => expect(api.arcBoardAccept).toHaveBeenCalledWith(ARC, 0))
  })

  it('offers mates and mission kits that are not yet lanes', async () => {
    renderPage()
    await screen.findByText('BRIEF for Tenon')
    const lanesCard = screen.getAllByRole('button', { name: /add lane/i })[0]
    fireEvent.click(lanesCard)
    const search = await screen.findByLabelText(/search sessions/i)
    const picker = search.parentElement as HTMLElement
    await within(picker).findByText('Baton')
    expect(within(picker).getByText('Gate')).toBeTruthy()
    expect(within(picker).queryByText('kit-evaluation')).toBeTruthy()
  })
})
