import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, __resetAuthRecoveryStateForTests } from '../../api/client'
import { __resetErrorJournalForTests, reportForError } from '../../utils/errorReport'
import { requestJson } from './TestEnvironmentPanel'

beforeEach(() => {
  __resetAuthRecoveryStateForTests()
  __resetErrorJournalForTests()
})

afterEach(() => vi.unstubAllGlobals())

it('keeps a failed environment request attached to its endpoint and method', async () => {
  const error = new TypeError('Failed to fetch')
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(error))

  await expect(requestJson('/environment/check', { fingerprint: 'ruler' })).rejects.toBe(error)
  expect(reportForError(error)).toMatchObject({
    endpoint: '/api/apps/auto-improvement/environment/check',
    method: 'POST',
    code: 'network',
  })
})

it.each([
  ['/repository/survey', 'GET'],
  ['/repository/assessment?areaPath=src%2Fapp', 'GET'],
  ['/repository/assessment/stop', 'POST'],
])('journals an unanswered %s request', async (path, method) => {
  const error = new TypeError('Failed to fetch')
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(error))

  await expect(api.autoImprovementResponse(path, { method })).rejects.toBe(error)
  expect(reportForError(error)).toMatchObject({
    endpoint: '/api/apps/auto-improvement' + path.split('?')[0],
    method,
    code: 'network',
  })
})

it('preserves a refused environment check diagnostic', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ ok: false, diagnostic: { stderr: 'Runner is unavailable' } }),
    { status: 409, headers: { 'Content-Type': 'application/json' } },
  )))

  await expect(requestJson('/environment/check', {})).rejects.toThrow('Runner is unavailable')
})
