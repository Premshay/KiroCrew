// Learning: the ruleset a review actually loads, for one namespace at a time.
// Namespace selection and management remain in LearningRail; this pane reads the
// current rules and holds a preview-first consolidation flow. Candidates stay
// untouched until the operator explicitly confirms a fresh preview's apply.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Brain, Check, FileText, Loader2, Sparkles } from 'lucide-react'
import { useEffect, useState } from 'react'

import { SageApiError, sageApi } from '../api'
import { useSage } from '../context'
import { LIVE_POLL_MS } from '../lib/layout'
import type { ConsolidationPreview, LearnedPattern } from '../lib/types'

import { Btn } from '../../../components/ui'
import { fmtNumber } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'

function ImpactTag({ impact }: { impact: string }) {
  const high = impact === 'high'
  return (
    <span
      className={`inline-block px-1.5 py-0.5 rounded text-[10px] mr-1.5 ${
        high ? 'text-danger border border-danger' : 'text-muted border border-border'
      }`}
    >
      {impact}
    </span>
  )
}

function PatternRow({ p }: { p: LearnedPattern }) {
  return (
    <li className="rounded-lg border border-border bg-card px-3.5 py-2.5">
      <div className="text-[13px]">
        <ImpactTag impact={p.impact} />
        <strong className="text-text">{p.title}</strong>
      </div>
      <div className="mt-1 text-[12.5px] text-muted leading-[1.6]">{p.guidance}</div>
    </li>
  )
}

function localizedCode(code: string, group: 'request' | 'apply'): string {
  if (group === 'request') {
    switch (code) {
      case 'consolidation_in_progress':
        return i18nT('apps.codeReviewSage.views.learningView.request_consolidation_in_progress')
      case 'invalid_candidate_ids':
        return i18nT('apps.codeReviewSage.views.learningView.request_invalid_candidate_ids')
      case 'nothing_to_consolidate':
        return i18nT('apps.codeReviewSage.views.learningView.request_nothing_to_consolidate')
      default:
        return i18nT('apps.codeReviewSage.views.learningView.request_unknown')
    }
  }

  switch (code) {
    case 'apply_rollback_failed':
      return i18nT('apps.codeReviewSage.views.learningView.apply_apply_rollback_failed')
    case 'apply_rolled_back':
      return i18nT('apps.codeReviewSage.views.learningView.apply_apply_rolled_back')
    case 'confirmation_required':
      return i18nT('apps.codeReviewSage.views.learningView.apply_confirmation_required')
    case 'preview_already_applied':
      return i18nT('apps.codeReviewSage.views.learningView.apply_preview_already_applied')
    case 'preview_expired':
      return i18nT('apps.codeReviewSage.views.learningView.apply_preview_expired')
    case 'preview_not_found':
      return i18nT('apps.codeReviewSage.views.learningView.apply_preview_not_found')
    case 'preview_stale':
      return i18nT('apps.codeReviewSage.views.learningView.apply_preview_stale')
    default:
      return i18nT('apps.codeReviewSage.views.learningView.apply_unknown')
  }
}

function localizedDecisionAction(action: string): string {
  switch (action) {
    case 'archive':
      return i18nT('apps.codeReviewSage.views.learningView.decision_archive')
    case 'merge':
      return i18nT('apps.codeReviewSage.views.learningView.decision_merge')
    case 'promote':
      return i18nT('apps.codeReviewSage.views.learningView.decision_promote')
    case 'retain':
      return i18nT('apps.codeReviewSage.views.learningView.decision_retain')
    default:
      return i18nT('apps.codeReviewSage.views.learningView.decision_unknown')
  }
}

function localizedDecisionReason(reasonCode: string): string {
  return reasonCode === 'candidate_merged'
    ? i18nT('apps.codeReviewSage.views.learningView.reason_candidate_merged')
    : i18nT('apps.codeReviewSage.views.learningView.reason_unknown')
}

function previewUnavailable(preview: ConsolidationPreview): string | null {
  if (preview.state.status === 'applied') {
    return i18nT('apps.codeReviewSage.views.learningView.preview_already_applied')
  }
  if (preview.state.expired) {
    return i18nT('apps.codeReviewSage.views.learningView.preview_expired')
  }
  if (preview.state.stale) {
    return i18nT('apps.codeReviewSage.views.learningView.preview_stale')
  }
  if (preview.state.status !== 'pending_confirmation') {
    return i18nT('apps.codeReviewSage.views.learningView.preview_unavailable')
  }
  return null
}

