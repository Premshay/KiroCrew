import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../../integration/mocks/server'
import { designCritiqueApi } from './api'

describe('designCritiqueApi agent selection', () => {
  it('uses the selected agent for the slot and every message sent to it', async () => {
    let slotBody: unknown
    let messageBody: unknown
    server.use(
      http.post('/api/chat/slots', async ({ request }) => {
        slotBody = await request.json()
        return HttpResponse.json({ key: 'dc-1' })
      }),
      http.post('/api/chat', async ({ request }) => {
        messageBody = await request.json()
        return new HttpResponse(null, { status: 204 })
      }),
    )

    await expect(designCritiqueApi.openSlot('crew-codex')).resolves.toEqual({ key: 'dc-1' })
    await expect(designCritiqueApi.send('dc-1', 'crew-codex', 'Review this screen.')).resolves.toBeUndefined()

    expect(slotBody).toMatchObject({ agent: 'crew-codex', memory_mode: 'temporary', mode: 'design-critique' })
    expect(messageBody).toEqual({ message: 'Review this screen.', slot: 'dc-1', agent: 'crew-codex', memory_mode: 'temporary' })
  })

  it('persists the critique lifecycle through the app-owned run API', async () => {
    let created: unknown
    let updated: unknown
    server.use(
      http.post('/api/apps/design-critique/runs', async ({ request }) => {
        created = await request.json()
        return HttpResponse.json({ run: { id: 'run-1' } }, { status: 201 })
      }),
      http.patch('/api/apps/design-critique/runs/run-1', async ({ request }) => {
        updated = await request.json()
        return HttpResponse.json({ run: { id: 'run-1', status: 'completed' } })
      }),
    )

    await designCritiqueApi.createReviewRun({
      slot_key: 'dc-1', agent: 'crew-codex', model: 'auto', stage: 'analyzing',
      source: { kind: 'screenshots' }, screens: [],
    })
    await designCritiqueApi.updateReviewRun('run-1', { status: 'completed', stage: 'report' })

    expect(created).toMatchObject({ slot_key: 'dc-1', agent: 'crew-codex', model: 'auto' })
    expect(updated).toEqual({ status: 'completed', stage: 'report' })
  })
})
