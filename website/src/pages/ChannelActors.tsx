import {
  Check,
  Crown,
  Ear,
  Hourglass,
  MessageSquare,
  Radio,
  RotateCcw,
  User,
  VolumeX,
  Wrench,
  X
} from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import ApprovalCard from '../components/ApprovalCard'
import MarkdownRenderer from '../components/MarkdownRenderer'
import MessageErrorBoundary from '../components/MessageErrorBoundary'
import { Badge, Btn } from '../components/ui'
import { menuItemsOf, useMenuKeyboard } from '../hooks/useMenuKeyboard'
import type { RootState } from '../store'
import { useAppSelector } from '../store'

import { fmtDateFields } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { isChatPageSurface } from '../utils/channelOrigin'
// ── Types ──

export interface ChannelAgent {
  id: string
  role: string
  agentName: string
  sessionKey?: string
  state: 'pending' | 'working' | 'listening' | 'done' | 'failed' | 'tool_running'
  listenMode: 'all' | 'mention' | 'silent'
  approvalPolicy: 'all' | 'writes' | 'trusted'
  isCoordinator: boolean
}

interface ChannelMessage {
  id: string
  fromId: string
  fromRole: string
  content: string
  mention?: string | string[]
  msgType: 'progress' | 'mention' | 'broadcast' | 'approval' | 'done' | 'system'
  timestamp: number
  threadId?: string
  replyTo?: string
  replyCount: number
  /** Structured facts the backend posts beside an approval's prose (its
   *  `ChannelMessage.meta`): the server's own verdict on which trust tiers it
   *  can record. Absent on every other message and on approvals persisted
   *  before the field existed, which keep the prose-parsing fallback. */
  meta?: ApprovalMeta
}

/** Flat string map, mirroring chat's `perm_meta`: "1" is true, "" is false. */
export interface ApprovalMeta {
  tool_title?: string
  tool_input?: string
  command_grantable?: string
  base_derivable?: string
  base_command?: string
}

export interface AttachRequest {
  id: string
  slot: string
  title: string
  role: string
  reason: string
  requestedBy: string
}

export interface Channel {
  id: string
  topic: string
  agents: ChannelAgent[]
  messages: ChannelMessage[]
  attachRequests: AttachRequest[]
}

export interface ChannelPageError {
  title: string
  message: string
}

/* Map snake_case backend → camelCase frontend */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const mapAgent = (a: any): ChannelAgent => ({
  id: a.id,
  role: a.role,
  agentName: a.agent_name || a.agentName || '',
  state: a.state || 'pending',
  sessionKey: a.session_key || a.sessionKey,
  listenMode: a.listen_mode || a.listenMode || 'mention',
  approvalPolicy: a.approval_policy || a.approvalPolicy || 'writes',
  isCoordinator: Boolean(a.is_orchestrator ?? a.isCoordinator),
})

export interface DashboardSlot {
  key: string
  title?: string
  agent?: string
  mode?: string
  surface?: string
}

/** Only sessions the sidebar shows; a crewmate's own chat is its desk and stays out of channels. */
export const isAttachableSlot = (slot: DashboardSlot) => isChatPageSurface(slot.surface ?? slot.mode)

export const attachableSlotLabel = (slot: DashboardSlot) =>
  `${slot.title || slot.key}${slot.agent ? ` · ${slot.agent}` : ''}`
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const mapMsg = (m: any): ChannelMessage => ({
  id: m.id,
  fromId: m.from_id || m.fromId,
  fromRole: m.from_role || m.fromRole,
  content: m.content,
  mention: m.mention,
  msgType: m.msg_type || m.msgType || 'progress',
  timestamp: m.timestamp ? m.timestamp * 1000 : Date.now(),
  threadId: m.thread_id || m.threadId,
  replyTo: m.reply_to || m.replyTo,
  replyCount: m.reply_count || m.replyCount || 0,
  meta: m.meta && typeof m.meta === 'object' ? m.meta : undefined,
})
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const mapAttachRequest = (r: any): AttachRequest => ({
  id: r.id,
  slot: r.slot,
  title: r.title || r.slot,
  role: r.role || r.title || r.slot,
  reason: r.reason || '',
  requestedBy: r.requested_by || r.requestedBy || '',
})
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const mapChannel = (c: any): Channel => ({
  id: c.id,
  topic: c.topic,
  agents: Object.values(c.members || {}).map(mapAgent),
  messages: (c.messages || []).map(mapMsg),
  attachRequests: (c.attach_requests || []).map(mapAttachRequest),
})

