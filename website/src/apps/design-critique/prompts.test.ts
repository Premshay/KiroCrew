import { describe, expect, it } from 'vitest'

import { reviewBriefContext } from './prompts'

describe('reviewBriefContext', () => {
  it('keeps repository context reusable while binding the current target and intent', () => {
    const context = reviewBriefContext({
      contextId: 'atlas',
      projectName: 'Atlas',
      repository: '/work/atlas',
      contextPaths: 'AGENTS.md\ndocs/design-system.md',
      notes: 'Preserve established terminology.',
      targets: 'The decision handoff flow.',
      intent: 'ground',
    })

    expect(context).toContain('Project: Atlas')
    expect(context).toContain('Review target: The decision handoff flow.')
    expect(context).toContain('Do not infer requirements for unselected areas')
    expect(context).toContain('Do not invent a replacement direction.')
  })
})
