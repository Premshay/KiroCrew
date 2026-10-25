// Roles are gateway mention identifiers; tasks are model instructions. Keep
// both byte-identical to the preset API in every dashboard language.
export const FALLBACK_PRESETS = [
  {
    id: 'incident',
    agents: [
      {
        role: 'Orchestrator',
        is_orchestrator: true,
        task: 'Coordinate investigation of {topic}',
      },
      { role: 'Logs Agent', task: 'Search logs related to {topic}' },
      {
        role: 'Code Agent',
        task: 'Check recent code changes related to {topic}',
      },
    ],
  },
  { id: 'custom', agents: [] },
]

export const channelTaskPrompt = (task: string | undefined, topic: string): string =>
  (task || '{topic}').replace('{topic}', topic)
