import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { parseErrorCode } from '../../utils/errorReport'
import type { PreferenceAdvice } from '../../api/client/decisions'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'

type Options = {
  enabled: boolean
  slot: string | null
  model: string
  eligible: boolean
  models: string[]
  scope?: string
  readDraft: () => string
  subscribeDraft?: (listener: () => void) => () => void
  openModelPicker: (trigger: HTMLButtonElement) => void
  applyModel: (model: string) => Promise<string | undefined>
}
type Pending = { slot: string | null; scope?: string; task: string; advice: PreferenceAdvice }
type Response = { choice: 'use' | 'keep' | 'choose'; model: string }
const noSubscription = () => () => {}
const taskKey = (slot: string | null, scope: string | undefined, task: string) => JSON.stringify([slot, scope, task])

export function usePreferenceAdvisor(options: Options) {
  const { t } = useTranslation()
  const latest = useRef(options)
  latest.current = options
  const inFlight = useRef(false)
  const generation = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const choosing = useRef(false)
  const applied = useRef<Response | null>(null)
  const appliedObserved = useRef(false)
  const applying = useRef<string | null>(null)
  const pickerPending = useRef(false)
  const feedbackController = useRef<AbortController | null>(null)
  const allowed = useRef('')
  const attempted = useRef('')
  const [pending, setPending] = useState<Pending | null>(null)
  const draftMatches = useSyncExternalStore(options.subscribeDraft ?? noSubscription,
    () => !pending || options.readDraft().trim() === pending.task)
  const [notice, setNotice] = useState<string | null>(null)
  const [recording, setRecording] = useState(false)
  const [picking, setPicking] = useState(false)
  const advice = useMutation({
    mutationFn: async ({ slot, task, models, signal }: { slot: string; task: string; models: string[]; signal: AbortSignal }) => {
      try {
        return await api.getPreferenceAdvice(slot, task, models, signal)
      } catch (error) {
        if (error instanceof ApiError && error.status === 403 && !error.authRequired &&
          parseErrorCode(error.body) === 'dashboard_owner_required') {
          return { reason: 'owner_only' }
        }
        throw error
      }
    },
  })
  const feedback = useMutation({
    mutationFn: async (response: Response) => {
      const { choice, model } = response
      const attempt = generation.current
      if (!pending || latest.current.slot !== pending.slot) return
      if (!matches(pending, applied.current !== null)) {
        setPending(null)
        return
      }
      if (choice !== 'keep' && !applied.current) {
        applying.current = model
        try {
          const value = await latest.current.applyModel(model)
          if (attempt !== generation.current) return
          if (value === undefined) throw new Error(t('components.chatInput.switch_not_confirmed'))
          applied.current = { choice, model: value }
        } finally {
          applying.current = null
        }
      }
      if (attempt !== generation.current || !matches(pending, applied.current !== null)) return
      const selected = applied.current ?? response
      if (pending.advice.preview) {
        allowed.current = taskKey(pending.slot, pending.scope, pending.task)
        setPending(null)
        return
      }
      if (!pending.advice.id) return
      feedbackController.current = new AbortController()
      setRecording(true)
      try {
        await api.sendPreferenceFeedback(pending.advice.id, selected.choice, selected.model, feedbackController.current.signal)
      } finally {
        if (attempt === generation.current) setRecording(false)
      }
      if (attempt !== generation.current || !matches(pending, applied.current !== null)) return
      allowed.current = taskKey(pending.slot, pending.scope, pending.task)
      setPending(null)
      setNotice(pending.slot)
    },
  })

  const matches = useCallback((value: Pending, appliedOnWire = false) => {
    const current = latest.current
    if (applied.current && current.model === applied.current.model) appliedObserved.current = true
    return current.enabled && current.slot === value.slot && current.scope === value.scope && current.eligible && current.readDraft().trim() === value.task &&
      (appliedOnWire || current.model === (applied.current?.model ?? value.advice.current) ||
        (applied.current !== null && !appliedObserved.current && current.model === value.advice.current) ||
        applying.current !== null || pickerPending.current)
  }, [])

  useEffect(() => {
    if (pending && (!draftMatches || !matches(pending))) {
      if (options.slot === pending.slot && options.scope === pending.scope && options.readDraft().trim() === pending.task && options.eligible) {
        allowed.current = taskKey(pending.slot, pending.scope, pending.task)
      }
      setPending(null)
      generation.current++
      choosing.current = false
      pickerPending.current = false
      setPicking(false)
      applied.current = null
      feedback.reset()
    }
  }, [pending, options, feedback, matches, draftMatches])
  useEffect(() => () => {
    generation.current++
    controller.current?.abort()
    feedbackController.current?.abort()
  }, [])

  const pickRef = useRef<(target: string) => (model?: string) => void>(() => () => {})
  pickRef.current = target => {
    if (!pending || !matches(pending)) return () => {}
    if (!choosing.current) {
      allowed.current = taskKey(pending.slot, pending.scope, pending.task)
      setPending(null)
      generation.current++
      return () => {}
    }
    const attempt = ++generation.current
    pickerPending.current = true
    setPicking(true)
    applying.current = target
    return model => {
      if (attempt !== generation.current) return
      pickerPending.current = false
      setPicking(false)
      applying.current = null
      choosing.current = false
      if (model === undefined || !matches(pending, true)) { setPending(null); return }
      applied.current = { choice: 'choose', model }
      feedback.mutate(applied.current)
    }
  }
  const beginModelPick = useCallback((target: string) => pickRef.current(target), [])

  function skip() {
    generation.current++
    controller.current?.abort()
    feedbackController.current?.abort()
    setRecording(false)
    inFlight.current = false
    allowed.current = taskKey(latest.current.slot, latest.current.scope, latest.current.readDraft().trim())
    choosing.current = false
    pickerPending.current = false
    setPicking(false)
    applied.current = null
    appliedObserved.current = false
    setPending(null)
    advice.reset()
    feedback.reset()
  }

  async function beforeSend(task: string): Promise<boolean> {
    const current = latest.current
    if (!current.enabled || !current.eligible || task.length > 4000 || task.startsWith('/')) return true
    const key = taskKey(current.slot, current.scope, task)
    if (allowed.current === key) return true
    if (inFlight.current || feedback.isPending || pickerPending.current) return false
    if (pending && matches(pending)) return false
    inFlight.current = true
    const attempt = ++generation.current
    controller.current = new AbortController()
    attempted.current = key
    setPending(null)
    setNotice(null)
    applied.current = null
    appliedObserved.current = false
    choosing.current = false
    feedback.reset()
    try {
      const result = await advice.mutateAsync({ slot: current.slot ?? '', task, models: current.models, signal: controller.current.signal })
      if (attempt !== generation.current) return false
      if (latest.current.slot !== current.slot || latest.current.model !== current.model || latest.current.scope !== current.scope) return false
      if (latest.current.readDraft().trim() !== task) return false
      if ((!result.id && !result.preview) || !(result.model || result.budget) || (result.model && result.model === current.model)) return true
      setPending({ slot: current.slot, scope: current.scope, task, advice: { ...result, current: current.model } })
      return false
    } catch {
      return false
    } finally {
      if (attempt === generation.current) inFlight.current = false
    }
  }

  const sendRef = useRef(beforeSend)
  sendRef.current = beforeSend
  const stableBeforeSend = useCallback((task: string) =>
    latest.current.enabled && latest.current.eligible ? sendRef.current(task) : true, [])

  const visible = pending && matches(pending) ? pending : null
  const ownsAttempt = attempted.current === taskKey(options.slot, options.scope, options.readDraft().trim())
  const error = ownsAttempt ? advice.error || feedback.error : null
  const saved = notice !== null && notice === options.slot && options.eligible
  const loading = ownsAttempt && advice.isPending
  const card = visible || error || saved || loading ? (
    <div className="mx-auto w-full px-4 pb-2 text-sm text-text" style={{ maxWidth: 'var(--mc-content-width, 900px)' }} aria-live="polite">
      {loading && <p>{t('preference_advisor.loading')}</p>}
      {visible && <div className="rounded-lg border border-border bg-card p-3 space-y-2">
        <p>{t('preference_advisor.recommend', { model: visible.advice.model || t(`preference_advisor.budget_${visible.advice.budget}`) })}</p>
        <p className="text-muted">{t('preference_advisor.reason', { examples: visible.advice.examples?.length ?? 0 })}</p>
        {visible.advice.evidence?.length ? <ul className="list-disc pl-5 text-muted">{visible.advice.evidence.map((example, index) => <li key={index}>{example}</li>)}</ul> : null}
        <div className="flex flex-wrap gap-2">
          {applied.current ? <Btn disabled={feedback.isPending || picking} onClick={() => feedback.mutate(applied.current!)}>{t('components.chatPane.retry')}</Btn> : <>
            {visible.advice.model && <Btn primary disabled={feedback.isPending || picking} onClick={() => feedback.mutate({ choice: 'use', model: visible.advice.model! })}>{t('preference_advisor.use')}</Btn>}
            <Btn disabled={feedback.isPending || picking} onClick={() => feedback.mutate({ choice: 'keep', model: visible.advice.current ?? '' })}>{t('preference_advisor.keep')}</Btn>
          </>}
        </div>
        <p className="text-muted">{t('preference_advisor.draft')}</p>
        {!applied.current && <div className="border-t border-border pt-2">
          <Btn disabled={feedback.isPending || picking} onClick={event => { choosing.current = true; options.openModelPicker(event.currentTarget) }}>{t('preference_advisor.choose')}</Btn>
        </div>}
      </div>}
      {/* No hand-off: navigation would discard the unsent task in the composer. */}
      <ErrorNotice message={error instanceof Error ? error.name === 'TimeoutError' ? t('preference_advisor.timeout') : error.message : undefined} />
      {ownsAttempt && (loading || recording || advice.isError || feedback.isError) && <Btn onClick={skip}>{t('preference_advisor.skip')}</Btn>}
      {saved && !visible && <p>{t('preference_advisor.saved')}</p>}
    </div>
  ) : null
  return { beforeSend: stableBeforeSend, card, beginModelPick }
}
