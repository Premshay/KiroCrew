import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useAppDispatch } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import ErrorNotice from './ErrorNotice'
import SimpleSelect from './SimpleSelect'

export default function RuntimeSelector({ slot, value, running, remote = false }: {
  slot: string
  value?: string
  running: boolean
  remote?: boolean
}) {
  const dispatch = useAppDispatch()
  const cache = useQueryClient()
  const choices = useQuery({
    queryKey: ['slot-runtimes', slot],
    queryFn: () => api.chatSlotRuntimes(slot),
    enabled: !!slot && !remote,
    retry: false,
  })
  const change = useMutation({
    mutationFn: (name: string) => api.chatSlotRuntime(slot, name),
    onSuccess: async result => {
      dispatch(updateSlot({ key: slot, runtime_agent: result.runtime_agent, model: result.model, reasoning_effort: result.reasoning_effort, served_model: result.served_model || '' }))
      await cache.invalidateQueries({ queryKey: ['slot-runtimes', slot] })
      await cache.invalidateQueries({ queryKey: ['slot-selection-capabilities', slot] })
    },
  })
  if (remote || !slot) return null
  if (choices.error) return <ErrorNotice message={choices.error instanceof Error ? choices.error.message : String(choices.error)} />
  const runtimeChoices = choices.data?.choices ?? []
  // An unset runtime_agent is not "no backend": the conversation still runs on
  // the seat its agent is configured for. Showing that seat is what couples the
  // picker to the identity in the view; clearing back to it stays one action.
  const selected = value || choices.data?.effective_runtime_agent || ''
  return <div className="flex items-center gap-1.5 text-[12px] text-muted" data-testid="runtime-selector">
    <span>{i18nT('components.webAppArtifactCard.backend')}</span>
    <SimpleSelect
      className="h-6 w-auto max-w-[13rem] rounded-full border-transparent bg-transparent px-2 py-0 text-[12px]"
      contentClassName="w-60 max-w-[22rem]"
      aria-label={i18nT('components.webAppArtifactCard.backend')}
      clearLabel={i18nT('components.modelDropdownList.auto_default')}
      options={runtimeChoices.map(choice => choice.name)}
      optionLabels={runtimeChoices.map(choice => choice.supported ? choice.label : `${choice.label} — ${choice.reason}`)}
      optionDisabled={runtimeChoices.map(choice => !choice.supported)}
      value={selected}
      disabled={running || change.isPending || runtimeChoices.length === 0}
      onChange={name => change.mutate(name)}
    />
    {change.data?.warning && <p role="status">{change.data.warning}</p>}
    <ErrorNotice message={change.error instanceof Error ? change.error.message : ''} onDismiss={() => change.reset()} />
  </div>
}
