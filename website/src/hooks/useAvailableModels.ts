import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { useProvider } from '../providers'
import { modelListRefetchInterval } from '../providers/modelListHealth'
import { withAutoFirst } from '../providers/modelList'
import type { ModelInfo } from '../providers/types'

/** Auto-only list used before the first fetch resolves.
 *
 *  `description: ''` for the same reason as `withAutoFirst`: Auto's short label
 *  is a catalog key resolved where it renders, not an English literal living in
 *  a data module. */
const PLACEHOLDER: ModelInfo[] = [{ name: 'auto', description: '' }]

/**
 * THE model list. Every picker reads it through here.
 *
 * ## Why a hook and not six `useQuery` calls
 *
 * Six surfaces render a model picker (ChatPage, ChatPane, ChatSidebar's bulk
 * switcher, AgentsPage, Settings ▸ Chat, KiroCrewAgentsPage) and all six used
 * the SAME query key — deliberately, so kiro-cli's `--list-models` is spawned
 * once — while each declared its own `queryFn`. React Query stores one cache
 * entry per key and the fetching observer's options win, so with divergent
 * fetchers the array every picker reads is decided by *which surface fetched
 * last*.
 *
 * That was not theoretical. Three shapes were live at once: four surfaces
 * returned `withAutoFirst(models)`, Settings ▸ Chat returned a hand-built
 * `[{name:'auto',description:'Default'}, ...rest]` that discarded everything
 * the live Auto row carried, and KiroCrewAgentsPage returned the raw list with
 * no Auto-first ordering at all. Opening Settings ▸ Chat replaced the shared
 * cache with the stripped shape, so Auto's credit-multiplier badge vanished
 * from every other picker until one of them refetched — a flicker whose cause
 * is three files away from the symptom.
 *
 * One key with one fetcher makes that class of bug unrepresentable: a caller
 * cannot supply a shape, only read one.
 *
 * `enabled` is the one option callers still control, because it is per-observer
 * and cannot corrupt the cached value: ChatSidebar's bulk switcher passes
 * `false` until its panel opens so merely rendering the sidebar does not spawn
 * kiro-cli. Other mounted observers still fetch normally — `enabled` gates who
 * *triggers* a fetch, not what lands in the cache.
 */
type ModelPickerAgent = {
  name: string
  runtime_policy?: { model?: 'selectable' | 'managed' | 'unsupported' } | null
}

/**
 * Read one agent's account-scoped catalog when its companion explicitly owns
 * model selection. Other callers retain the shared Kiro catalog and cache.
 */
export function useAvailableModels({
  enabled,
  agent,
}: {
  enabled?: boolean
  agent?: ModelPickerAgent
} = {}): ModelInfo[] {
  const provider = useProvider()
  const selectableAgent = agent?.runtime_policy?.model === 'selectable' ? agent : undefined
  const { data } = useQuery({
    queryKey: selectableAgent
      ? ['available-models', provider.id, 'agent', selectableAgent.name]
      : ['available-models', provider.id],
    queryFn: async () => {
      if (selectableAgent) {
        const discovered = await api.kirocrewAgentModels(selectableAgent.name)
        return withAutoFirst(discovered.models.map(model => ({
          name: model.modelId,
          description: model.description || model.name,
          contextWindow: provider.getContextWindow(model.modelId),
        })))
      }
      return withAutoFirst(await provider.fetchAvailableModels())
    },
    refetchInterval: selectableAgent ? false : modelListRefetchInterval,
    ...(enabled === undefined ? {} : { enabled }),
  })
  return data ?? PLACEHOLDER
}
