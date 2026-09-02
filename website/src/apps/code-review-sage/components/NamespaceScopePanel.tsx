// Settings: where each namespace's learned rules apply.
//
// Three states per namespace, and the third one is the reason this panel exists.
// A namespace with NO binding is a LEGACY namespace: reviews still load it for
// every repository, which is what the operator gets by default and almost never
// what they want once a second repository is under review. It is called out here
// rather than silently migrated, because narrowing a rule set behind the
// operator's back would change review results with nothing on screen to explain it.
//
// The active-namespace checkboxes stay in the learning rail: "load this namespace
// at all" and "where do its rules apply" are separate decisions, and a namespace
// can be scoped while switched off.
import { AlertTriangle } from 'lucide-react'
import { useState } from 'react'

import {
  formatRepositorySource, parseRepositorySource, scopeChoiceOf,
  type RepositoryParseError, type ScopeChoice,
} from '../lib/namespaceBindings'
import type { NamespaceBinding } from '../lib/types'

import SimpleSelect from '../../../components/SimpleSelect'
import { Btn, Input } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'

const SCOPE_OPTIONS: ScopeChoice[] = ['global', 'repository']

const PARSE_ERROR_KEY: Record<RepositoryParseError, string> = {
  repository_required:
    'apps.codeReviewSage.components.namespaceScopePanel.error_repository_required',
  repository_invalid:
    'apps.codeReviewSage.components.namespaceScopePanel.error_repository_invalid',
}

const SELECT_CLASS =
  'text-[12.5px] px-2 py-1 rounded-md bg-bg-elevated text-text border border-border '
  + 'outline-none focus:border-accent cursor-pointer'

export interface NamespaceScopePanelProps {
  namespaces: string[]
  activeNamespaces: string[]
  bindings: Record<string, NamespaceBinding>
  /** Receives the WHOLE map: the settings PUT replaces `namespace_bindings`. */
  onSave: (bindings: Record<string, NamespaceBinding>) => void
  saving: boolean
}

export default function NamespaceScopePanel({
  namespaces, activeNamespaces, bindings, onSave, saving,
}: NamespaceScopePanelProps) {
  // Namespaces whose row is showing the repository field before anything is saved.
  // A row is also in repository mode once its binding IS one, so nothing here has
  // to be cleared after a save lands.
  const [editing, setEditing] = useState<Record<string, boolean>>({})
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [errors, setErrors] = useState<Record<string, RepositoryParseError>>({})

  const active = new Set(activeNamespaces)
  const unscopedActive = namespaces.filter((name) => active.has(name) && !bindings[name])

  const write = (name: string, binding: NamespaceBinding | null) => {
    const next = { ...bindings }
    if (binding) next[name] = binding
    else delete next[name]
    onSave(next)
  }

  const chooseScope = (name: string, choice: string) => {
    setErrors(({ [name]: _dropped, ...rest }) => rest)
    if (choice === 'repository') {
      setEditing((prev) => ({ ...prev, [name]: true }))
      return
    }
    setEditing(({ [name]: _closed, ...rest }) => rest)
    write(name, choice === 'global' ? { scope: 'global' } : null)
  }

  const saveRepository = (name: string, raw: string) => {
    const parsed = parseRepositorySource(raw)
    if (!parsed.ok) {
      setErrors((prev) => ({ ...prev, [name]: parsed.error }))
      return
    }
    setErrors(({ [name]: _cleared, ...rest }) => rest)
    write(name, { scope: 'repository', repository: parsed.value })
  }

  return (
    <section className="mt-6 rounded-xl border border-border bg-card px-4 py-4">
      <h2 className="text-[13px] font-semibold text-text">{i18nT('apps.codeReviewSage.components.namespaceScopePanel.rule_scope')}</h2>
      <p className="text-[12px] text-muted mt-1.5 leading-[1.5] max-w-[560px]">
        {i18nT('apps.codeReviewSage.components.namespaceScopePanel.scope_intro')}
      </p>

      {unscopedActive.length > 0 && (
        <p
          data-testid="sage-unscoped-namespaces"
          className="mt-3 flex items-start gap-1.5 rounded-md border border-warn bg-bg-elevated px-2 py-1.5 text-[12px] text-warn leading-[1.5]"
        >
          <AlertTriangle className="lucide-inline mt-[2px]" aria-hidden="true" />
          <span>{i18nT('apps.codeReviewSage.components.namespaceScopePanel.unscoped_warning', { names: unscopedActive.join(', ') })}</span>
        </p>
      )}

      {namespaces.length === 0 && (
        <p className="mt-3 text-[12px] text-muted">{i18nT('apps.codeReviewSage.components.namespaceScopePanel.no_namespaces')}</p>
      )}

      <div className="mt-3 flex flex-col">
        {namespaces.map((name) => {
          const binding = bindings[name]
          const choice = scopeChoiceOf(binding)
          const repositoryMode = choice === 'repository' || !!editing[name]
          const draft = drafts[name] ?? (binding?.scope === 'repository'
            ? formatRepositorySource(binding.repository)
            : '')
          const error = errors[name]
          return (
            <div
              key={name}
              data-testid={`sage-namespace-scope-${name}`}
              className="py-3 border-b border-border last:border-b-0"
            >
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <span className="font-mono text-[12.5px] text-text">{name}</span>
                <SimpleSelect
                  aria-label={i18nT('apps.codeReviewSage.components.namespaceScopePanel.scope_for', { name })}
                  options={SCOPE_OPTIONS}
                  optionLabels={[i18nT('apps.codeReviewSage.components.namespaceScopePanel.option_global'), i18nT('apps.codeReviewSage.components.namespaceScopePanel.option_repository')]}
                  value={repositoryMode ? 'repository' : choice}
                  onChange={(v) => chooseScope(name, v)}
                  clearLabel={i18nT('apps.codeReviewSage.components.namespaceScopePanel.option_unscoped')}
                  disabled={saving}
                  className={SELECT_CLASS}
                />
              </div>

              {repositoryMode && (
                <div className="mt-2 flex items-center gap-2 flex-wrap">
                  <Input
                    value={draft}
                    aria-label={i18nT('apps.codeReviewSage.components.namespaceScopePanel.repository_for', { name })}
                    placeholder={i18nT('apps.codeReviewSage.components.namespaceScopePanel.repository_placeholder')}
                    onChange={(e) => setDrafts((prev) => ({ ...prev, [name]: e.target.value }))}
                    className="flex-1 min-w-[220px] font-mono text-[12.5px]"
                  />
                  <Btn
                    type="button"
                    disabled={saving}
                    onClick={() => saveRepository(name, draft)}
                  >
                    {i18nT('apps.codeReviewSage.components.namespaceScopePanel.save')}
                  </Btn>
                </div>
              )}

              {error && (
                <p className="mt-1.5 text-[11.5px] text-danger">{i18nT(PARSE_ERROR_KEY[error])}</p>
              )}
              {!repositoryMode && choice === '' && active.has(name) && (
                <p className="mt-1.5 text-[11.5px] text-muted">{i18nT('apps.codeReviewSage.components.namespaceScopePanel.row_unscoped_note')}</p>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}
