/**
 * /notifications?id=<ts> deep-link contract.
 *
 * A push notification's tap-through lands on the full page with the note's
 * store id (its ts — the same key the ack/delete APIs use) in the `id` query
 * param. The page must open that notification's detail as if the row were
 * tapped (so it acks), fetch the list when the deep link lands on a cold
 * store, and degrade to the plain page when the id matches nothing even in a
 * fresh fetch (expired or cleared away) — never crash, never hang on a
 * missing note.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationsPage from '../pages/NotificationsPage'
import { api } from '../api/client'
import type { RootState } from '../store'
import type { Notification } from '../types'

let mobile = false
vi.mock('../hooks/useIsMobile', () => ({
  useIsMobile: () => mobile,
}))

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    taskRunToChat: vi.fn().mockResolvedValue({}),
    resolveApproval: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

// A real bus ts: microsecond ISO-8601 with an offset, so the test exercises
// the characters (+, :) that must survive the query param round-trip encoded.
const TS = '2026-08-07T10:00:00.123456+00:00'
const note: Notification = {
  kind: 'cron', ts: TS, title: 'Cron Result', body: 'cron body text', acked: false,
}

function stateWith(notifs: Notification[]): Partial<RootState> {
  return { notifications: { items: notifs } as RootState['notifications'] }
}

const routeFor = (id: string) => `/notifications?id=${encodeURIComponent(id)}`

beforeEach(() => {
  localStorage.clear()
  mobile = false
  vi.mocked(api.notifications).mockClear().mockResolvedValue({ notifications: [] })
  vi.mocked(api.ackNotification).mockClear()
})

describe('NotificationsPage ?id= deep link', () => {
  it('desktop: opens the detail for the linked notification and acks it', async () => {
    const store = createTestStore(stateWith([note]))
    renderWithProviders(<NotificationsPage />, { store, route: routeFor(TS) })

    // Detail panel open: its Close control exists and the no-selection empty
    // state is gone.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Close/ })).toBeInTheDocument()
    })
    expect(screen.queryByText('Select a notification')).not.toBeInTheDocument()
    // Same path as tapping the row: the note acks.
    await waitFor(() => {
      expect(store.getState().notifications.items[0].acked).toBe(true)
    })
    expect(api.ackNotification).toHaveBeenCalledWith(TS)
    // Warm store: the id resolved locally, so no immediate confirming GET
    // fires (the reconciling settle fetch runs seconds later, past this
    // test's lifetime).
    expect(api.notifications).not.toHaveBeenCalled()
  })

  it('mobile: opens the full-width detail view directly', async () => {
    mobile = true
    const store = createTestStore(stateWith([note]))
    renderWithProviders(<NotificationsPage />, { store, route: routeFor(TS) })

    // The mobile detail card is identified by its Back button (the feed is
    // hidden while a note is selected).
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()
    })
  })

  it('cold store: fetches the list and then opens the linked detail', async () => {
    vi.mocked(api.notifications).mockResolvedValue({ notifications: [note] })
    const store = createTestStore()
    renderWithProviders(<NotificationsPage />, { store, route: routeFor(TS) })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Close/ })).toBeInTheDocument()
    })
    expect(api.notifications).toHaveBeenCalled()
  })

  it('expired id: renders the plain page without crashing', async () => {
    vi.mocked(api.notifications).mockResolvedValue({ notifications: [note] })
    const store = createTestStore(stateWith([note]))
    renderWithProviders(<NotificationsPage />, {
      store, route: routeFor('2026-01-01T00:00:00.000000+00:00'),
    })

    // The confirmed miss leaves the page in its normal no-selection state.
    await waitFor(() => {
      expect(api.notifications).toHaveBeenCalled()
    })
    expect(screen.getByText('Select a notification')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Close/ })).not.toBeInTheDocument()
    expect(api.ackNotification).not.toHaveBeenCalled()
  })

  it('no param: nothing selected, no fetch beyond the page default', () => {
    const store = createTestStore(stateWith([note]))
    renderWithProviders(<NotificationsPage />, { store, route: '/notifications' })

    expect(screen.getByText('Select a notification')).toBeInTheDocument()
    expect(api.notifications).not.toHaveBeenCalled()
  })
})
