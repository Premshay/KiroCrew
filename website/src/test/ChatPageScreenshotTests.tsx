import { describe, expect, it, type Mock } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'

interface ScreenshotHarness {
  apiSpy: (name: string) => Mock
  renderTurn: () => Promise<unknown>
  getInputProps: () => { onScreenshot?: () => void } | null
}

export function registerScreenshotFailureTests({ apiSpy, renderTurn, getInputProps }: ScreenshotHarness) {
// A failed screen capture used to be discarded by a bare `catch {}` commented
// "user cancelled" -- but cancellation is NOT an error path: the route answers a
// cancelled capture with 200 `{"path": ""}`, which the caller's `if (path)`
// guard absorbs. So the only things that reached that catch were real failures
// (the 400 off macOS, the 120s capture timeout, the request never reaching the
// gateway), and the user saw nothing at all.
describe('ChatPage screen capture failures', () => {
  it('reports a failed capture instead of swallowing it', async () => {
    apiSpy('screenshot').mockRejectedValue(new Error('screenshot timed out'))
    await renderTurn()
    await waitFor(() => expect(getInputProps()?.onScreenshot).toBeTypeOf('function'))
    await act(async () => { getInputProps()!.onScreenshot!() })
    // The notice names the action, not just the transport text: a bare
    // "screenshot timed out" above the composer tells the user nothing about
    // which click failed.
    expect(await screen.findByText('Screenshot failed: screenshot timed out')).toBeInTheDocument()
  })

  it('stays silent when the user cancels, which is not a failure', async () => {
    // The cancelled shape: HTTP 200, empty path. No notice, no attachment.
    apiSpy('screenshot').mockResolvedValue({ path: '' })
    await renderTurn()
    await waitFor(() => expect(getInputProps()?.onScreenshot).toBeTypeOf('function'))
    await act(async () => { getInputProps()!.onScreenshot!() })
    expect(screen.queryByText(/unknown error/i)).not.toBeInTheDocument()
  })
})
}