function DecisionRow({ preview }: { preview: ConsolidationPreview }) {
  return (
    <ul className="list-none p-0 space-y-1.5">
      {preview.per_candidate_decisions.map((decision) => {
        return (
          <li
            key={decision.candidate_id}
            className="flex items-start justify-between gap-3 text-[12px]"
          >
            <span className="min-w-0 text-muted break-all">{decision.candidate_id}</span>
            <span className="shrink-0 text-right text-text">
              {localizedDecisionAction(decision.action)}
              <span className="text-muted"> · {localizedDecisionReason(decision.reason_code)}</span>
            </span>
          </li>
        )
      })}
    </ul>
  )
}

function BudgetPane({ preview }: { preview: ConsolidationPreview }) {
  const selection = preview.budget_impact.selection
  if (!preview.budget_impact.governed || !selection) {
    return (
      <p className="text-[12px] text-muted">
        {i18nT('apps.codeReviewSage.views.learningView.budget_not_governed')}
      </p>
    )
  }
  return (
    <div className="space-y-1 text-[12px] text-muted">
      {Object.entries(selection.usage).map(([scope, usage]) => {
        const budget = selection.budgets[scope]
        if (!budget) return null
        return (
          <p key={scope}>
            {i18nT('apps.codeReviewSage.views.learningView.budget_scope', {
              scope,
              rules: fmtNumber(usage.rules),
              maxRules: fmtNumber(budget.max_rules),
              tokens: fmtNumber(usage.tokens),
              maxTokens: fmtNumber(budget.max_tokens),
            })}
          </p>
        )
      })}
    </div>
  )
}

