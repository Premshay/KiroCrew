/**
 * Renders one persisted peer-channel delivery without exposing its machine
 * instruction. The request envelope is shared by the model and transcript, so
 * the card can show the sender and exact body that the model received.
 */
import { memo } from 'react'
import { ChevronRight, MessageCircle } from 'lucide-react'

import MarkdownRenderer from '../../components/MarkdownRenderer'
import { useRowDisclosure } from './rowDisclosure'
import { parsePeerChannelRequest, type ParsedPeerChannelRequest } from './peerChannelRequest'

// Re-exported so existing importers keep one entry point for the card and its
// parser; the parser itself lives in a pure module the grouping pass can use.
export { parsePeerChannelRequest }
export type { ParsedPeerChannelRequest }

export default memo(function PeerChannelRequestCard({
  parsed,
  disclosureKey,
}: {
  parsed: ParsedPeerChannelRequest
  disclosureKey?: string
}) {
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  // Delivery remains in the durable envelope for diagnostics, but transport
  // field names and values are not dashboard copy.
  const label = parsed.delivery === 'interrupt' ? 'Urgent' : 'Message'

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
        <span className="font-medium text-fg shrink-0">{label}</span>
        <span className="truncate text-[12px] opacity-75 min-w-0">{parsed.fromRole}</span>
      </button>
      {expanded && (
        <div className="px-2.5 pb-2.5 pt-2 border-t border-border" data-testid="peer-channel-request-body">
          <MarkdownRenderer content={parsed.content} />
        </div>
      )}
    </div>
  )
})
