import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import ScopeBuilder from './ScopeBuilder'
import type { ReviewBrief } from './types'

const brief: ReviewBrief = {
  contextId: '',
  projectName: 'Atlas',
  repository: '/work/atlas',
  contextPaths: 'AGENTS.md',
  notes: 'Preserve the current product language.',
  targets: '',
  intent: 'ground',
}

describe('ScopeBuilder', () => {
  it('keeps reusable project context separate from the review target', () => {
    const onChange = vi.fn()
    const onSaveContext = vi.fn()
    render(
      <ScopeBuilder
        contexts={[]}
        brief={brief}
        busy={false}
        onChange={onChange}
        onSelectContext={vi.fn()}
        onSaveContext={onSaveContext}
        onDeleteContext={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByLabelText('Review target'), {
      target: { value: 'The operator handoff process and its empty state.' },
    })
    fireEvent.change(screen.getByLabelText('Review intent'), { target: { value: 'invent' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save project context' }))

    expect(onChange).toHaveBeenCalledWith({ targets: 'The operator handoff process and its empty state.' })
    expect(onChange).toHaveBeenCalledWith({ intent: 'invent' })
    expect(onSaveContext).toHaveBeenCalledOnce()
  })
})
