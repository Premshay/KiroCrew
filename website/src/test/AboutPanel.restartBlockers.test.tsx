//
// Contract under test — Settings > About answers a refused update/restart with
// the SAME named blocker panel the sessions-reset button raises.
//
// The gateway refuses a coordinated reset while any live ACP session still owns
// a turn, and every endpoint that drains sessions says so the same way: 409 with
// `code: "restart_ack_required"`. `/api/update` is one of them, and this panel
// used to collapse that refusal into the generic ApiError branch — a red
// sentence naming nobody, offering nothing, and indistinguishable from a dirty
// working tree. The blocker panel names each channel worker and carries the one
// safe action over it.
//
// Two properties this file exists to pin, because both failed silently before:
// the ack refusal is tested BEFORE the generic 409 branch that would swallow it,
// and an ordinary 409 still reads as an error with no blocker read at all.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { setUpdateProgress, sseStatus } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

const ACK_BODY = {
  ok: false,
  code: 'restart_ack_required',
  maintenance: { active: true, ready: false, required: [], pending: [], unmanaged_busy: ['channel:c1:a1'] },
}

const BLOCKER_REPORT = {
  ok: true,
  maintenance: { active: true, ready: false, required: [], pending: [], unmanaged_busy: ['channel:c1:a1'] },
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
}

/** Route every request the panel makes; `applyStatus`/`applyBody` decide how
 *  the update apply is refused. `reads` records GET urls so a test can assert
 *  the blocker endpoint was (or was not) consulted. */
function stubFetch(opts: { applyStatus: number; applyBody: unknown }) {
  const reads: string[] = []
  const json = (body: unknown, status = 200) => ({
    ok: status < 400,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  vi.stubGlobal('fetch', vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      if (url.includes('/api/update')) return json(opts.applyBody, opts.applyStatus)
      return json({ ok: true })
    }
    reads.push(url)
    if (url.includes('/api/sessions/restart-blockers')) return json(BLOCKER_REPORT)
    if (url.includes('/api/update/check')) {
      return json({ update_available: true, can_apply: true, check_status: 'succeeded', changes: '' })
    }
    return json({})
  }))
  return reads
}

function mountWeb() {
  // No window.updateAPI => isDesktop false => the gateway branch renders.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

/** Open the confirm modal and press its apply. Returns the dialog. */
async function applyUpdate() {
  fireEvent.click(screen.getByRole('button', { name: /update now/i }))
  const dialog = await screen.findByRole('dialog')
  const apply = await within(dialog).findByRole('button', { name: /update now/i })
  await waitFor(() => expect(apply).not.toBeDisabled())
  fireEvent.click(apply)
  return dialog
}

describe('AboutPanel maintenance blockers', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    // A self-updatable install with an update pending: the only shape that
    // renders the Update button whose POST can come back ack-refused.
    store.dispatch(sseStatus({
      ...BLANK_STATUS, update_available: true, update_can_apply: true,
    } as never))
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('names the blocking workers when the barrier refuses the update', async () => {
    stubFetch({ applyStatus: 409, applyBody: ACK_BODY })
    mountWeb()

    const dialog = await applyUpdate()

    expect(await within(dialog).findByTestId('restart-blockers')).toBeInTheDocument()
    expect(within(dialog).getByText('Logs Agent')).toBeInTheDocument()
    expect(within(dialog).getByText('Incident 4821')).toBeInTheDocument()
    // The panel says it; the error line must not say it again in worse words.
    expect(screen.queryByTestId('gateway-restart-error')).not.toBeInTheDocument()
  })

  it('leaves an ordinary 409 as an error line and never reads the blockers', async () => {
    // A dirty working tree is the same status code and nothing here can act on
    // it, so the generic branch must still own it.
    const reads = stubFetch({ applyStatus: 409, applyBody: { error: 'zzq-dirty-tree' } })
    mountWeb()

    const dialog = await applyUpdate()

    expect(await within(dialog).findByText('zzq-dirty-tree')).toBeInTheDocument()
    expect(screen.queryByTestId('restart-blockers')).not.toBeInTheDocument()
    expect(reads.some(u => u.includes('/api/sessions/restart-blockers'))).toBe(false)
  })

  it('keeps the panel on the card once the dialog is dismissed', async () => {
    // Closing the dialog does not un-block the restart, and the standing
    // Restart control on the card is what the operator retries with — so the
    // panel has to follow them out of the modal rather than vanish with it.
    stubFetch({ applyStatus: 409, applyBody: ACK_BODY })
    mountWeb()

    const dialog = await applyUpdate()
    await within(dialog).findByTestId('restart-blockers')
    fireEvent.click(dialog)

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await screen.findByTestId('restart-blockers')).toBeInTheDocument()
  })

  it('leaves updating and shows blockers when a late barrier event arrives', async () => {
    // The apply request succeeded long ago, then a session started while the
    // update was building. The restart's only remaining reply path is the
    // structured update-progress event, not the original HTTP response.
    stubFetch({ applyStatus: 200, applyBody: { ok: true, status: 'updating' } })
    mountWeb()

    const dialog = await applyUpdate()
    await within(dialog).findByText('Updating — gateway restarting…')

    store.dispatch(setUpdateProgress({
      step: 'blocked',
      detail: 'restart waiting',
      maintenance: ACK_BODY.maintenance,
    }))

    expect(await within(dialog).findByTestId('restart-blockers')).toBeInTheDocument()
    await waitFor(() => expect(within(dialog).queryByText('Updating — gateway restarting…')).not.toBeInTheDocument())
    expect(within(dialog).getByRole('button', { name: /update now/i })).not.toBeDisabled()
  })

  it('drops the panel when the operator starts a fresh restart', async () => {
    // Retrying is the operator's call and the panel is a verdict about a moment
    // that has passed: leaving it up beside a running restart would report a
    // barrier nobody has re-read.
    stubFetch({ applyStatus: 409, applyBody: ACK_BODY })
    mountWeb()

    const dialog = await applyUpdate()
    await within(dialog).findByTestId('restart-blockers')
    fireEvent.click(dialog)
    await screen.findByTestId('restart-blockers')

    // Two presses: the standing control arms before it fires.
    const restart = screen.getByTestId('gateway-restart-standing')
    fireEvent.click(restart)
    fireEvent.click(restart)

    await waitFor(() => expect(screen.queryByTestId('restart-blockers')).not.toBeInTheDocument())
  })
})