// ── Agent colors ──

const AGENT_COLORS = [
  { bg: 'bg-accent', fg: 'text-accent-fg' },
  { bg: 'bg-ok', fg: 'text-ok-fg' },
  { bg: 'bg-warn', fg: 'text-warn-fg' },
  { bg: 'bg-info', fg: 'text-info-fg' },
  { bg: 'bg-danger', fg: 'text-danger-fg' },
]

const agentColor = (idx: number) => AGENT_COLORS[idx % AGENT_COLORS.length]

/**
 * Agent state and listen-mode badges.
 *
 * The copy sits behind a `get label()` rather than a plain string because both
 * tables are evaluated at module load, where an `i18nT()` call would freeze
 * whatever language was active at boot and never re-resolve on a language
 * switch. A getter runs on every property access — i.e. during render, at each of
 * the four call sites below — so it follows the language without any of them
 * changing shape. Each key is a literal at the `i18nT()` call, which is what lets
 * `scripts/check-i18n-keys.mjs` resolve it statically.
 *
 * Every label in both tables is converted, not just the ones the lint reports:
 * `eslint.i18n.config.js` exempts single lowercase words by shape (its stated
 * false-negative class #1), so only `working` — which carries a `●` glyph — is
 * actually flagged. Leaving its five siblings as literals would ship a badge row
 * that is half-translated in every locale.
 */
export const STATE_BADGE: Record<string, { variant: 'ok' | 'err' | 'warn'; label: ReactNode }> = {
  pending: {
    variant: 'warn',
    get label() {
      return (
        <>
          <Hourglass className="lucide-inline" /> {i18nT('pages.channelPage.state_pending')}
        </>
      )
    },
  },
  working: {
    variant: 'ok',
    get label() {
      return <>● {i18nT('pages.channelPage.state_working')}</>
    },
  },
  listening: {
    variant: 'ok',
    get label() {
      return (
        <>
          <Ear className="lucide-inline" /> {i18nT('pages.channelPage.state_listening')}
        </>
      )
    },
  },
  done: {
    variant: 'ok',
    get label() {
      return (
        <>
          <Check className="lucide-inline" /> {i18nT('pages.channelPage.state_done')}
        </>
      )
    },
  },
  failed: {
    variant: 'err',
    get label() {
      return (
        <>
          <X className="lucide-inline" /> {i18nT('pages.channelPage.state_failed')}
        </>
      )
    },
  },
  tool_running: {
    variant: 'ok',
    get label() {
      return (
        <>
          <Wrench className="lucide-inline" /> {i18nT('pages.channelPage.state_running')}
        </>
      )
    },
  },
}
const LISTEN_BADGE: Record<string, { variant: 'ok' | 'warn'; label: ReactNode }> = {
  all: {
    variant: 'ok',
    get label() {
      return (
        <>
          <Radio className="lucide-inline" /> {i18nT('pages.channelPage.listen_all')}
        </>
      )
    },
  },
  mention: {
    variant: 'warn',
    get label() {
      return (
        <>
          <Ear className="lucide-inline" /> {i18nT('pages.channelPage.listen_mention')}
        </>
      )
    },
  },
  silent: {
    variant: 'warn',
    get label() {
      return (
        <>
          <VolumeX className="lucide-inline" /> {i18nT('pages.channelPage.listen_silent')}
        </>
      )
    },
  },
}

// ── Components ──

export function AgentBadge({ agent, index }: { agent: ChannelAgent; index: number }) {
  const c = agentColor(index)
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[13px] font-medium ${c.bg} ${c.fg}`}
    >
      {agent.role}
    </span>
  )
}

/** Tool title embedded in a channel approval message by the backend
 * (`⚠️ Approval needed: **<name>**` + fenced input). Greedy up to the LAST
 * `**` before the input fence so a command containing `**` stays whole.
 * Empty when the message carries no name (legacy messages). The approval
 * card's TrustDropdown derives its trust_command / trust_base patterns from
 * this title, so it must be the tool's identity, never the agent role. */
export function approvalToolTitle(content: string): string {
  return /^⚠️ Approval needed: \*\*([\s\S]*)\*\*\n```/.exec(content)?.[1] || ''
}

