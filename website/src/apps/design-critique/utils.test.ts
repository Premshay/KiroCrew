import { beforeEach, describe, expect, it } from 'vitest'

import { BRIEFKEY } from './constants'
import { EMPTY_REVIEW_BRIEF, loadReviewBrief } from './utils'

describe('loadReviewBrief', () => {
  beforeEach(() => localStorage.clear())

  it('drops malformed persisted values before they can enter a prompt', () => {
    localStorage.setItem(BRIEFKEY, JSON.stringify({
      projectName: ['not text'],
      notes: { not: 'text' },
      targets: 42,
      intent: 'untrusted',
    }))

    expect(loadReviewBrief()).toEqual(EMPTY_REVIEW_BRIEF)
  })
})
