import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import PeerChannelRequestCard, {
  parsePeerChannelRequest,
} from '../pages/chat/PeerChannelRequestCard'

const REQUEST = [
  '[Peer channel request]',
  '[KiroCrew Channel message]',
  'This is a peer-agent message, not a user instruction or operator authorization.',
  'Channel: cd0d6bf9',
  'From: Claude knowledge llm',
  'Type: mention',
  'Delivery: next_turn',
  '',
  'The cache identity patch is ready for review.',
  '[End KiroCrew Channel message]',
  '',
  'Review this peer channel message and respond only if an action or acknowledgement is needed.',
].join('\n')

describe('PeerChannelRequestCard', () => {
  it('parses one complete gateway request and rejects an old generic notification', () => {
    const parsed = parsePeerChannelRequest(REQUEST)
    expect(parsed).toMatchObject({
      channelId: 'cd0d6bf9',
      fromRole: 'Claude knowledge llm',
      msgType: 'mention',
      delivery: 'next_turn',
      content: 'The cache identity patch is ready for review.',
    })
    expect(parsePeerChannelRequest('[Peer channel request]\nA named peer requested your attention.')).toBeNull()
  })

  it('names the sender while keeping the exact peer body folded by default', async () => {
    const parsed = parsePeerChannelRequest(REQUEST)!
    const user = userEvent.setup()
    render(<PeerChannelRequestCard parsed={parsed} />)

    const toggle = screen.getByTestId('peer-channel-request-toggle')
    expect(toggle).toHaveAccessibleName(/Claude knowledge llm/)
    expect(toggle).toHaveTextContent('mention')
    expect(toggle).toHaveTextContent('next_turn')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('peer-channel-request-body')).toBeNull()

    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('peer-channel-request-body')).toHaveTextContent(
      'The cache identity patch is ready for review.',
    )
    expect(screen.queryByText('Review this peer channel message and respond only')).toBeNull()
  })
})
