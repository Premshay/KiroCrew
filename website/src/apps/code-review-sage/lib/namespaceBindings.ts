// Reading and writing the repository a namespace's rules are scoped to.
//
// The backend canonicalizes the same identity in `learning.canonical_repository_source`
// and REJECTS anything it cannot canonicalize. Parsing here as well is not a second
// contract: it is what lets the panel say which field is wrong while the user is
// still looking at it, instead of surfacing a 400 whose English body a localized
// page must not render.
import type { NamespaceBinding, RepositorySource } from './types'

/** The provider the backend accepts today. Sent explicitly rather than inferred
 *  from the host, because a GitHub Enterprise install has an arbitrary hostname. */
export const REPOSITORY_PROVIDER = 'github'

/** Assumed when the input names no host, so `owner/repo` is a legal shorthand. */
export const DEFAULT_REPOSITORY_HOST = 'github.com'

export type RepositoryParseError = 'repository_required' | 'repository_invalid'

export type RepositoryParse =
  | { ok: true; value: RepositorySource }
  | { ok: false; error: RepositoryParseError }

/** Mirrors the backend's `_HOST_RE` and `_REPOSITORY_SEGMENT_RE`. */
const HOST_RE = /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/
const SEGMENT_RE = /^[A-Za-z0-9._-]+$/
/** `.` and `..` satisfy SEGMENT_RE. The backend stores an identity rather than a
 *  path so they are harmless there, but they name no repository a review can ever
 *  resolve to — refusing them here keeps a dead binding out of the config. */
const DOTS_ONLY_RE = /^\.+$/
const SCHEME_RE = /^[A-Za-z][A-Za-z0-9+.-]*:\/\//

/** Which of the three states a namespace is in. `unscoped` is the legacy state:
 *  no binding at all, so the namespace still applies to every repository. */
export type ScopeChoice = '' | 'global' | 'repository'

export function scopeChoiceOf(binding: NamespaceBinding | undefined): ScopeChoice {
  return binding ? binding.scope : ''
}

/** `host/owner/repository` — the form the input round-trips and the provenance
 *  pane shows. The host is always spelled out: two enterprise installs can host
 *  the same `owner/repo`, and that is exactly the case a binding disambiguates. */
export function formatRepositorySource(source: RepositorySource): string {
  return `${source.host}/${source.owner}/${source.repository}`
}

/**
 * Parse a repository identity out of what a person is likely to paste.
 *
 * Accepts `owner/repo`, `host/owner/repo`, a repository or pull-request URL, and
 * the scp-style `git@host:owner/repo.git` remote. A leading segment counts as a
 * host only when it looks like one (it contains a dot, or is `localhost`), so
 * `acme/service/pull/7` still resolves to acme/service on the default host rather
 * than reading `acme` as a hostname.
 */
export function parseRepositorySource(input: string): RepositoryParse {
  const raw = input.trim()
  if (!raw) return { ok: false, error: 'repository_required' }

  let rest = raw.replace(SCHEME_RE, '')
  const at = rest.indexOf('@')
  const slash = rest.indexOf('/')
  // userinfo (`git@host…`, `user:token@host…`) only when it precedes the path.
  if (at !== -1 && (slash === -1 || at < slash)) rest = rest.slice(at + 1)
  // scp-style `host:owner/repo`. A port (`host:8443/…`) is left alone.
  rest = rest.replace(/^([^/:]+):(?!\d)/, '$1/')
  rest = rest.split(/[?#]/)[0]

  const parts = rest.split('/').filter(Boolean)
  const hostLike = parts.length > 2
    && (parts[0].includes('.') || parts[0].toLowerCase() === 'localhost')
  const host = (hostLike ? parts[0] : DEFAULT_REPOSITORY_HOST).toLowerCase()
  const segments = hostLike ? parts.slice(1) : parts
  if (segments.length < 2) return { ok: false, error: 'repository_invalid' }

  const owner = segments[0].toLowerCase()
  const repository = segments[1].toLowerCase().replace(/\.git$/, '')
  // `www.github.com` is the same origin as `github.com`; the backend folds it too,
  // and an unfolded binding would never match a source identity.
  const canonicalHost = host === 'www.github.com' ? DEFAULT_REPOSITORY_HOST : host
  if (!HOST_RE.test(canonicalHost)
    || !SEGMENT_RE.test(owner) || DOTS_ONLY_RE.test(owner)
    || !SEGMENT_RE.test(repository) || DOTS_ONLY_RE.test(repository)) {
    return { ok: false, error: 'repository_invalid' }
  }
  return {
    ok: true,
    value: {
      provider: REPOSITORY_PROVIDER,
      host: canonicalHost,
      owner,
      repository,
    },
  }
}
