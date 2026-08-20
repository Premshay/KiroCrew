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
    expect(messageBody).toEqual({ message: 'Review this screen.', slot: 'dc-1', agent: 'crew-codex', memory_mode: 'temporary', mode: 'design-critique' })
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

  it('creates and updates reusable project context through the app API', async () => {
    let created: unknown
    let updated: unknown
    server.use(
      http.post('/api/apps/design-critique/contexts', async ({ request }) => {
        created = await request.json()
        return HttpResponse.json({ context: { id: 'atlas' } }, { status: 201 })
      }),
      http.patch('/api/apps/design-critique/contexts/atlas', async ({ request }) => {
        updated = await request.json()
        return HttpResponse.json({ context: { id: 'atlas' } })
      }),
    )

    await designCritiqueApi.createProjectContext({
      name: 'Atlas', repository: '/work/atlas', context_paths: ['AGENTS.md'], notes: 'Use the real workflow.',
    })
    await designCritiqueApi.updateProjectContext('atlas', { notes: 'Review this flow only.' })

    expect(created).toEqual({
      name: 'Atlas', repository: '/work/atlas', context_paths: ['AGENTS.md'], notes: 'Use the real workflow.',
    })
    expect(updated).toEqual({ notes: 'Review this flow only.' })
  })

  it('persists a Claude Design round separately from the critique chat lifecycle', async () => {
    let created: unknown
    let updated: unknown
    server.use(
      http.post('/api/apps/design-critique/design-rounds', async ({ request }) => {
        created = await request.json()
        return HttpResponse.json({ round: { id: 'round-1', status: 'prepared' } }, { status: 201 })
      }),
      http.patch('/api/apps/design-critique/design-rounds/round-1', async ({ request }) => {
        updated = await request.json()
        return HttpResponse.json({ round: { id: 'round-1', status: 'building' } })
      }),
    )

    await designCritiqueApi.createDesignRound({
      mode: 'generate-prototype', intent: 'ground', project_name: 'Atlas', repository: '/work/atlas',
      context_paths: 'docs/design-system.md', notes: 'Use existing outcome states.', target: 'Review queue',
      claude_design_url: 'https://claude.ai/design/p/atlas', handoff_path: 'docs/design/handoffs/queue',
      review_run_id: 'run-1', report: { overallRead: 'State is unclear.' },
    })
    await designCritiqueApi.updateDesignRound('round-1', { status: 'building' })

    expect(created).toMatchObject({ review_run_id: 'run-1', mode: 'generate-prototype', intent: 'ground' })
    expect(updated).toEqual({ status: 'building' })
  })
})