function PreviewPane({
  preview,
  applying,
  onApply,
}: {
  preview: ConsolidationPreview
  applying: boolean
  onApply: () => void
}) {
  const unavailable = previewUnavailable(preview)
  const [confirming, setConfirming] = useState(false)

  useEffect(() => {
    setConfirming(false)
  }, [preview.preview_id, unavailable])

  return (
    <section
      className="mt-5 rounded-xl border border-border bg-card p-4"
      aria-labelledby="sage-preview-heading"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 id="sage-preview-heading" className="text-[13px] font-semibold text-text">
            {i18nT('apps.codeReviewSage.views.learningView.preview_ready')}
          </h3>
          <p className="mt-1 text-[12px] text-muted">
            {i18nT('apps.codeReviewSage.views.learningView.preview_selected_count', {
              count: preview.selected_candidate_ids.length,
            })}
          </p>
        </div>
        {unavailable ? (
          <p className="max-w-[42ch] text-[12px] text-warn">{unavailable}</p>
        ) : confirming ? (
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span className="text-[12px] text-muted">
              {i18nT('apps.codeReviewSage.views.learningView.confirm_apply')}
            </span>
            <Btn primary type="button" disabled={applying} onClick={onApply}>
              {i18nT('apps.codeReviewSage.views.learningView.apply_preview')}
            </Btn>
            <Btn type="button" disabled={applying} onClick={() => setConfirming(false)}>
              {i18nT('apps.codeReviewSage.views.learningView.cancel')}
            </Btn>
          </div>
        ) : (
          <Btn primary type="button" disabled={applying} onClick={() => setConfirming(true)}>
            <Check className="lucide-inline" aria-hidden="true" />
            {i18nT('apps.codeReviewSage.views.learningView.apply_preview')}
          </Btn>
        )}
      </div>

      <div className="mt-4 max-h-[30rem] overflow-y-auto rounded-lg border border-border bg-bg p-3 scrollbar-none">
        <div className="space-y-5">
          <div>
            <h4 className="text-[11.5px] font-semibold text-muted">
              {i18nT('apps.codeReviewSage.views.learningView.proposed_ruleset')}
            </h4>
            <pre className="mt-2 whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.6] text-text">
              {preview.proposed_ruleset_markdown}
            </pre>
          </div>
          <div>
            <h4 className="text-[11.5px] font-semibold text-muted">
              {i18nT('apps.codeReviewSage.views.learningView.decisions')}
            </h4>
            <div className="mt-2">
              <DecisionRow preview={preview} />
            </div>
          </div>
          <div>
            <h4 className="text-[11.5px] font-semibold text-muted">
              {i18nT('apps.codeReviewSage.views.learningView.budget_impact')}
            </h4>
            <div className="mt-2">
              <BudgetPane preview={preview} />
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

export default function LearningView() {
  const { selectedNamespace } = useSage()
  const ns = selectedNamespace
  const qc = useQueryClient()
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [previewIdsBeforeRequest, setPreviewIdsBeforeRequest] = useState<string[]>([])
  const [awaitingPreview, setAwaitingPreview] = useState(false)
  const [previewFailure, setPreviewFailure] = useState<string | null>(null)

  const learningsQuery = useQuery({
    queryKey: ['code-review-sage', 'learnings', ns],
    queryFn: () => sageApi.learnings(ns as string),
    enabled: !!ns,
    refetchInterval: (q) => (q.state.data?.consolidating || awaitingPreview ? LIVE_POLL_MS : false),
  })
  const settingsQuery = useQuery({
    queryKey: ['code-review-sage', 'settings'],
    queryFn: () => sageApi.settings(),
  })
  const previewsQuery = useQuery({
    queryKey: ['code-review-sage', 'consolidation-previews', ns],
    queryFn: () => sageApi.consolidationPreviews(ns as string),
    enabled: !!ns,
    refetchInterval: awaitingPreview ? LIVE_POLL_MS : false,
  })
  const previewDetailQuery = useQuery({
    queryKey: [
      'code-review-sage',
      'consolidation-preview',
      ns,
      previewId ?? previewsQuery.data?.previews[0]?.preview_id,
    ],
    queryFn: () =>
      sageApi.consolidationPreview(
        ns as string,
        (previewId ?? previewsQuery.data?.previews[0]?.preview_id) as string,
      ),
    enabled: !!ns && !!(previewId ?? previewsQuery.data?.previews[0]?.preview_id),
  })
  const previewRequest = useMutation({
    mutationFn: (candidateIds: string[]) =>
      sageApi.requestConsolidationPreview(ns as string, candidateIds),
    onMutate: () => {
      setPreviewIdsBeforeRequest(previewsQuery.data?.previews.map((p) => p.preview_id) ?? [])
      setPreviewFailure(null)
    },
    onSuccess: () => {
      setAwaitingPreview(true)
      void qc.invalidateQueries({
        queryKey: ['code-review-sage', 'learnings', ns],
      })
      void qc.invalidateQueries({
        queryKey: ['code-review-sage', 'consolidation-previews', ns],
      })
    },
  })
  const applyPreview = useMutation({
    mutationFn: () =>
      sageApi.applyConsolidationPreview(
        ns as string,
        (previewId ?? previewsQuery.data?.previews[0]?.preview_id) as string,
      ),
    onSuccess: () => {
      setSelectedIds([])
      void qc.invalidateQueries({
        queryKey: ['code-review-sage', 'learnings', ns],
      })
      void qc.invalidateQueries({
        queryKey: ['code-review-sage', 'namespaces'],
      })
      void qc.invalidateQueries({
        queryKey: ['code-review-sage', 'consolidation-previews', ns],
      })
      void qc.invalidateQueries({
        queryKey: ['code-review-sage', 'consolidation-preview', ns, previewId],
      })
    },
  })

  const activeList = settingsQuery.data?.settings.active_namespaces ?? []
  const isActive = !!ns && activeList.includes(ns)
  const patterns = learningsQuery.data?.patterns ?? []
  const candidate = learningsQuery.data?.candidate ?? []
  const candidateIds = candidate.map((item) => item.id)
  const activePreviewId = previewId ?? previewsQuery.data?.previews[0]?.preview_id ?? null

  useEffect(() => {
    setSelectedIds([])
    setPreviewId(null)
    setAwaitingPreview(false)
    setPreviewFailure(null)
  }, [ns])
  useEffect(() => {
    setSelectedIds((current) => current.filter((id) => candidateIds.includes(id)))
  }, [candidateIds.join('|')])
  useEffect(() => {
    const previews = previewsQuery.data?.previews ?? []
    if (!awaitingPreview) return
    const created = previews.find(
      (preview) => !previewIdsBeforeRequest.includes(preview.preview_id),
    )
    if (created) {
      setPreviewId(created.preview_id)
      setAwaitingPreview(false)
    }
  }, [awaitingPreview, previewId, previewIdsBeforeRequest, previewsQuery.data?.previews])
  useEffect(() => {
    if (awaitingPreview && learningsQuery.data?.consolidate_error) {
      setAwaitingPreview(false)
      setPreviewFailure(i18nT('apps.codeReviewSage.views.learningView.preview_failed'))
    }
  }, [awaitingPreview, learningsQuery.data?.consolidate_error])

  if (!ns) {
    return (
      <div className="h-full overflow-y-auto scrollbar-none px-4 md:px-6 py-6">
        <h1 className="text-[22px] font-bold leading-tight text-text-strong flex items-center gap-2">
          <Brain size={18} className="text-accent" aria-hidden="true" />{' '}
          {i18nT('apps.codeReviewSage.views.learningView.learning')}
        </h1>
        <p className="text-[13px] text-muted mt-1.5 leading-[1.5] max-w-[620px]">
          {i18nT(
            'apps.codeReviewSage.views.learningView.pick_a_namespace_in_the_sidebar_to_read_the_patt',
          )}
        </p>
      </div>
    )
  }

  const toggleCandidate = (id: string) => {
    setSelectedIds((current) =>
      current.includes(id) ? current.filter((candidateId) => candidateId !== id) : [...current, id],
    )
  }
  const requestError =
    previewRequest.error instanceof SageApiError
      ? localizedCode(previewRequest.error.code, 'request')
      : previewRequest.error
        ? i18nT('apps.codeReviewSage.views.learningView.request_unknown')
        : null
  const applyError =
    applyPreview.error instanceof SageApiError
      ? localizedCode(applyPreview.error.code, 'apply')
      : applyPreview.error
        ? i18nT('apps.codeReviewSage.views.learningView.apply_unknown')
        : null

  return (
    <div className="h-full overflow-y-auto scrollbar-none px-4 md:px-6 py-6">
      <div className="max-w-[820px]">
        <h1 className="text-[22px] font-bold leading-tight text-text-strong flex items-center gap-2">
          <Brain size={18} className="text-accent" aria-hidden="true" />
          <span className="font-mono">{ns}</span>
          <span
            className={`text-[11px] px-2 py-0.5 rounded-full border ${
              isActive ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted'
            }`}
          >
            {isActive
              ? i18nT('apps.codeReviewSage.views.learningView.loaded_during_reviews')
              : i18nT('apps.codeReviewSage.views.learningView.not_loaded')}
          </span>
        </h1>
        <p className="text-[13px] text-muted mt-1.5 leading-[1.5] max-w-[620px]">
          {i18nT(
            'apps.codeReviewSage.views.learningView.reviews_read_the_consolidated_ruleset_below_neve',
          )}
        </p>

        {learningsQuery.isLoading && (
          <div className="mt-6 inline-flex items-center gap-2 text-[13px] text-muted">
            <Loader2 size={14} className="animate-spin motion-reduce:animate-none" />
            {i18nT('apps.codeReviewSage.views.learningView.loading_learnings')}
          </div>
        )}
        {learningsQuery.error && (
          <div className="mt-6 text-[13px] text-danger">
            {i18nT('apps.codeReviewSage.views.learningView.learnings_unavailable')}
          </div>
        )}

        {!learningsQuery.isLoading && !learningsQuery.error && (
          <>
            <div className="mt-6 flex items-center gap-2">
              <h2 className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                {i18nT('apps.codeReviewSage.views.learningView.ruleset')}{' '}
                {i18nT('apps.codeReviewSage.views.learningView.pattern', {
                  count: patterns.length,
                })}
              </h2>
            </div>
            {patterns.length === 0 ? (
              <div className="mt-2 text-[12.5px] text-muted italic leading-[1.5]">
                {i18nT(
                  'apps.codeReviewSage.views.learningView.nothing_consolidated_yet_reviews_in_this_namespa',
                )}
              </div>
            ) : (
              <ul className="list-none p-0 mt-2 flex flex-col gap-2">
                {patterns.map((p) => (
                  <PatternRow key={p.id} p={p} />
                ))}
              </ul>
            )}

            <div className="mt-7 flex flex-wrap items-center gap-2">
              <h2 className="text-[11px] font-semibold uppercase tracking-wider text-warn">
                {i18nT('apps.codeReviewSage.views.learningView.pending_consolidation')}{' '}
                {fmtNumber(candidate.length)}
              </h2>
              {candidate.length > 0 && (
                <>
                  <span className="text-[12px] text-muted">
                    {i18nT('apps.codeReviewSage.views.learningView.selected_count', {
                      count: selectedIds.length,
                    })}
                  </span>
                  <Btn
                    type="button"
                    disabled={selectedIds.length === 0 || previewRequest.isPending}
                    onClick={() => setSelectedIds([])}
                  >
                    {i18nT('apps.codeReviewSage.views.learningView.clear_selection')}
                  </Btn>
                  <Btn
                    primary
                    type="button"
                    disabled={selectedIds.length === 0 || previewRequest.isPending}
                    onClick={() => previewRequest.mutate(selectedIds)}
                  >
                    {previewRequest.isPending || awaitingPreview ? (
                      <Loader2
                        className="lucide-inline animate-spin motion-reduce:animate-none"
                        aria-hidden="true"
                      />
                    ) : (
                      <Sparkles className="lucide-inline" aria-hidden="true" />
                    )}
                    {i18nT('apps.codeReviewSage.views.learningView.create_preview')}
                  </Btn>
                </>
              )}
            </div>
            {candidate.length === 0 ? (
              <div className="mt-2 text-[12.5px] text-muted italic leading-[1.5]">
                {i18nT(
                  'apps.codeReviewSage.views.learningView.nothing_staged_new_learnings_land_here_when_a_re',
                )}
              </div>
            ) : (
              <ul className="list-none p-0 mt-2 flex flex-col gap-2">
                {candidate.map((c) => {
                  const isSelected = selectedIds.includes(c.id)
                  return (
                    <li
                      key={c.id}
                      className="rounded-lg border border-warn/40 bg-card px-3.5 py-2.5"
                    >
                      <label className="flex cursor-pointer items-start gap-3">
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => toggleCandidate(c.id)}
                          aria-label={i18nT(
                            'apps.codeReviewSage.views.learningView.select_candidate',
                            { title: c.title },
                          )}
                          className="mt-1 h-4 w-4 accent-[var(--accent)]"
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block text-[13px]">
                            <ImpactTag impact={c.impact} />
                            <strong className="text-text">{c.title}</strong>
                          </span>
                          <span className="mt-1 block text-[12.5px] text-muted leading-[1.6]">
                            {c.guidance}
                          </span>
                        </span>
                        <span
                          className={`shrink-0 text-[11px] ${isSelected ? 'text-accent' : 'text-muted'}`}
                        >
                          {isSelected
                            ? i18nT('apps.codeReviewSage.views.learningView.selected')
                            : i18nT('apps.codeReviewSage.views.learningView.retained_unselected')}
                        </span>
                      </label>
                    </li>
                  )
                })}
              </ul>
            )}

            <section className="mt-7" aria-labelledby="sage-preview-list-heading">
              <div className="flex flex-wrap items-center gap-2">
                <FileText className="lucide-inline text-muted" aria-hidden="true" />
                <h2
                  id="sage-preview-list-heading"
                  className="text-[11px] font-semibold uppercase tracking-wider text-muted"
                >
                  {i18nT('apps.codeReviewSage.views.learningView.preview_history')}
                </h2>
              </div>
              {awaitingPreview && (
                <p className="mt-2 inline-flex items-center gap-2 text-[12.5px] text-muted">
                  <Loader2
                    className="lucide-inline animate-spin motion-reduce:animate-none"
                    aria-hidden="true"
                  />
                  {i18nT('apps.codeReviewSage.views.learningView.preparing_preview')}
                </p>
              )}
              {(previewFailure || requestError || applyError) && (
                <p className="mt-2 text-[12.5px] text-danger">
                  {previewFailure || requestError || applyError}
                </p>
              )}
              {previewsQuery.error && (
                <p className="mt-2 text-[12.5px] text-danger">
                  {i18nT('apps.codeReviewSage.views.learningView.previews_unavailable')}
                </p>
              )}
              {(previewsQuery.data?.previews.length ?? 0) > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {previewsQuery.data?.previews.map((preview) => (
                    <Btn
                      key={preview.preview_id}
                      type="button"
                      onClick={() => setPreviewId(preview.preview_id)}
                      className={
                        activePreviewId === preview.preview_id ? '!border-accent !text-accent' : ''
                      }
                    >
                      {i18nT('apps.codeReviewSage.views.learningView.preview_selected_count', {
                        count: preview.selected_candidate_ids.length,
                      })}
                    </Btn>
                  ))}
                </div>
              )}
              {previewDetailQuery.data?.preview && (
                <PreviewPane
                  preview={previewDetailQuery.data.preview}
                  applying={applyPreview.isPending}
                  onApply={() => applyPreview.mutate()}
                />
              )}
            </section>
          </>
        )}
      </div>
    </div>
  )
}
