import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import RestartButton from './RestartButton'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      restartSessions: vi.fn(),
      restartBlockers: vi.fn(),
      clearRestartBlockers: vi.fn(),
    },
  }
})

const restartSessions = vi.mocked(api.restartSessions)
const restartBlockers = vi.mocked(api.restartBlockers)

const ackRequired = () =>
  new ApiError(
    409,
    'restart blocked',
    JSON.stringify({
      ok: false,
      code: 'restart_ack_required',
      maintenance: { active: true, ready: false, pending: [], unmanaged_busy: ['channel:c1:a1'] },
    }),
  )

describe('RestartButton with blocked restarts', () => {
  beforeEach(() => {
    restartSessions.mockReset()
    restartBlockers.mockReset()
    restartBlockers.mockResolvedValue({
      ok: true,
      maintenance: {
        active: true,
        ready: false,
        required: [],
        pending: [],
        unmanaged_busy: ['channel:c1:a1'],
      },
      channel_blockers: [
        {
          session_key: 'channel:c1:a1',
          channel_id: 'c1',
          channel_topic: 'Incident 4821',
          agent_id: 'a1',
          role: 'Logs Agent',
          agent_name: '',
          state: 'working',
          is_coordinator: false,
        },
      ],
      other_blockers: [],
    })
  })

  it('names the blockers when the gateway refuses the reset', async () => {
    restartSessions.mockRejectedValue(ackRequired())
    render(<RestartButton />)

    fireEvent.click(screen.getByText(/apply & restart/i))

    expect(await screen.findByTestId('restart-blockers')).toBeInTheDocument()
    expect(screen.getByText('Logs Agent')).toBeInTheDocument()
    expect(screen.getByText(/restart is waiting on active sessions/i)).toBeInTheDocument()
    // The panel carries that sentence; the button does not repeat it.
    expect(screen.getAllByText(/restart is waiting on active sessions/i)).toHaveLength(1)
  })

  it('leaves an ordinary failure as a message with no blocker panel', async () => {
    restartSessions.mockRejectedValue(new Error('zzq-restart-broke'))
    render(<RestartButton />)

    fireEvent.click(screen.getByText(/apply & restart/i))

    expect(await screen.findByText('zzq-restart-broke')).toBeInTheDocument()
    expect(screen.queryByTestId('restart-blockers')).not.toBeInTheDocument()
    expect(restartBlockers).not.toHaveBeenCalled()
  })

  it('drops the panel once a later restart succeeds', async () => {
    restartSessions.mockRejectedValueOnce(ackRequired())
    render(<RestartButton />)

    fireEvent.click(screen.getByText(/apply & restart/i))
    expect(await screen.findByTestId('restart-blockers')).toBeInTheDocument()

    restartSessions.mockResolvedValue({
      ok: true,
      sessions_reset: 3,
      mcp_synced: 0,
      mcp_sync_ok: true,
    })
    fireEvent.click(screen.getByText(/apply & restart/i))

    await waitFor(() =>
      expect(screen.queryByTestId('restart-blockers')).not.toBeInTheDocument(),
    )
  })
})
