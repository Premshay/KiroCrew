/** Theme tokens only; a literal hex fails `no-raw-colors` and would ignore the
 *  user's theme. */
export const C = {
  text: 'var(--text)',
  dim: 'var(--text-dim)',
  border: 'var(--border)',
  cardHl: 'var(--card-hl)',
  bg: 'var(--bg)',
  accent: 'var(--accent)',
  aim: 'var(--aim)',
  ok: 'var(--ok)',
  warn: 'var(--warn)',
  danger: 'var(--danger)',
} as const

/** Store vocabulary is shown verbatim; only the colour is ours. */
export const KIND_VARIANT = {
  CLAIM: 'aim',
  FACT: 'muted',
  FAIL: 'err',
  ASK: 'warn',
  DONE: 'ok',
} as const

export function kindVariant(kind: string): 'aim' | 'muted' | 'err' | 'warn' | 'ok' {
  return (KIND_VARIANT as Record<string, 'aim' | 'muted' | 'err' | 'warn' | 'ok'>)[kind] ?? 'muted'
}

export const KIND_COLOR: Record<string, string> = {
  CLAIM: C.aim,
  FACT: C.dim,
  FAIL: C.danger,
  ASK: C.warn,
  DONE: C.ok,
}

/** Autonomy is a store field with exactly three values. Literal keys —
 *  assembling one is what `dynamicKeys.test.ts` forbids. */
export const AUTONOMY_KEY = {
  1: 'pages.arcBoard.autonomy_1',
  2: 'pages.arcBoard.autonomy_2',
  3: 'pages.arcBoard.autonomy_3',
} as const

export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}
