/**
 * What a chat slot runs on: the model, effort-level and slash-command
 * catalogs, selection capabilities, and a slot's agent, model, reasoning
 * effort, auto-compact threshold, workspace, project and in-place reload.
 */

import { withDeadline } from '../../lib/withDeadline'
import type { ClientTransport } from './transport'

/** Deadline for `api.slashCommands`. Same value as the `$`-menu's
 *  `SKILLS_TIMEOUT_MS` (`./skills`), for the same reason and against the same
 *  gateway: both menus are composer affordances whose fetch blocks Enter while
 *  it is unsettled, so a divergent bound here would only be a second number to
 *  explain. Rationale in the CR description. */
export const SLASH_COMMANDS_TIMEOUT_MS = 15_000

/** A session's turn time limit as `/api/chat/slots/{slot}/turn-timeout` reports it. */
export interface SlotTurnTimeout {
  secs: number | null
  global_secs: number
  min: number
  max: number
}

export interface SlotAccountOption {
  id: string
  label: string
}

export interface SlotAccountState {
  available: boolean
  eligible: boolean
  reason: string
  current: string
  default: string
  default_label: string
  accounts: SlotAccountOption[]
}

export interface SlotAccountSwitchResult extends SlotAccountState {
  ok: boolean
  account: string
  deferred: boolean
  warning?: string
}

/** What `GET /api/chat/slots/follow-default` answers. */
export interface FollowDefaultCount {
  count: number
  digest: string
  default_model: string
  models: { model: string; count: number }[]
  titles: string[]
}

export function createChatSlotSettingsEndpoints({ post, j, jfetch: fetch }: ClientTransport) {
  const selection = {
    chatSlotRuntimes: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/runtime').then(j) as Promise<{
        runtime_agent: string
        /** The seat the conversation already runs on when `runtime_agent` is
         *  unset, so the picker shows the backend in force instead of "Default". */
        effective_runtime_agent: string
        choices: { name: string; label: string; supported: boolean; reason: string }[]
      }>,
    chatSlotRuntime: (slot: string, runtime_agent: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/runtime', { runtime_agent }).then(j) as Promise<{
        ok: boolean; runtime_agent: string; model: string; reasoning_effort: string; served_model?: string; warning?: string
      }>,
    models: () => fetch('/api/models').then(j),
    chatSlotSelectionCapabilities: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/selection-capabilities').then(j) as Promise<{
        known: boolean
        backend?: string
        effort_supported?: boolean
        effort_levels?: string[]
        model_effort_pair_ids?: boolean
      }>,
    effortLevels: (slot?: string) =>
      fetch('/api/effort-levels' + (slot ? '?slot=' + encodeURIComponent(slot) : '')).then(j) as Promise<string[]>,
    // Bounded HERE, not per initiator: react-query dedupes on the key, so the
    // weakest initiator would otherwise decide whether the promise is bounded.
    slashCommands: (signal?: AbortSignal, slot?: string) =>
      withDeadline(SLASH_COMMANDS_TIMEOUT_MS, signal, s =>
        fetch('/api/slash-commands' + (slot ? '?slot=' + encodeURIComponent(slot) : ''), { signal: s }).then(j)),
    /** `kind` names the namespace the user picked from. Omitted, the backend
     *  keeps its legacy name-first resolution; stated, a same-name template and
     *  member are told apart and an unresolvable choice is refused (409) rather
     *  than answered by the default agent. `announce` asks the gateway to
     *  append the "Switched to agent" transcript line, as the typed `/agent`
     *  command does; the picker leaves it off. */
    chatSlotAgent: (slot: string, agent: string, kind?: 'member' | 'template', opts?: { announce?: boolean }) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/agent', {
        agent,
        ...(kind ? { agent_kind: kind } : {}),
        ...(opts?.announce ? { announce: true } : {}),
      }).then(j) as Promise<{ ok?: boolean; agent?: string; agent_kind?: 'member' | 'template' | ''; workspace?: string }>,
    /** `deferred: true` when a response was running: the pick is committed and
     *  applies from the next message; the running reply keeps its model. */
    chatSlotModel: (slot: string, model: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/model', { model }).then(j) as Promise<{ ok?: boolean; model?: string; deferred?: boolean }>,
    /** This slot's auto-compact threshold override (null = follows a default:
     *  `agent_pct` when the session's agent declares one, else `global_pct`). */
    chatSlotAutocompact: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/autocompact').then(j) as Promise<{ pct: number | null; global_pct: number; agent_pct?: number | null; agent?: string | null; min: number; max: number }>,
    /** Set (number) or clear (null) this slot's auto-compact threshold override. */
    setChatSlotAutocompact: (slot: string, pct: number | null) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/autocompact', { pct }).then(j) as Promise<{ ok?: boolean; pct: number | null; global_pct: number; agent_pct?: number | null; agent?: string | null }>,
    /** This slot's own turn time limit in seconds (null = follows the global
     *  `agent.chat_turn_timeout_secs`, reported as `global_secs`). */
    chatSlotTurnTimeout: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/turn-timeout').then(j) as Promise<SlotTurnTimeout>,
    /** Give this slot its own turn time limit (whole seconds), or null to follow the global. */
    setChatSlotTurnTimeout: (slot: string, secs: number | null) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/turn-timeout', { secs }).then(j) as Promise<SlotTurnTimeout & { ok?: boolean }>,
    chatSlotAccount: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/account').then(j) as Promise<SlotAccountState>,
    setChatSlotAccount: (slot: string, account: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/account', { account }).then(j) as Promise<SlotAccountSwitchResult>,
    chatSlotsModel: (model: string, skip_running: boolean) =>
      post('/api/chat/slots/model', { model, skip_running }).then(j) as Promise<{ ok: boolean; model: string; switched: string[]; skipped_running: string[]; unchanged: string[]; failed: string[] }>,
    /** How many chats a person started keep a model of their own, and a digest of
     *  exactly that set for `chatSlotsFollowDefault` to confirm. Changes nothing.
     *  Also what the confirm names: the saved Default Model (`''` on Auto), how
     *  many counted chats keep each model (most first), and the redacted titles of
     *  the first five counted chats (`''` when untitled). */
    chatSlotsFollowDefaultCount: () =>
      fetch('/api/chat/slots/follow-default').then(j) as Promise<FollowDefaultCount>,
    /** Clear the kept model on the chats the count's `digest` names, so each
     *  follows the default again. 409 `chats_changed` (nothing changed) when that
     *  set has moved since the count. */
    chatSlotsFollowDefault: (digest: string) =>
      post('/api/chat/slots/follow-default', { digest }).then(j) as Promise<{ ok: boolean; changed: number }>,
    chatSlotReasoningEffort: (slot: string, reasoning_effort: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/reasoning-effort', { reasoning_effort }).then(j) as Promise<{ ok?: boolean; reasoning_effort?: string; model?: string; deferred?: boolean }>,
    chatSlotWorkspace: (slot: string, workspace: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/workspace', { workspace }).then(j),
    // Relaunch the slot's agent process in place (fresh agent spec, env, and MCP
    // servers; conversation preserved). 409 while a turn is in flight.
    chatSlotReload: (slot: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/reload', {}).then(j) as Promise<{ ok?: boolean; error?: string }>,
    chatSlotProject: (slot: string, project: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/project', { project }).then(j) as Promise<{ ok?: boolean; project?: string }>,
  }

  return { selection }
}
