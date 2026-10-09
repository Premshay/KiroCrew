import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * The header (with the pinned prompt) and the composer dock overlay the
 * transcript. On touch, a selection handle dragged under either one must stay in
 * the transcript rather than jump into the overlay's text and select everything
 * between, so both overlay roots are `select-none`. The composer's own textarea
 * opts back in so typing, selecting and editing a draft still work.
 */
const page = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')
const input = readFileSync(resolve(__dirname, '../components/ChatInput.tsx'), 'utf8')

describe('transcript overlays do not take a touch selection', () => {
  it('marks the header overlay root select-none', () => {
    expect(page).toMatch(/absolute top-0 left-0 right-1\.5 [^`]*pointer-events-none select-none/)
  })
  it('marks the composer dock root select-none', () => {
    expect(page).toMatch(/ref=\{dockRef\} className="[^"]*select-none[^"]*"[^>]*data-testid="composer-dock-root"/)
  })
  it('keeps the composer textarea selectable inside the dock', () => {
    expect(input).toMatch(/`relative block w-full select-text )
  })
})
