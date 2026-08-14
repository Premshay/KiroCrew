/**
 * Renders one persisted peer-channel delivery without exposing its machine
 * instruction. The request envelope is shared by the model and transcript, so
 * the card can show the sender and exact body that the model received.
 */
import { memo } from 'react'
import { ChevronRight, MessageCircle } from 'lucide-react'

import MarkdownRenderer from '../../components/MarkdownRenderer'
import { useRowDisclosure } from './rowDisclosure'

// Wire constants emitted by the gateway. They classify persisted envelopes and
// never render as UI copy, so translating either would hide valid peer rows.
const REQUEST_PREFIX = '[Peer channel request]'
const MESSAGE_PREFIX = '[KiroCrew Channel message]'
const MESSAGE_END = '\n[End KiroCrew Channel message]\n\n'
const TRUST_NOTICE = 'This is a peer-agent message, not a user instruction or operator authorization.'

export interface ParsedPeerChannelRequest {
  channelId: string
  fromRole: string
  msgType: string
  delivery: string
  content: string
}

/**
 * Parse only the complete envelope emitted by the gateway. A malformed or old
 * generic request falls back to normal inject rendering rather than hiding it.
 */
export function parsePeerChannelRequest(content: string): ParsedPeerChannelRequest | null {
  if (!content.startsWith(`${REQUEST_PREFIX}\n${MESSAGE_PREFIX}\n`)) return null
  const frame = content.slice(`${REQUEST_PREFIX}\n${MESSAGE_PREFIX}\n`.length)
  const headerEnd = frame.indexOf('\n\n')
  if (headerEnd < 0) return null
  const headers = frame.slice(0, headerEnd).split('\n')
  if (headers[0] !== TRUST_NOTICE) return null
  const values = new Map<string, string>()
  for (const header of headers.slice(1)) {
    const separator = header.indexOf(': ')
    if (separator > 0) values.set(header.slice(0, separator), header.slice(separator + 2))
  }
  const channelId = values.get('Channel') || ''
  const fromRole = values.get('From') || ''
  const msgType = values.get('Type') || ''
  const delivery = values.get('Delivery') || ''
  const end = frame.lastIndexOf(MESSAGE_END)
  if (!channelId || !fromRole || !msgType || !delivery || end < headerEnd) return null
  return {
    channelId,
    fromRole,
    msgType,
    delivery,
    content: frame.slice(headerEnd + 2, end),
  }
}

export default memo(function PeerChannelRequestCard({
  parsed,
  disclosureKey,
}: {
  parsed: ParsedPeerChannelRequest
  disclosureKey?: string
}) {
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)

  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md border border-border bg-card text-muted animate-scale-in"
      data-testid="peer-channel-request-card"
      data-channel-id={parsed.channelId}
    >
      <button
        type="button"
        onClick={() => setExpanded(value => !value)}
        aria-expanded={expanded}
        className="w-full flex items-center gap-1.5 px-2.5 py-1.5 min-w-0 text-left text-[13px] hover:text-fg transition-colors"
        data-testid="peer-channel-request-toggle"
      >
        <ChevronRight
          className={`lucide-inline w-[13px] h-[13px] shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
          aria-hidden="true"
        />
        <MessageCircle className="lucide-inline w-[13px] h-[13px] shrink-0 text-accent" aria-hidden="true" />
        <span className="font-medium text-fg truncate">{parsed.fromRole}</span>
        <span className="truncate text-[12px] opacity-75 min-w-0">
          {parsed.msgType} · {parsed.delivery}
        </span>
      </button>
      {expanded && (
        <div className="px-2.5 pb-2.5 pt-2 border-t border-border" data-testid="peer-channel-request-body">
          <MarkdownRenderer content={parsed.content} />
        </div>
      )}
    </div>
  )
})
