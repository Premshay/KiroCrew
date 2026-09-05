// One run's frozen ruleset: which namespaces reached the reviewer, which did not,
// and why each was decided that way.
//
// Read-only on purpose. The resolution was frozen before the reviewer started and
// the review already ran against exactly it, so a control here would either lie
// about a finished review or silently re-scope a future one. Scope is changed in
// Settings; this pane explains what a run actually loaded.
import { ChevronDown, ChevronRight, ScrollText } from 'lucide-react'
import { useState } from 'react'

import { prLabelFromChange } from '../lib/format'
import { formatRepositorySource } from '../lib/namespaceBindings'
import type { NamespaceResolution, Run, RuleResolution } from '../lib/types'

import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'

/** Backend inclusion reasons. An unreported or unknown reason falls back rather
 *  than rendering a raw identifier into localized copy. */
const REASON_KEY: Record<string, string> = {
  legacy_active_namespace:
    'apps.codeReviewSage.components.ruleProvenance.reason_legacy_active_namespace',
  explicit_global_binding:
    'apps.codeReviewSage.components.ruleProvenance.reason_explicit_global_binding',
  repository_binding_match:
    'apps.codeReviewSage.components.ruleProvenance.reason_repository_binding_match',
  repository_binding_mismatch:
    'apps.codeReviewSage.components.ruleProvenance.reason_repository_binding_mismatch',
  source_identity_unavailable:
    'apps.codeReviewSage.components.ruleProvenance.reason_source_identity_unavailable',
  namespace_missing:
    'apps.codeReviewSage.components.ruleProvenance.reason_namespace_missing',
}

export function reasonLabel(reason: string): string {
  return i18nT(REASON_KEY[reason]
    ?? 'apps.codeReviewSage.components.ruleProvenance.reason_unknown')
}

function NamespaceRow({ entry }: { entry: NamespaceResolution }) {
  const binding = entry.binding
  return (
    <li className="flex items-baseline gap-2 flex-wrap">
      <span className={`font-mono text-[12px] ${entry.included ? 'text-accent' : 'text-muted'}`}>
        {entry.namespace}
      </span>
      <span className="text-[11.5px] text-muted">{reasonLabel(entry.reason)}</span>
      {binding?.scope === 'repository' && (
        <span className="font-mono text-[11.5px] text-muted">
          {formatRepositorySource(binding.repository)}
        </span>
      )}
    </li>
  )
}

function ChangeResolution({ change, resolution }: { change: string; resolution: RuleResolution }) {
  const [showRules, setShowRules] = useState(false)
  const included = resolution.namespaces.filter((n) => n.included)
  const excluded = resolution.namespaces.filter((n) => !n.included)
  const rules = resolution.effective_rules ?? []

  return (
    <div className="rounded-lg border border-border bg-bg-elevated px-3 py-2.5 flex flex-col gap-2">
      <div className="flex items-baseline gap-2 flex-wrap">
        <span className="font-mono text-[12.5px] text-text">{prLabelFromChange(change)}</span>
        <span className="text-[11.5px] text-muted">
          {/* One key, not a label plus a colon: the separator and the word order
              around it are the translation's to choose. */}
          {i18nT('apps.codeReviewSage.components.ruleProvenance.source_is', {
            source: resolution.source_identity
              ? formatRepositorySource(resolution.source_identity)
              : i18nT('apps.codeReviewSage.components.ruleProvenance.no_source_identity'),
          })}
        </span>
      </div>

      {included.length > 0 && (
        <div>
          <div className="text-[11px] uppercase tracking-[.05em] text-muted">
            {i18nT('apps.codeReviewSage.components.ruleProvenance.loaded')}
          </div>
          <ul className="mt-1 flex flex-col gap-1">
            {included.map((entry) => <NamespaceRow key={entry.namespace} entry={entry} />)}
          </ul>
        </div>
      )}

      {excluded.length > 0 && (
        <div>
          <div className="text-[11px] uppercase tracking-[.05em] text-muted">
            {i18nT('apps.codeReviewSage.components.ruleProvenance.not_loaded')}
          </div>
          <ul className="mt-1 flex flex-col gap-1">
            {excluded.map((entry) => <NamespaceRow key={entry.namespace} entry={entry} />)}
          </ul>
        </div>
      )}

      {resolution.warnings?.length > 0 && (
        <ul className="flex flex-col gap-1" data-testid="sage-provenance-warnings">
          {resolution.warnings.map((warning) => (
            <li key={warning} className="text-[11.5px] text-warn leading-[1.5]">{warning}</li>
          ))}
        </ul>
      )}

      {rules.length === 0 ? (
        <div className="text-[11.5px] text-muted">
          {i18nT('apps.codeReviewSage.components.ruleProvenance.no_rules')}
        </div>
      ) : (
        <div>
          {/* Collapsed by default: a mature namespace carries dozens of rules, and
              the question this pane answers first is WHICH namespaces applied. */}
          <Btn
            type="button"
            onClick={() => setShowRules((open) => !open)}
            aria-expanded={showRules}
            className="gap-1 border-0 bg-transparent px-0 py-0 text-[11.5px] text-muted hover:bg-transparent hover:text-text"
          >
            {showRules
              ? <ChevronDown className="lucide-inline" aria-hidden="true" />
              : <ChevronRight className="lucide-inline" aria-hidden="true" />}
            {showRules
              ? i18nT('apps.codeReviewSage.components.ruleProvenance.hide_rules')
              : i18nT('apps.codeReviewSage.components.ruleProvenance.show_rules')}
          </Btn>
          {showRules && (
            <ul className="mt-1.5 flex flex-col gap-1.5">
              {rules.map((rule) => (
                <li key={`${rule.namespace}/${rule.rule_id}`} className="leading-[1.5]">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span className="font-mono text-[11.5px] text-muted">{rule.namespace}</span>
                    <span className="text-[12px] text-text">{rule.pattern?.title || rule.rule_id}</span>
                    <span className="text-[11px] text-muted">{reasonLabel(rule.reason)}</span>
                  </div>
                  {rule.pattern?.guidance && (
                    <div className="text-[11.5px] text-muted">{rule.pattern.guidance}</div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

/** Nothing renders for a run recorded before rules carried a resolution — there is
 *  no way to reconstruct what such a run loaded, and an empty pane would read as
 *  "no rules applied". */
export default function RuleProvenance({ run }: { run: Run }) {
  const resolutions = run.rule_resolutions
  if (!resolutions) return null
  const changes = run.changes.filter((change) => resolutions[change])
  if (changes.length === 0) return null

  return (
    <section className="rounded-xl border border-border bg-card px-4 py-4">
      <h2 className="text-[13px] font-semibold text-text flex items-center gap-1.5">
        <ScrollText className="lucide-inline text-accent" aria-hidden="true" />
        {i18nT('apps.codeReviewSage.components.ruleProvenance.rules_applied')}
      </h2>
      <p className="text-[12px] text-muted mt-1 leading-[1.5] max-w-[560px]">
        {i18nT('apps.codeReviewSage.components.ruleProvenance.frozen_note')}
      </p>
      <div className="mt-3 flex flex-col gap-2">
        {changes.map((change) => (
          <ChangeResolution key={change} change={change} resolution={resolutions[change]} />
        ))}
      </div>
    </section>
  )
}
