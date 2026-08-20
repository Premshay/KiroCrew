/**
 * Rewind helper — wraps `api.rewind` with a rollback callback that fires on
 * failure. Separate from ChatPage so the success/failure branches can be
 * unit-tested without mounting the full chat page.
 */

import { api } from '../api/client'

/**
 * Call `/api/chat/slots/{slot}/rewind` and invoke `rollback` if the request
 * rejects. Logs a warning on failure (debug only).
 */
export async function rewindWithRollback(
  slot: string,
  ts: string,
  content: string,
  rollback: (reason: string) => void,
): Promise<void> {
  try {
    await api.rewind(slot, ts, content)
  } catch (e) {
    // The reason reaches the caller so it can reach the user. This path is how
    // a signed-out install presented as "the edit button is broken": the server
    // says exactly what is wrong (503 "Kiro CLI setup or sign-in is required"),
    // and discarding it left the edit reverted with nothing on screen.
    // eslint-disable-next-line no-console
    console.warn('rewind failed', e)
    rollback(e instanceof Error && e.message ? e.message : String(e))
  }
}
