import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

const channel = {
  id: 'ch1',
  topic: 'Coordination',
  members: {
    lead: {
      id: 'lead', role: 'Lead', agent_name: 'crew-codex', state: 'listening',
      listen_mode: 'all', approval_policy: 'writes', session_key: 'dashboard:chat-lead',
      is_orchestrator: true,
    },
  },
  attach_requests: [
    {
      id: 'req1', slot: 'chat-review', title: 'Review lane', role: 'Reviewer',
      reason: 'Needs a second pair of eyes.', requested_by: 'lead',
    },
  ],
  messages: [],
}

describe('ChannelPage — coordinator attach requests', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [channel] })
    vi.mocked(api).channelGet = vi.fn().mockResolvedValue(channel)
    vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({ presets: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    vi.mocked(api).channelResolveAttachRequest = vi.fn().mockResolvedValue({ ok: true })
  })

  it('shows the pending request with its coordinator, target and reason', async () => {
    renderWithProviders(<ChannelPage />)
    const card = await screen.findByTestId('channel-attach-request')
    expect(card).toHaveTextContent('Lead asks to add Review lane as Reviewer')
    expect(card).toHaveTextContent('Needs a second pair of eyes.')
  })

  it.each(['approve', 'decline'] as const)('sends %s for that request', async (action) => {
    const user = userEvent.setup()
    renderWithProviders(<ChannelPage />)
    await screen.findByTestId('channel-attach-request')
    await user.click(screen.getByRole('button', { name: action === 'approve' ? 'Approve' : 'Decline' }))
    await waitFor(() =>
      expect(vi.mocked(api).channelResolveAttachRequest).toHaveBeenCalledWith('ch1', 'req1', action),
    )
  })
})
