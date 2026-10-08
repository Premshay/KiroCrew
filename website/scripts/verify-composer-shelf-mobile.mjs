/** Check the shipped composer shelf's hit targets at phone widths. */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const SLOT = 'composer-shelf-mobile'
const PROJECT = '/home/user/work/Atlas'
const out = process.argv[2]
if (out) mkdirSync(out, { recursive: true })

const slots = [{
  key: SLOT, title: 'Composer shelf', running: false, messages: 2,
  last_message: 'Ready', agent: 'kirocrew', model: 'auto', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 2, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: 1, content: 'Check the mobile composer.' },
    { role: 'assistant', ts: 2, content: 'Ready.' },
  ],
  context_pct: 51, context_used_tokens: 102_000, context_window_tokens: 200_000,
}

const { srv, base } = await serveDist(process.env.COMPOSER_VERIFY_DIST)
const browser = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
    : undefined,
)
try {
  for (const width of [390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 800 }, deviceScaleFactor: 2 })
    try {
      const page = await context.newPage()
      const errors = []
      page.on('pageerror', error => errors.push(error.message))
      await stubDashboardApi(page, {
        slots,
        extra: async (path, route) => {
          if (path === '/api/project/git') return json(route, { path: PROJECT, repo: true, branch: 'main' }), true
          if (path === '/api/project/git/status') return json(route, {
            repo: true, branch: 'main', files: [{ path: 'README.md', status: 'modified', staged: false }],
            ahead: 0, behind: 0,
          }), true
          if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
          return false
        },
      })
      await page.goto(`${base}/chat?sid=${SLOT}`, { waitUntil: 'domcontentloaded' })
      const shelf = page.getByTestId('composer-context-shelf')
      await shelf.getByRole('button', { name: /Copy branch name main/ }).waitFor()
      await shelf.getByRole('status').waitFor()
      if (errors.length) throw new Error(`${width}px page error: ${errors[0]}`)
      const geometry = await shelf.evaluate(el => {
        const project = el.querySelector('button[aria-label^="Project:"]')
        const branch = el.querySelector('button[aria-label^="Copy branch name"]')
        const badge = el.querySelector('[role="status"]')
        const icon = project?.querySelector('svg')
        if (!project || !branch || !badge || !icon) throw new Error('Missing shelf control')
        const boxes = [project, branch, badge].map(node => node.getBoundingClientRect())
        const glyph = icon.getBoundingClientRect()
        return {
          widths: boxes.map(box => box.width),
          gaps: [boxes[1].left - boxes[0].right, boxes[2].left - boxes[1].right],
          iconInside: glyph.left >= boxes[0].left && glyph.right <= boxes[0].right,
        }
      })
      if (geometry.widths.some(value => value <= 0) || geometry.gaps.some(value => value < -0.5) || !geometry.iconInside) {
        throw new Error(`${width}px shelf controls overlap: ${JSON.stringify(geometry)}`)
      }
      if (out) await page.screenshot({ path: `${out}/composer-shelf-${width}.png` })
      console.log(`${width}px shelf: ${JSON.stringify(geometry)}`)
    } finally {
      await context.close()
    }
  }
} finally {
  await browser.close()
  await new Promise((resolve, reject) => srv.close(error => error ? reject(error) : resolve()))
}
