// The run surface's read-only provenance pane: what a finished review loaded,
// what it left out, and why.
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import RuleProvenance from '../apps/code-review-sage/components/RuleProvenance'
import type { Run, RuleResolution } from '../apps/code-review-sage/lib/types'

const CHANGE = 'https://github.com/acme/service/pull/7'

const resolution: RuleResolution = {
  source_identity: {
    provider: 'github', host: 'github.com', owner: 'acme', repository: 'service',
  },
  namespaces: [
    { namespace: 'default', included: true, reason: 'legacy_active_namespace' },
    {
      namespace: 'service-rules',
      included: true,
      reason: 'repository_binding_match',
      binding: {
        scope: 'repository',
        repository: {
          provider: 'github', host: 'github.com', owner: 'acme', repository: 'service',
        },
      },
    },
    {
      namespace: 'other-rules',
      included: false,
      reason: 'repository_binding_mismatch',
      binding: {
        scope: 'repository',
        repository: {
          provider: 'github', host: 'github.com', owner: 'acme', repository: 'other',
        },
      },
    },
  ],
  effective_namespaces: ['default', 'service-rules'],
  effective_rules: [{
    namespace: 'service-rules',
    rule_id: 'R1',
    pattern: {
      id: 'R1',
      title: 'Guard the retry budget',
      scope: 'common',
      impact: 'high',
      guidance: 'Bound every retry loop.',
    },
    reason: 'repository_binding_match',
    sidecar_record_ids: ['rec-1'],
  }],
  warnings: ["namespace 'default' is active without a binding and applies globally"],
}

function run(overrides: Partial<Run> = {}): Run {
  return {
    run_id: 'r1',
    changes: [CHANGE],
    status: 'done',
    started_at: '2026-09-02T10:00:00Z',
    rule_resolutions: { [CHANGE]: resolution },
    ...overrides,
  }
}

describe('Code Review Sage rule provenance', () => {
  it('names the frozen source identity the review resolved', () => {
    render(<RuleProvenance run={run()} />)

    expect(screen.getByText(/Source: github\.com\/acme\/service/)).toBeInTheDocument()
  })

  it('separates the namespaces that were loaded from the ones that were not', () => {
    render(<RuleProvenance run={run()} />)

    expect(screen.getByText('Active, not scoped yet')).toBeInTheDocument()
    expect(screen.getByText('Scoped to this repository')).toBeInTheDocument()
    expect(screen.getByText('Scoped to another repository')).toBeInTheDocument()
    // The excluded namespace still shows the binding that excluded it, so the
    // reason is checkable rather than something to take on faith.
    expect(screen.getByText('github.com/acme/other')).toBeInTheDocument()
  })

  it('surfaces the backend migration warning verbatim', () => {
    render(<RuleProvenance run={run()} />)

    expect(screen.getByTestId('sage-provenance-warnings'))
      .toHaveTextContent('is active without a binding and applies globally')
  })

  it('keeps the rule list collapsed until it is asked for', () => {
    render(<RuleProvenance run={run()} />)

    expect(screen.queryByText('Guard the retry budget')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /show rules/i }))
    expect(screen.getByText('Guard the retry budget')).toBeInTheDocument()
    expect(screen.getByText('Bound every retry loop.')).toBeInTheDocument()
  })

  it('says a change with no repository identity loaded nothing, rather than staying blank', () => {
    const empty: RuleResolution = {
      source_identity: null,
      namespaces: [{
        namespace: 'service-rules',
        included: false,
        reason: 'source_identity_unavailable',
      }],
      effective_namespaces: [],
      effective_rules: [],
      warnings: [],
    }
    render(<RuleProvenance run={run({ rule_resolutions: { [CHANGE]: empty } })} />)

    expect(screen.getByText(/Source: no repository identity/)).toBeInTheDocument()
    expect(screen.getByText('This change names no repository')).toBeInTheDocument()
    expect(screen.getByText('No rules were loaded for this change.')).toBeInTheDocument()
  })

  it('renders nothing for a run recorded before rules carried a resolution', () => {
    const { container } = render(<RuleProvenance run={run({ rule_resolutions: undefined })} />)

    expect(container).toBeEmptyDOMElement()
  })

  it('falls back rather than rendering a raw identifier for an unknown reason', () => {
    const unknown: RuleResolution = {
      ...resolution,
      namespaces: [{ namespace: 'default', included: true, reason: 'reason_from_a_newer_backend' }],
      effective_rules: [],
      warnings: [],
    }
    render(<RuleProvenance run={run({ rule_resolutions: { [CHANGE]: unknown } })} />)

    expect(screen.getByText('Reason not reported')).toBeInTheDocument()
    expect(screen.queryByText(/reason_from_a_newer_backend/)).toBeNull()
  })
})
