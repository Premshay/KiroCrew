import { useState, useCallback, useEffect, useRef } from 'react'
import { ArrowLeft } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { useIsMobile } from '../hooks/useIsMobile'
import { useAppSelector, useAppDispatch } from '../store'
import { ackNotification, fetchNotifications } from '../store/notificationsSlice'
import { PageHeader, StatCard, Card, CardTitle, EmptyState } from '../components/ui'
import InfoTip from '../components/InfoTip'
import NotificationFeed from '../components/notifications/NotificationFeed'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import type { Notification } from '../types'

import { i18nT } from '../i18n/t'

/** Delay before the one-shot reconciling fetch after a deep-link auto-ack.
 *  Long enough for the boot-time list fetches (app shell, WS connect, this
 *  page's cold-store fetch) to have resolved — any of them snapshots the
 *  list before the ack lands and would otherwise overwrite the optimistic
 *  acked flag with its stale copy. */
const DEEP_LINK_ACK_SETTLE_MS = 3000

/**
 * Full Notifications page (route /notifications). Page chrome + master/detail
 * layout only; the feed (filter/list) and detail view are the same shared
 * components rendered by the topbar bell popover, so behavior stays identical
 * in both surfaces. This page owns the selection state and stat cards.
 */
export default function NotificationsPage() {
  const dispatch = useAppDispatch()
  const items = useAppSelector(s => s.notifications.items)
  const [selectedTs, setSelectedTs] = useState<string | null>(null)
  const isMobile = useIsMobile()

  const unread = items.filter(n => !n.acked).length
  const byCat = useCallback((k: string) => items.filter(n => n.kind === k).length, [items])
  // Derived from items so deleting/clearing the selected notification clears the
  // detail automatically (no separate selection bookkeeping needed).
  const selected = items.find(n => n.ts === selectedTs) || null

  // Auto-ack on select
  const handleSelect = useCallback((n: Notification) => {
    setSelectedTs(n.ts)
    if (!n.acked) dispatch(ackNotification(n.ts))
  }, [dispatch])

  // Deep link: /notifications?id=<ts> opens that notification's detail (push
  // taps land here — the note's ts is its store id, the same key ack/delete
  // use). Selection goes through handleSelect so the note acks exactly as a
  // tapped row does. The param is consumed once handled so back/refresh
  // behave as the plain page.
  const [params, setParams] = useSearchParams()
  const deepLinkTs = params.get('id')
  const clearDeepLink = useCallback(() => {
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('id')
      return next
    }, { replace: true })
  }, [setParams])
  // Shared with the fetch effect below: an id resolved (or already fetched
  // for) needs no confirming GET.
  const fetchedForTs = useRef<string | null>(null)
  // Lives outside the select effect's cleanup: clearing the param re-runs
  // that effect, and a per-run cleanup would cancel the timer immediately.
  const settleTimer = useRef<number | null>(null)
  useEffect(() => () => {
    if (settleTimer.current != null) window.clearTimeout(settleTimer.current)
  }, [])
  useEffect(() => {
    if (!deepLinkTs) return
    const match = items.find(n => n.ts === deepLinkTs)
    if (!match) return
    fetchedForTs.current = deepLinkTs
    handleSelect(match)
    clearDeepLink()
    // Boot-time fetches whose snapshots predate the auto-ack can resolve
    // after it and revert the acked flag; one re-fetch after they settle
    // shows server truth (including a deliberate un-ack made meanwhile).
    settleTimer.current = window.setTimeout(() => {
      dispatch(fetchNotifications())
    }, DEEP_LINK_ACK_SETTLE_MS)
    // After the selected state paints, bring the feed row into view (desktop:
    // the feed pane scrolls independently; mobile hides the feed, so there is
    // no row to scroll and the query matches nothing).
    requestAnimationFrame(() => {
      const row = document.querySelector(`[data-notif-ts="${CSS.escape(deepLinkTs)}"]`)
      if (row instanceof HTMLElement && typeof row.scrollIntoView === 'function') {
        row.scrollIntoView({ block: 'center' })
      }
    })
  }, [deepLinkTs, items, handleSelect, clearDeepLink, dispatch])
  // A deep link that did not match above usually landed on a cold tab whose
  // store is still empty, so fetch once per id. Only a CONFIRMED miss (fresh
  // list without the id — expired or cleared away) drops the param; a failed
  // fetch keeps it so a later WS-driven store update can still match.
  useEffect(() => {
    if (!deepLinkTs || fetchedForTs.current === deepLinkTs) return
    fetchedForTs.current = deepLinkTs
    dispatch(fetchNotifications()).then(res => {
      if (fetchNotifications.fulfilled.match(res) &&
          !res.payload.some(n => n.ts === deepLinkTs)) clearDeepLink()
    })
  }, [deepLinkTs, dispatch, clearDeepLink])

  return (
    <>
      <PageHeader title={i18nT('pages.notificationsPage.notifications')} subtitle={i18nT('pages.notificationsPage.all_agent_activity_cron_results_webhooks_and_app')} />
      {/* Desktop height-locks the master/detail split so feed and detail scroll
          as independent panes. On mobile the split collapses to one column and
          the stat grid stacks several rows tall, so height-locking would pin
          the feed/detail to the sliver left under the grid; the page scrolls as
          a whole instead (the standard page skeleton). */}
      <div className={`pb-8 flex-1 min-h-0 flex flex-col ${isMobile ? 'px-2 overflow-y-auto' : 'px-6 overflow-hidden'}`}>
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(120px,1fr))] mb-4 shrink-0">
          <StatCard label={i18nT('pages.notificationsPage.total')} value={items.length} accent />
          <StatCard label={i18nT('pages.notificationsPage.unread')} value={unread} />
          <StatCard label={i18nT('pages.notificationsPage.cron')} value={byCat('cron')} />
          <StatCard label={i18nT('pages.notificationsPage.hooks')} value={byCat('hook')} />
          <StatCard label={i18nT('pages.notificationsPage.heartbeat')} value={byCat('heartbeat')} />
        </div>

        {/* Split layout: feed + detail */}
        <div className={`flex gap-4 ${isMobile ? '' : 'flex-1 min-h-0'}`}>
          {/* Left: feed */}
          <div className={`flex flex-col shrink-0 ${isMobile ? 'w-full' : 'min-w-[320px] max-w-[420px] w-[40%]'} ${isMobile && selected ? 'hidden' : ''}`}>
            <Card className="flex flex-col flex-1 min-h-0">
              <CardTitle>{i18nT('pages.notificationsPage.activity_feed')} <InfoTip text={i18nT('pages.notificationsPage.click_a_notification_to_view_details_jump_to_the')} /></CardTitle>
              <NotificationFeed selectedTs={selectedTs} onSelect={handleSelect} />
            </Card>
          </div>

          {/* Right: detail panel */}
          {isMobile && selected ? (
            <div className="flex-1 min-w-0">
              {/* Natural height: the page scrolls on mobile, so the detail body
                  grows instead of inner-scrolling a clipped pane. p-0: the
                  panel brings its own px-5; stacked with Card's p-5 and the
                  page gutter it squeezed body text to ~260px on a 390px
                  screen. */}
              <Card className="flex flex-col p-0 overflow-hidden">
                <button className="flex items-center gap-1 px-2 py-1.5 text-[13px] text-muted hover:text-text cursor-pointer bg-transparent border-none mb-1" onClick={() => setSelectedTs(null)}>
                  <ArrowLeft size={14} /> {i18nT('pages.notificationsPage.back')}
                </button>
                <NotificationDetailPanel key={selected.ts} n={selected} onClose={() => setSelectedTs(null)} />
              </Card>
            </div>
          ) : !isMobile && <div className="flex-1 min-w-0">
            {selected ? (
              <Card className="flex flex-col h-full min-h-0">
                <NotificationDetailPanel key={selected.ts} n={selected} onClose={() => setSelectedTs(null)} />
              </Card>
            ) : (
              <Card className="flex items-center justify-center h-full">
                <EmptyState icon={<ArrowLeft className="lucide-inline" />} title={i18nT('pages.notificationsPage.select_a_notification')} subtitle={i18nT('pages.notificationsPage.click_any_item_to_view_details_and_navigate_to_i')} />
              </Card>
            )}
          </div>}
        </div>
      </div>
    </>
  )
}
