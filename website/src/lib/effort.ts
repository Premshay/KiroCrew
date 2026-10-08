/**
 * Reasoning-effort vocabulary for the dashboard — mirrors the backend
 * `kiro_crew/effort.py` so the UI and server agree on levels and per-model
 * capability. Kept as a standalone module (not inside ChatInput) so it can be
 * imported without pulling in the component — and so test mocks of ChatInput
 * don't have to re-export it.
 */

import { i18nT } from '../i18n/t'

/**
 * Catalog KEY for each effort level's display label. '' = provider/model default.
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()` call
 * here would freeze the boot language and never re-resolve on a language switch.
 * The lookup happens in `effortLabel()`, which runs during render.
 *
 * Shaped as a flat `Record` of full literal keys, and indexed inline at the
 * `i18nT()` call, because that is the form `scripts/check-i18n-keys.mjs` can
 * resolve statically — a key it cannot resolve is a key it cannot verify exists.
 */
export const EFFORT_LABEL_KEY: Record<string, string> = {
  '': 'lib.effort.default',
  default: 'lib.effort.default',
  low: 'lib.effort.low',
  medium: 'lib.effort.medium',
  high: 'lib.effort.high',
  xhigh: 'lib.effort.xhigh',
  max: 'lib.effort.max',
}

/**
 * Localised display name for an effort level.
 *
 * A level the backend reports dynamically (via `/api/effort-levels`) that has no
 * entry above has no catalog entry either, so it is returned VERBATIM. It used to
 * be title-cased (`charAt(0).toUpperCase() + slice(1)`), which was wrong twice
 * over: it dressed a raw backend identifier up as English display copy in every
 * locale, and `toUpperCase()` is locale-insensitive, so the result was not even
 * reliably English-correct. Returning the identifier unchanged makes it legible
 * as an identifier and leaves no fabricated copy on screen.
 */
export function effortLabel(level: string): string {
  // `hasOwnProperty`, not `in`: the levels come from /api/effort-levels, so a
  // backend that reports `toString` or `constructor` would otherwise resolve to
  // an inherited Object.prototype member and hand a function to i18next.
  return Object.prototype.hasOwnProperty.call(EFFORT_LABEL_KEY, level)
    ? i18nT(EFFORT_LABEL_KEY[level])
    : level
}

/**
 * Concrete effort levels offered in the dropdown, ordered low→high, with the
 * '' default sentinel first. kiro-cli (acp) supports these on Fable/Opus/Sonnet
 * and GPT-5.x models.
 */
export const EFFORT_LEVELS = ['', 'low', 'medium', 'high', 'xhigh', 'max'] as const

/** Providers whose backend accepts a reasoning-effort level. KiroCrew is
 *  KiroACP-only, so this is just 'acp'. */
export const REASONING_EFFORT_PROVIDERS = new Set(['acp'])

/**
 * Pre-discovery effort capability fallback, keyed on model FAMILY.
 *
 * The authoritative answer comes from the running crew itself: its model catalog
 * carries the reasoning-effort selector its runtime advertised
 * (`effortLevels` in useAvailableModels), and the composer gates on that
 * whenever it is known. This allowlist answers only the case where no crew
 * runtime has reported yet -- the generic catalog, a cold slot -- so it is
 * deliberately conservative and returns false for anything it cannot place
 * rather than guessing a capability.
 *
 * In particular it does NOT know DeepSeek: a DeepSeek session runs through a
 * harness whose model ids are `["provider","model"]` route pairs, and that
 * harness advertises its own effort selector. That case is covered by
 * `effortLevels`, which is exactly why this function is a fallback and not the
 * gate.
 */
export function modelSupportsEffort(model: string | undefined): boolean {
  if (!model) return false
  const m = model.toLowerCase()
  if (m === 'auto' || m.includes('haiku')) return false
  return m.includes('opus') || m.includes('sonnet') || m.includes('fable') || m.includes('gpt')
}

/**
 * May the effort control render for a session?
 *
 * `crewLevels` is what the running crew's own runtime advertised
 * (`useAvailableModels` returns it as `effortLevels`). It is authoritative when
 * present: a non-empty list opens the control, and an EMPTY list closes it --
 * discovery ran and the runtime offered no selector, which outranks any guess
 * from the model id.
 *
 * `undefined` means no crew runtime answered (the generic catalog, a cold
 * slot), and only then does the family allowlist decide. That ordering is what
 * lets a harness whose model ids the allowlist cannot parse still expose its
 * own effort selector.
 */
export function effortSupportedForCrew(
  crewLevels: string[] | undefined,
  model: string | undefined,
): boolean {
  if (crewLevels !== undefined) return crewLevels.length > 0
  return modelSupportsEffort(model)
}
