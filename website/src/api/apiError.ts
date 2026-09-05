/**
 * The typed API error, its message extraction, and a Response -> error factory.
 *
 * Split out of `api/client.ts` so an APP can import it. `client.ts` is ~3.5k
 * lines and its module graph pulls in `queryClient`, `installApiTransport`,
 * artifact-write bookkeeping and the error journal — side effects a standalone
 * app has no business importing just to name an error type. Apps that already
 * import `client.ts` for other reasons are unaffected; the three that do not
 * (design-tweak, design-critique, mochi) stay independent of it.
 *
 * `client.ts` re-exports `ApiError` and `friendlyErrText`, so every existing
 * import path — and every test that mocks `../api/client` — keeps working.
 */
import { i18nT } from '../i18n/t'

/**
 * A failed API call, carrying the HTTP status so callers can branch on specific
 * codes (e.g. 404 = not found, 409 = conflict) without regex-matching the error
 * message text.
 *
 * Extends Error so existing `e instanceof Error ? e.message : String(e)`
 * fallbacks keep working.
 */
export class ApiError extends Error {
  readonly status: number
  /** The raw response body, kept so a caller can read structured fields that
   * `friendlyErrText` collapses away when it unwraps the human message. */
  readonly body: string
  /** The gateway rejected this call because the dashboard session no longer
   * authenticates (403 + `X-Auth-Required`). Call sites branch on this to drop
   * retry affordances that cannot succeed until the user re-authenticates. */
  readonly authRequired: boolean
  constructor(status: number, message: string, body = '', authRequired = false) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
    this.authRequired = authRequired
  }
}

/**
 * The `error` field of a JSON error envelope, or '' when the body is not JSON
 * or carries no such field. Deliberately narrower than the unwrap below, which
 * also accepts `detail`/`message`: this one is used to tell a Kiro Crew refusal
 * apart from an edge throttle, and `message` is exactly the field the edge sets.
 */
const errorFieldOf = (body: string): string => {
  const trimmed = body.trim()
  if (!trimmed.startsWith('{')) return ''
  try {
    const msg = (JSON.parse(trimmed) as { error?: unknown })?.error
    return typeof msg === 'string' && msg.trim() ? msg : ''
  } catch { return '' }
}

/**
 * Map raw edge/proxy error bodies to a human-readable message. A dashboard
 * served through Builder Tunnels sits behind API Gateway, whose throttle
 * response is the opaque `{"message":"Rate exceeded","throttlingReasons":null}`
 * — rendering that verbatim in an error card is a terrible UX.
 *
 * The retry ladder in api/queryClient.ts absorbs edge throttles BEFORE this
 * message is reached, but only for calls that go through React Query. Direct
 * `post`/`put`/`del` handlers (channel create among them) have no retry at all,
 * so for those this text is the FIRST thing the operator sees rather than the
 * last resort — which is why it must not be shown unless the edge really is
 * what answered.
 */
export const friendlyErrText = (status: number, body: string): string => {
  if (status === 429) {
    // 429 is NOT only the edge. Kiro Crew answers its own capacity refusals
    // with it too — the channel cap, the live-slot cap, the fork cap, the auth
    // throttle — and every one of those carries an actionable {"error": "…"}
    // saying what to do ("Channel limit reached. Close an existing channel
    // first."). Returning the tunnel text on the status code alone threw that
    // away and sent the operator after a network problem they did not have.
    //
    // `error` is the discriminator, not a guess: every Kiro Crew refusal uses
    // that field, while API Gateway's throttle body is `{"message":"Rate
    // exceeded","throttlingReasons":null}` — no `error`, and its `message` is
    // the opaque string this function exists to replace. So an `error` present
    // means the gateway answered and the edge did not.
    const serverMsg = errorFieldOf(body)
    if (serverMsg) return serverMsg
    return i18nT('api.client.rate_limited_by_the_tunnel_edge_http_429_too_man')
  }
  // Backends return errors as {"error": "…"} (or detail/message). Unwrap the
  // field so the UI shows the human message with its real newlines, not the
  // raw JSON envelope with escaped \n and \".
  const trimmed = body.trim()
  if (trimmed.startsWith('{')) {
    try {
      const parsed = JSON.parse(trimmed)
      const msg = parsed?.error ?? parsed?.detail ?? parsed?.message
      if (typeof msg === 'string' && msg.trim()) return msg
    } catch { /* not JSON — fall through to raw body */ }
  }
  return body
}

/**
 * Read a failed `Response` and build the {@link ApiError} for it.
 *
 * For app API clients, which each used to do
 * `throw new Error(body || \`HTTP ${status}\`)` — that put the raw wire body in
 * the message, so a refusal rendered as JSON in the UI and no caller could
 * branch on the status at all.
 *
 * The body is read ONCE and tolerates a read failure (`.catch`), because a
 * refusal mid-stream must still produce the status rather than an unrelated
 * throw. The header read is equally defensive (`?.`): this function runs on the
 * failure path, so anything it throws REPLACES the real error with a misleading
 * one — a `Response`-like object without `headers` would surface "Cannot read
 * properties of undefined" where the backend had said "path is outside the
 * allowed roots". The `HTTP <status>` fallback is what the previous bare-Error
 * calls used for an empty body, so an empty-body refusal reads exactly as it did
 * before.
 *
 * Deliberately does NOT journal to `utils/errorReport` and does NOT raise the
 * stale-owner prompt, both of which `client.ts::apiFailure` does on top of this.
 * Those are dashboard-session concerns tied to the dashboard's own error banner,
 * and they are the side effects this module exists to keep out of app bundles.
 * An app that wants them can call the dashboard client instead.
 */
export async function toApiError(r: Response): Promise<ApiError> {
  const body = await r.text().catch(() => '')
  const authRequired = r.status === 403 && r.headers?.get?.('X-Auth-Required') === 'true'
  const message = friendlyErrText(r.status, body) || `HTTP ${r.status}`
  return new ApiError(r.status, message, body, authRequired)
}
