/**
 * Composer shelf: the context readout and model chip never paint over the agent
 * and project chips. Groups retain their intrinsic width inside one scroller.
 * happy-dom pins the layout contract; verify-composer-shelf-mobile.mjs checks
 * actual geometry and activation after scrolling in Chromium.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

vi.mock('../api/client', () => ({ api: {} }))

async function renderShelf() {
  await act(async () => {
    renderWithProviders(
      <ChatInput
        value="" onChange={vi.fn()} onSend={vi.fn()} connected
        onAgentClick={vi.fn()} agentName="kirocrew"
        onProjectClick={vi.fn()} projectBranch="main"
        onModelClick={vi.fn()} modelName="claude-opus-5.5" reasoningEffort="high" hasEffort
        contextPct={53} contextUsedTokens={524800} contextWindowTokens={1000000}
        showContextPct showContextTokens
      />,
    )
  })
  const agentChip = screen.getByRole('button', { name: 'Agent: kirocrew' })
  return {
    agentChip,
    projectChip: screen.getByTestId('composer-project-chip'),
    chipGroup: agentChip.parentElement as HTMLElement,
    rightGroup: screen.getByTestId('composer-model-chip').parentElement as HTMLElement,
  }
}

describe('ChatInput — shelf chips keep their room', () => {
  it('keeps controls at their usable width inside a single scroll row', async () => {
    // No ResizeObserver in happy-dom: the shelf stays in its wide band.
    const { agentChip, projectChip, chipGroup, rightGroup } = await renderShelf()

    expect(rightGroup).toHaveClass('shrink-0')
    expect(chipGroup).toHaveClass('flex-auto', 'shrink-0', 'gap-2')
    const scroller = screen.getByTestId('composer-shelf-scroll')
    expect(scroller).toHaveClass('overflow-x-auto')
    expect(scroller).toContainElement(chipGroup)
    expect(scroller).toContainElement(rightGroup)
    expect(screen.getByTestId('composer-context-shelf')).not.toHaveClass('relative', 'edge-fade-x')
    expect(chipGroup).toContainElement(projectChip)
    for (const chip of [agentChip, projectChip]) {
      expect(chip).toHaveClass('px-2.5', 'gap-1.5')
      expect(chip.querySelector('svg')).toHaveAttribute('width', '13')
    }
  })

  it('collapses labels without shrinking the controls on a narrow shelf', async () => {
    const observers = new Map<Element, ResizeObserverCallback>()
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element) { observers.set(target, this.callback) }
      unobserve(target: Element) { observers.delete(target) }
      disconnect() {}
    })
    try {
      await renderShelf()
      const shelf = screen.getByTestId('composer-context-shelf')
      act(() => observers.get(shelf)?.([{
        target: shelf, contentRect: { width: 320, height: 32 },
      } as ResizeObserverEntry], {} as ResizeObserver))
      const agentChip = screen.getByRole('button', { name: 'Agent: kirocrew' })
      const projectChip = screen.getByTestId('composer-project-chip')
      const chipGroup = agentChip.parentElement as HTMLElement
      expect(chipGroup).toHaveClass('flex-auto', 'shrink-0', 'gap-2')
      expect(projectChip).not.toHaveTextContent('Project')
      expect(chipGroup).toContainElement(projectChip)
      for (const chip of [agentChip, projectChip]) {
        expect(chip).toHaveClass('px-2.5')
        expect(chip.querySelector('svg')).toHaveAttribute('width', '13')
      }
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