/** What the approval card renders and which trust tiers it offers, from the
 *  message. With `meta` the answers are the SERVER's: it alone can refuse a
 *  per-command tier, and it posted its verdict beside the prose, so the card
 *  never offers a tier the endpoint would answer with `pattern_underivable`
 *  (a compound `cat f | wc -l` has no base to trust; a non-shell tool has no
 *  command). Without `meta` -- a message persisted before the field existed --
 *  the prose is parsed as before, unchanged, so those cards render as they
 *  did. Exported for the capture entries and the coverage tests. */
export function approvalCardProps(msg: Pick<ChannelMessage, 'content' | 'fromRole' | 'meta'>): {
  title: string
  toolInput: string
  hasCommand: boolean
  baseCommand?: string
  baseDerivable?: boolean
} {
  const meta = msg.meta
  if (meta) {
    return {
      title: meta.tool_title || msg.fromRole,
      toolInput: meta.tool_input ?? '',
      hasCommand: meta.command_grantable === '1',
      baseCommand: meta.base_command || undefined,
      baseDerivable: meta.base_derivable === '1',
    }
  }
  const title = approvalToolTitle(msg.content)
  const toolInput = msg.content.replace(/^⚠️ Approval needed:.*\n```\n?/, '').replace(/\n?```$/, '')
  const hasCommand = title.startsWith('Running: ') && !/\[REDACTED/.test(toolInput)
  return { title: title || msg.fromRole, toolInput, hasCommand }
}

/** Scope-accurate confirmations for the channel tiers: what a click just
 *  granted, in the words of the tier the reader chose. */
const CHANNEL_TRUSTED_LABEL_KEYS = {
  trust: 'components.approvalCard.trusted_channel_all',
  trust_command: 'components.approvalCard.trusted_channel_command',
  trust_base: 'components.approvalCard.trusted_channel_base',
} as const

/** Exported for the capture entries only, alongside `approvalToolTitle`: an
 *  approval's posted TEXT is rendered here, above the card, so a frame that
 *  mounts the card alone cannot show what a channel reader actually reads. */
export function MessageBubble({
  msg,
  agents,
  onReply,
  onOpenThread,
  onApprove,
}: {
  msg: ChannelMessage
  agents: ChannelAgent[]
  onReply?: () => void
  onOpenThread?: () => void
  onApprove?: (action: string, pattern?: string) => Promise<unknown>
}) {
  const isHuman = msg.fromId === 'human'
  const approvalMode = useAppSelector((s: RootState) => s.dashboard.approvalMode)
  const agentIdx = agents.findIndex((a) => a.id === msg.fromId)
  const c = isHuman ? null : agentColor(agentIdx >= 0 ? agentIdx : 0)
  const time = fmtDateFields(msg.timestamp, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })

  return (
    <div
      className={`flex gap-3 py-2 px-3 rounded-lg animate-rise group ${isHuman ? 'bg-accent/10' : 'hover:bg-bg-hover'}`}
    >
      {/* Avatar */}
      <div
        className={`w-8 h-8 rounded-full flex items-center justify-center text-[13px] font-bold shrink-0 ${isHuman ? 'bg-accent text-accent-fg' : `${c?.bg} ${c?.fg}`}`}
      >
        {isHuman ? <User className="lucide-inline" /> : msg.fromRole[0]}
      </div>
      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          <span className="text-sm font-semibold text-text-strong">{msg.fromRole}</span>
          {msg.mention && (
            <span className="text-[13px] text-muted">
              →{' '}
              {(Array.isArray(msg.mention) ? msg.mention : [msg.mention])
                .map((id) => '@' + (agents.find((a) => a.id === id)?.role || id))
                .join(', ')}
            </span>
          )}
          <span className="text-[13px] text-muted ml-auto">{time}</span>
        </div>
        {/* Per-message boundary, as the chat transcript has: a markdown render
            crash (an unmodeled nesting spelling past the depth bound, an
            unknown HTML element) degrades this one message instead of
            escalating to the app-level ErrorBoundary and replacing the view. */}
        <div className="text-sm text-text">
          {msg.msgType === 'approval' ? (
            <span className="whitespace-pre-wrap">{msg.content}</span>
          ) : (
            <MessageErrorBoundary rawContent={msg.content}>
              <MarkdownRenderer content={msg.content} />
            </MessageErrorBoundary>
          )}
        </div>
        {/* Approval card. The title is the tool name the backend embedded in
            the message — the TrustDropdown derives its trust_command /
            trust_base patterns from it, so the agent ROLE (fromRole) is only
            a fallback for legacy messages without a name. Per-command tiers
            are shell-only on channels (the endpoint refuses them for
            non-shell tools with pattern_underivable), so a non-shell card
            offers just Approve / blanket Trust / Reject. The channel tiers
            grant agent-scoped, until-restart trust (blanket trust is
            channel-wide and persisted), and their labels and confirmations
            say so: the chat keys would understate or overstate the grant. */}
        {msg.msgType === 'approval' && onApprove && (
          <div className="mt-2">
            <ApprovalCard
              {...approvalCardProps(msg)}
              showButtons={approvalMode === 'normal'}
              trustAllLabelKey="components.trustDropdown.trust_all_tools_channel"
              trustCommandLabelKey="components.trustDropdown.trust_this_command_channel"
              trustBaseLabelKey="components.trustDropdown.trust_all_base_channel"
              trustedLabelKeys={CHANNEL_TRUSTED_LABEL_KEYS}
              labelValues={{ role: msg.fromRole }}
              onApprove={onApprove}
            />
          </div>
        )}
        {/* Thread badge + reply */}
        <div className="flex items-center gap-2 mt-1">
          {msg.replyCount > 0 && (
            <Btn
              onClick={onOpenThread!}
              className="!p-0 !border-none !rounded-none text-[13px] text-accent hover:underline"
            >
              <MessageSquare className="lucide-inline" />{' '}
              {i18nT('pages.channelPage.reply_2', { count: msg.replyCount })}
            </Btn>
          )}
          {onReply && (
            <Btn onClick={onReply} className="!p-0 !border-none !rounded-none text-[13px] text-muted hover:text-text transition-opacity md:opacity-0 md:group-hover:opacity-100 [@media(hover:none)]:opacity-100">
              <MessageSquare className="lucide-inline" /> {i18nT('pages.channelPage.reply')}
            </Btn>
          )}
        </div>
      </div>
    </div>
  )
}

