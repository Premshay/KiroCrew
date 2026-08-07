// Sentence-boundary cutting for streaming TTS.
//
// While assistant tokens stream in, auto-speak synthesizes each finished
// sentence as soon as its terminal lands, so speech starts on the first
// sentence instead of after the whole turn. This module is the pure,
// unit-testable half: given the accumulated streaming text and how much of it
// has already been sent to TTS, decide where the next speakable span ends.
//
// Boundary discipline: TERMINAL punctuation only — `.`, `!`, `?` followed by
// whitespace or end-of-text. Commas, colons, dashes and newlines without a
// terminal never cut: a mid-clause synthesis start sounds like a stumble, and
// abbreviations glued to their next word ("e.g.x") don't match the
// terminal+whitespace shape. A decimal like "3.14" is safe for the same
// reason — its dot has no following whitespace.

/**
 * Spans shorter than this (after trimming) are not worth a synthesis round
 * trip: a lone "Ok." spoken as its own clip chops the prosody and spends a
 * TTS call on filler. The span stays unspoken until more text accumulates or
 * the turn-completion pass speaks the remainder.
 */
export const MIN_TTS_CHARS = 10

/** Terminal sentence punctuation followed by whitespace or end-of-text. */
const SENTENCE_TERMINAL_RE = /[.!?](?:\s|$)/g

/**
 * A `.` ending one of these tokens is an abbreviation, not a sentence end.
 * Deliberately small: a missed abbreviation costs one odd pause; an
 * over-broad list delays speech onset. `!`/`?` never abbreviate.
 */
const ABBREVIATIONS = new Set(['e.g', 'i.e', 'etc', 'vs', 'cf', 'Mr', 'Mrs', 'Ms', 'Dr', 'Prof', 'St', 'Fig', 'approx'])

/** Word characters (plus `.` so "e.g." resolves whole) running up to a terminal. */
const TOKEN_BEFORE_RE = /[\w.]+$/

/**
 * True when the terminal at `idx` (always `.` for the first two cases) ends a
 * token that is not a real sentence end:
 *  - an abbreviation ("e.g.", "Dr.");
 *  - a bare enumerator — "1." / "a." at a list-item position, where cutting
 *    would synthesize the number alone and orphan its item text;
 *  - any position inside an unbalanced ``` fence — code is held back whole so
 *    the turn-completion pass (whose server-side strip sees the balanced
 *    fence) can replace it with its spoken placeholder.
 */
function isGuardedTerminal(full: string, idx: number): boolean {
  const fences = (full.slice(0, idx).match(/```/g) || []).length
  if (fences % 2 === 1) return true
  if (full[idx] !== '.') return false
  const token = TOKEN_BEFORE_RE.exec(full.slice(0, idx))?.[0] ?? ''
  if (ABBREVIATIONS.has(token)) return true
  // Enumerator: a number or single letter opening a line ("1. Do the thing").
  if (/^\d{1,3}$/.test(token) || /^[A-Za-z]$/.test(token)) {
    const lineStart = full.lastIndexOf('\n', idx) + 1
    return full.slice(lineStart, idx).trim() === token
  }
  return false
}

/**
 * One-past-the-terminal index of the LAST completed sentence in `full` that
 * ends after `from`, or -1 when no new complete sentence exists. Slicing
 * `full.slice(from, cut)` yields every whole sentence not yet spoken —
 * batching all completed sentences into one span keeps the synthesis-call
 * rate at one per flush, not one per sentence.
 */
export function lastSentenceCut(full: string, from: number): number {
  SENTENCE_TERMINAL_RE.lastIndex = 0
  let cut = -1
  let match: RegExpExecArray | null
  while ((match = SENTENCE_TERMINAL_RE.exec(full)) !== null) {
    if (match.index + 1 > from && !isGuardedTerminal(full, match.index)) cut = match.index + 1
  }
  return cut
}

/**
 * The next span to synthesize: every completed-but-unspoken sentence in
 * `full` past `spokenLen`, or null when there is none (no new terminal, or
 * the span is under MIN_TTS_CHARS). `nextSpokenLen` is what the caller's
 * spoken-length counter becomes after sending `text`.
 */
export function nextSpeakableSpan(
  full: string,
  spokenLen: number,
): { text: string; nextSpokenLen: number } | null {
  const cut = lastSentenceCut(full, spokenLen)
  if (cut <= spokenLen) return null
  const text = full.slice(spokenLen, cut).trim()
  if (text.length < MIN_TTS_CHARS) return null
  return { text, nextSpokenLen: cut }
}
