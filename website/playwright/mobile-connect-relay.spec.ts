import { test, expect } from '@playwright/test'
import { json, stubDashboardApi } from '../scripts/lib/stub-dashboard-api.mjs'

test.use({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })

test('unmanaged relay uses the configured-address phone sign-in flow', async ({ page, baseURL }, info) => {
  let linkMints = 0
  let tailnetMints = 0
  await stubDashboardApi(page, {
    localStorageEntries: { 'mc-lang': 'en', 'mc-active-slot': 'mobile-relay-fixture', 'mc-active-slot-chat': 'mobile-relay-fixture' },
    slots: [{ key: 'mobile-relay-fixture', title: 'Phone access', running: false,
      messages: 1, agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
      modified: 1793980800, source_links: [], source_links_total: 0 }],
    extra: async (path, route) => {
      if (path === '/api/chat/slots/mobile-relay-fixture') {
        await json(route, { messages: [{ role: 'assistant', ts: 1793980800, content: 'Phone connection fixture ready.' }],
          running: false, queue: [], total: 1, has_more: false, project: '' })
      } else if (path === '/api/mobile-connect/methods') {
        await json(route, { methods: [
          { id: 'tailnet_qr', kind: 'tailnet_qr' },
          { id: 'login_link', kind: 'login_link' },
        ] })
      } else if (path === '/api/tailnet/mobile') {
        await json(route, { step: 'occupied' })
      } else if (path === '/api/auth/mobile-link') {
        linkMints += 1
        await json(route, { url: 'https://gateway.example/?token=fixture', expires_in: 300 })
      } else if (path === '/api/tailnet/mobile/qr') {
        tailnetMints += 1
        await json(route, { error: 'This fixture must not use the managed Serve mint' }, 409)
      } else return false
      return true
    },
  })
  await page.goto(`${baseURL!}/?slot=mobile-relay-fixture`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByText('Phone connection fixture ready.', { exact: true })).toBeVisible()
  const connect = page.getByRole('button', { name: 'Connect your phone', exact: true })
  await connect.click()
  await expect(page.getByText(/^Create a sign-in link to scan/)).toBeVisible()
  await expect(page.getByText(/Tailscale setup is not complete yet/)).toHaveCount(0)
  expect(linkMints).toBe(0)
  expect(tailnetMints).toBe(0)
  await page.screenshot({ path: info.outputPath('mobile-relay-connect.png') })
  await page.getByRole('button', { name: 'Create sign-in link', exact: true }).click()
  await expect(page.getByRole('textbox', { name: 'Sign-in link', exact: true }))
    .toHaveValue('https://gateway.example/?token=fixture')
  expect(linkMints).toBe(1)
  expect(tailnetMints).toBe(0)
})
