import type { ClientTransport } from './transport'

export function createAutoImprovementEndpoints({ jfetch, sendResponseAuthRecovery }: ClientTransport) {
  return {
    // The app reads diagnostic payloads and logical failures inside successful
    // responses itself; keep that body intact while journaling transport failures.
    autoImprovementResponse: (path: string, init?: RequestInit): Promise<Response> =>
      jfetch('/api/apps/auto-improvement' + path, init).then(sendResponseAuthRecovery),
  }
}
