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
  if (remote) return null
  if (choices.error) return <ErrorNotice message={choices.error instanceof Error ? choices.error.message : String(choices.error)} />
  if (!choices.data?.choices.length) return null
  return <div className="mx-4 mt-2">
    <SimpleSelect
      aria-label={i18nT('components.webAppArtifactCard.backend')}
      clearLabel={i18nT('components.modelDropdownList.auto_default')}
      options={choices.data.choices.map(choice => choice.name)}
      optionLabels={choices.data.choices.map(choice => choice.supported ? choice.label : `${choice.label} — ${choice.reason}`)}
      optionDisabled={choices.data.choices.map(choice => !choice.supported)}
      value={value || ''}
      disabled={running || change.isPending}
      onChange={name => change.mutate(name)}
    />
    {change.data?.warning && <p role="status">{change.data.warning}</p>}
    <ErrorNotice message={change.error instanceof Error ? change.error.message : ''} onDismiss={() => change.reset()} />
  </div>
}
