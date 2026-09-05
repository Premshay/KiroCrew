import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import RestartBlockers from './RestartBlockers'
import { api, type RestartBlockerReport } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: { ...mod.api, restartBlockers: vi.fn(), clearRestartBlockers: vi.fn() },
  }
})

const restartBlockers = vi.mocked(api.restartBlockers)
const clearRestartBlockers = vi.mocked(api.clearRestartBlockers)

const worker = (over: Partial<RestartBlockerReport['channel_blockers'][number]> = {}) => ({
  session_key: 'channel:c1:a1',
  channel_id: 'c1',
  channel_topic: 'Incident 4821',
  agent_id: 'a1',
  role: 'Logs Agent',
  agent_name: '',
  state: 'tool_running',
  is_coordinator: false,
  ...over,
})

const report = (over: Partial<RestartBlockerReport> = {}): RestartBlockerReport => ({
  ok: true,
  maintenance: {
    active: true,
    ready: false,
    required: [],
    pending: [],
    unmanaged_busy: ['channel:c1:a1'],
  },
  channel_blockers: [worker()],
  other_blockers: [],
  ...over,
})

describe('RestartBlockers', () => {
  beforeEach(() => {
    restartBlockers.mockReset()
    clearRestartBlockers.mockReset()
  })

  it('names a busy channel worker by its role, channel and current state', async () => {
    restartBlockers.mockResolvedValue(report())
    render(<RestartBlockers />)

    expect(await screen.findByText('Logs Agent')).toBeInTheDocument()
    expect(screen.getByText('Incident 4821')).toBeInTheDocument()
    expect(screen.getByText(/running/i)).toBeInTheDocument()
  })

  it('lists a busy dashboard slot without offering an action over it', async () => {
    restartBlockers.mockResolvedValue(
      report({
        maintenance: {
          active: true,
          ready: false,
          required: ['dashboard:main'],
          pending: ['dashboard:main'],
          unmanaged_busy: [],
        },
        channel_blockers: [],
      }),
    )
    render(<RestartBlockers />)

    expect(await screen.findByText('dashboard:main')).toBeInTheDocument()
    expect(screen.queryByTestId('restart-blockers-clear')).not.toBeInTheDocument()
  })

  it('says why a blocking session that is not a channel worker has no button', async () => {
    restartBlockers.mockResolvedValue(
      report({
        channel_blockers: [],
        other_blockers: [{ session_key: 'task:runner-7', reason: 'not_a_channel_worker' }],
      }),
    )
    render(<RestartBlockers />)

    expect(await screen.findByText('task:runner-7')).toBeInTheDocument()
    expect(screen.getByText(/not a channel worker/i)).toBeInTheDocument()
    expect(screen.queryByTestId('restart-blockers-clear')).not.toBeInTheDocument()
  })

  it('states that clearing context is not a dismissal', async () => {
    restartBlockers.mockResolvedValue(report())
    render(<RestartBlockers />)

    expect(await screen.findByText(/this is not a dismissal/i)).toBeInTheDocument()
    // Nothing here offers to remove a worker from its channel, so no control
    // can be mistaken for one that stops it.
    expect(screen.queryByText(/dismiss$/i)).not.toBeInTheDocument()
  })

  it('does not clear anything until the operator confirms', async () => {
    restartBlockers.mockResolvedValue(report())
    render(<RestartBlockers />)

    fireEvent.click(await screen.findByTestId('restart-blockers-clear'))
    expect(clearRestartBlockers).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('restart-blockers-confirm'))
    await waitFor(() =>
      expect(clearRestartBlockers).toHaveBeenCalledWith(['channel:c1:a1']),
    )
  })

  it('cancelling the confirmation clears nothing', async () => {
    restartBlockers.mockResolvedValue(report())
    render(<RestartBlockers />)

    fireEvent.click(await screen.findByTestId('restart-blockers-clear'))
    fireEvent.click(screen.getByTestId('restart-blockers-cancel'))

    expect(clearRestartBlockers).not.toHaveBeenCalled()
    expect(screen.getByTestId('restart-blockers-clear')).toBeInTheDocument()
  })

  it('reports a worker that had already finished as skipped, not cleared', async () => {
    restartBlockers.mockResolvedValue(report())
    clearRestartBlockers.mockResolvedValue({
      ...report({
        maintenance: {
          active: true,
          ready: true,
          required: [],
          pending: [],
          unmanaged_busy: [],
        },
        channel_blockers: [],
      }),
      results: [
        { session_key: 'channel:c1:a1', outcome: 'skipped', reason: 'not_blocking' },
      ],
    })
    render(<RestartBlockers />)

    fireEvent.click(await screen.findByTestId('restart-blockers-clear'))
    fireEvent.click(screen.getByTestId('restart-blockers-confirm'))

    expect(await screen.findByText(/already finished/i)).toBeInTheDocument()
    expect(screen.queryByText(/context cleared/i)).not.toBeInTheDocument()
  })

  it('shows the barrier as it stands after the batch, not as it was before', async () => {
    restartBlockers.mockResolvedValue(report())
    // A second operator's worker started blocking while this batch ran: the
    // panel must show what is left, not congratulate the operator on an empty
    // list it measured before the clear.
    clearRestartBlockers.mockResolvedValue({
      ...report({
        channel_blockers: [
          worker({ session_key: 'channel:c2:a9', channel_topic: 'Release 0.4', role: 'Reviewer' }),
        ],
      }),
      results: [
        {
          session_key: 'channel:c1:a1',
          outcome: 'cleared',
          reason: '',
          channel_id: 'c1',
          role: 'Logs Agent',
        },
      ],
    })
    render(<RestartBlockers />)

    fireEvent.click(await screen.findByTestId('restart-blockers-clear'))
    fireEvent.click(screen.getByTestId('restart-blockers-confirm'))

    expect(await screen.findByText('Reviewer')).toBeInTheDocument()
    expect(screen.getByText('Release 0.4')).toBeInTheDocument()
    expect(screen.getByTestId('restart-blockers-clear')).toBeInTheDocument()
  })

  it('surfaces a failed clear and leaves the panel usable', async () => {
    restartBlockers.mockResolvedValue(report())
    clearRestartBlockers.mockRejectedValue(new Error('zzq-clear-broke'))
    render(<RestartBlockers />)

    fireEvent.click(await screen.findByTestId('restart-blockers-clear'))
    fireEvent.click(screen.getByTestId('restart-blockers-confirm'))

    expect(await screen.findByText('zzq-clear-broke')).toBeInTheDocument()
    expect(screen.getByTestId('restart-blockers-clear')).toBeInTheDocument()
  })

  it('reports a per-worker failure returned by a partly successful batch', async () => {
    restartBlockers.mockResolvedValue(report())
    clearRestartBlockers.mockResolvedValue({
      ...report(),
      results: [
        {
          session_key: 'channel:c1:a1',
          outcome: 'failed',
          reason: 'clear_failed',
          role: 'Logs Agent',
          detail: 'provider shutdown timed out',
        },
      ],
    })
    render(<RestartBlockers />)

    fireEvent.click(await screen.findByTestId('restart-blockers-clear'))
    fireEvent.click(screen.getByTestId('restart-blockers-confirm'))

    expect(await screen.findByText(/could not clear/i)).toBeInTheDocument()
    expect(screen.getByText(/provider shutdown timed out/)).toBeInTheDocument()
  })

  it('says so when nothing is holding up a restart', async () => {
    restartBlockers.mockResolvedValue(
      report({
        maintenance: { active: false, ready: true, required: [], pending: [], unmanaged_busy: [] },
        channel_blockers: [],
      }),
    )
    render(<RestartBlockers />)

    expect(await screen.findByTestId('restart-blockers-empty')).toBeInTheDocument()
  })

  it('surfaces a failure to read the blocker list', async () => {
    restartBlockers.mockRejectedValue(new Error('zzq-read-broke'))
    render(<RestartBlockers />)

    expect(await screen.findByText('zzq-read-broke')).toBeInTheDocument()
  })
})
