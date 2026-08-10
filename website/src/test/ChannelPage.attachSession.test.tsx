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
    attached: {
      id: 'attached', role: 'Codex Multiplex', agent_name: 'crew-codex',
      state: 'listening', listen_mode: 'mention', approval_policy: 'writes',
      session_key: 'dashboard:chat-codex',
    },
  },
  messages: [],
}

describe('ChannelPage — attach live dashboard session', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [channel] })
    vi.mocked(api).channelGet = vi.fn().mockResolvedValue(channel)
    vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({ presets: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([
      { key: 'chat-codex', title: 'Codex Multiplex', agent: 'crew-codex' },
      { key: 'chat-claude', title: 'Claude Multiplex', agent: 'crew-claude' },
    ])
    vi.mocked(api).channelAttachSession = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).channelCreate = vi.fn().mockResolvedValue({ channel })
  })

  async function openSessionPicker() {
    const user = userEvent.setup()
    renderWithProviders(<ChannelPage />)
    await user.click(await screen.findByRole('button', { name: '1 agent' }))
    await user.click(screen.getByRole('button', { name: 'Sessions' }))
    return user
  }

  it('offers only dashboard sessions that are not already attached', async () => {
    await openSessionPicker()
    const picker = await screen.findByRole('combobox', { name: 'Sessions' })
    expect(picker).toHaveTextContent('Claude Multiplex · crew-claude')
    expect(picker).not.toHaveTextContent('Codex Multiplex')
  })

  it('attaches the selected dashboard slot then refreshes the channel', async () => {
    const user = await openSessionPicker()
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(vi.mocked(api).channelAttachSession).toHaveBeenCalledWith('ch1', 'chat-claude'))
    await waitFor(() => expect(vi.mocked(api).channelGet).toHaveBeenCalledWith('ch1'))
  })

  it('defaults to a session-only channel and opens its session picker', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ChannelPage />)

    const [mobileNew] = await screen.findAllByRole('button', { name: /New/ })
    await user.click(mobileNew)
    await user.type(screen.getByRole('textbox', { name: 'Topic' }), 'Multiplex coordination')
    await user.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(vi.mocked(api).channelCreate).toHaveBeenCalledWith(
      'Multiplex coordination', [], true,
    ))
    expect(await screen.findByRole('combobox', { name: 'Sessions' })).toBeInTheDocument()
  })

  it('offers a compact channel switcher on small screens', async () => {
    renderWithProviders(<ChannelPage />)

    expect(await screen.findByRole('combobox', { name: 'Channels' })).toBeInTheDocument()
  })
})