const LISTEN_MODES: Array<ChannelAgent['listenMode']> = ['all', 'mention', 'silent']

export function AgentControlRow({
  agent,
  onDismiss,
  onListenChange,
  onClearContext,
  onSetCoordinator,
}: {
  agent: ChannelAgent
  onDismiss: () => void
  onListenChange: (m: ChannelAgent['listenMode']) => void
  onClearContext: () => void
  onSetCoordinator: () => void
}) {
  const [menu, setMenu] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  // The trigger, so an explicit dismissal can hand focus back to it: the menu
  // keyboard contract moves focus INTO the menu on open, and the row holding
  // it is unmounted by the close — without a restore, focus would be orphaned
  // on <body>. Outside-click dismissal is left alone (the browser routes focus
  // per the click target), matching the MicSourceMenu posture.
  const triggerRef = useRef<HTMLButtonElement>(null)
  // The role="menu" element itself — narrower than `menuRef` (which wraps the
  // trigger too) so item discovery never picks up the trigger button.
  const menuListRef = useRef<HTMLDivElement>(null)
  const alive = agent.state !== 'done' && agent.state !== 'failed'

  // role="menu" promises the WAI-ARIA menu keyboard contract (arrow-key row
  // navigation with wrap, Home/End, Tab containment). The shared hook owns it
  // for all role="menu" surfaces rather than re-spelled here.
  // The rows are native <button>s (`Btn`), so the hook's item discovery finds
  // them with no extra markup. Escape stays owned by the dismiss effect below:
  // what "close" means here — menu state, focus restore — is this host's
  // business. Focus ENTRY is host-owned too (`focusFirstOnOpen: false`): this
  // menu is not portalled and sits inside the agents rail's scroll container,
  // so the hook's plain `.focus()` entry would scroll the rail on every open,
  // shifting the row the user just clicked out from under the pointer —
  // `preventScroll` keeps the rail still (arrow navigation still scrolls a
  // focused row into view, which is wanted).
  useMenuKeyboard({
    enabled: menu,
    containerRef: menuListRef,
    focusFirstOnOpen: false,
  })
  useEffect(() => {
    if (menu) menuItemsOf(menuListRef.current)[0]?.focus({ preventScroll: true })
  }, [menu])

  useEffect(() => {
    if (!menu) return
    const close = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(false)
    }
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setMenu(false)
        // Focus lives inside the menu at this point (focus entry on open, Tab
        // containment while open, and any outside mousedown already closed the
        // menu) — hand it back to the trigger before its row unmounts.
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', esc)
    }
  }, [menu])

  return (
    <div className="px-2 py-2 rounded-lg hover:bg-bg-hover group">
      <div className="flex items-start gap-2 min-w-0">
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-text break-words" title={agent.role}>
            {agent.role}
          </div>
          {agent.agentName && (
            <div className="text-[11px] text-muted font-mono break-all" title={agent.agentName}>
              {agent.agentName}
            </div>
          )}
        </div>
        <div className="flex items-center gap-1">
          {agent.isCoordinator && (
            <Badge variant="aim">{i18nT('pages.channelPage.coordinator')}</Badge>
          )}
          <Badge variant={STATE_BADGE[agent.state]?.variant || 'warn'}>
            {STATE_BADGE[agent.state]?.label || agent.state}
          </Badge>
        </div>
      </div>
      <div className="flex items-center justify-between gap-2 mt-1.5">
        <div className="relative inline-block" ref={menuRef}>
          <Btn
            ref={triggerRef}
            onClick={() => setMenu(!menu)}
            aria-haspopup="menu"
            aria-expanded={menu}
            className="!p-0 !border-none !rounded-none text-[13px] text-muted hover:text-text"
          >
            <Badge variant={LISTEN_BADGE[agent.listenMode]?.variant || 'warn'}>
              {LISTEN_BADGE[agent.listenMode]?.label || agent.listenMode}
            </Badge>
          </Btn>
          {menu && (
            <div
              role="menu"
              ref={menuListRef}
              aria-label={i18nT('pages.channelPage.listen_mode')}
              className="absolute top-full left-0 mt-1 bg-bg-elevated border border-border rounded-md shadow-lg z-10"
            >
              {LISTEN_MODES.map((m) => (
                <Btn
                  key={m}
                  role="menuitemradio"
                  aria-checked={m === agent.listenMode}
                  onClick={() => {
                    onListenChange(m)
                    setMenu(false)
                    // Activation is an explicit dismissal too: the row that has
                    // focus is being unmounted, so restore to the trigger rather
                    // than dropping focus on <body>.
                    triggerRef.current?.focus()
                  }}
                  className={`!rounded-none block w-full text-left px-3 py-1.5 text-[13px] !border-none ${m === agent.listenMode ? 'text-accent bg-accent/10' : 'text-text hover:bg-bg-hover'}`}
                >
                  <Badge variant={LISTEN_BADGE[m]?.variant || 'warn'}>
                    {LISTEN_BADGE[m]?.label || m}
                  </Badge>
                </Btn>
              ))}
            </div>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {!agent.isCoordinator && (
            <Btn
              onClick={onSetCoordinator}
              aria-label={i18nT('pages.channelPage.make_coordinator')}
              title={i18nT('pages.channelPage.make_coordinator')}
            >
              <Crown className="lucide-inline" />
            </Btn>
          )}
          {alive && (
            <Btn
              onClick={onClearContext}
              aria-label={i18nT('pages.channelPage.clear_context')}
              title={i18nT('pages.channelPage.clear_context')}
            >
              <RotateCcw className="lucide-inline" />
            </Btn>
          )}
          <Btn
            onClick={onDismiss}
            aria-label={i18nT('pages.channelPage.dismiss')}
            danger
            title={i18nT('pages.channelPage.dismiss')}
          >
            <X className="lucide-inline" />
          </Btn>
        </div>
      </div>
    </div>
  )
}
