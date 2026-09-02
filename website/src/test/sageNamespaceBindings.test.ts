// The client-side half of the namespace-binding contract: what a person may type
// into the repository field, and what the settings PUT then carries.
//
// The backend canonicalizes the same identity and rejects what it cannot parse.
// These cases pin the forms it accepts, in the shapes people actually paste.
import { describe, expect, it } from 'vitest'

import {
  DEFAULT_REPOSITORY_HOST, formatRepositorySource, parseRepositorySource, scopeChoiceOf,
} from '../apps/code-review-sage/lib/namespaceBindings'

const value = (input: string) => {
  const parsed = parseRepositorySource(input)
  if (!parsed.ok) throw new Error(`expected ${input} to parse, got ${parsed.error}`)
  return parsed.value
}

describe('parseRepositorySource', () => {
  it('accepts the owner/repo shorthand on the default host', () => {
    expect(value('acme/service')).toEqual({
      provider: 'github', host: DEFAULT_REPOSITORY_HOST, owner: 'acme', repository: 'service',
    })
  })

  it('canonicalizes case, a .git suffix and the www host the same way the backend does', () => {
    expect(value('https://WWW.GitHub.com/Acme/Service.git')).toEqual({
      provider: 'github', host: 'github.com', owner: 'acme', repository: 'service',
    })
  })

  it('keeps an enterprise host, so two installs of one owner/repo stay distinct', () => {
    expect(value('https://github.acme-corp.io/acme/service')).toEqual({
      provider: 'github', host: 'github.acme-corp.io', owner: 'acme', repository: 'service',
    })
  })

  it('reads the repository out of a pull-request URL', () => {
    expect(value('https://github.com/acme/service/pull/7?w=1')).toEqual({
      provider: 'github', host: 'github.com', owner: 'acme', repository: 'service',
    })
  })

  it('reads an scp-style remote without mistaking the user for the host', () => {
    expect(value('git@github.com:acme/service.git')).toEqual({
      provider: 'github', host: 'github.com', owner: 'acme', repository: 'service',
    })
  })

  it('does not read a bare owner as a host when no host was given', () => {
    // `acme` has no dot, so the default host stands and acme/service survives —
    // reading it as a hostname would bind the namespace to a repository nobody named.
    expect(value('acme/service/pull/7')).toEqual({
      provider: 'github', host: DEFAULT_REPOSITORY_HOST, owner: 'acme', repository: 'service',
    })
  })

  it('separates "nothing typed" from "typed something unusable"', () => {
    expect(parseRepositorySource('   ')).toEqual({ ok: false, error: 'repository_required' })
    expect(parseRepositorySource('service')).toEqual({ ok: false, error: 'repository_invalid' })
    expect(parseRepositorySource('acme/serv ice')).toEqual({ ok: false, error: 'repository_invalid' })
    expect(parseRepositorySource('acme/../etc')).toEqual({ ok: false, error: 'repository_invalid' })
  })
})

describe('formatRepositorySource', () => {
  it('round-trips through the input field', () => {
    const source = value('https://github.com/acme/service')
    expect(formatRepositorySource(source)).toBe('github.com/acme/service')
    expect(value(formatRepositorySource(source))).toEqual(source)
  })
})

describe('scopeChoiceOf', () => {
  it('reports a missing binding as the legacy unscoped state, not as global', () => {
    expect(scopeChoiceOf(undefined)).toBe('')
    expect(scopeChoiceOf({ scope: 'global' })).toBe('global')
    expect(scopeChoiceOf({
      scope: 'repository',
      repository: { provider: 'github', host: 'github.com', owner: 'acme', repository: 'service' },
    })).toBe('repository')
  })
})
