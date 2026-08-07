// Per-turn latency marks for the voice conversation loop.
//
// A turn's felt latency is two spans measured from the moment the user
// stopped speaking (the VAD endpoint committing the capture): to the first
// streamed token, and to the first audible TTS audio. Both are logged at
// debug level so a phone-vs-desktop comparison is one console filter away,
// and cleared at turn end so a mark can never bleed into the next turn.
//
// Module-level singleton on purpose: the marks are written from three
// unrelated seams (the hands-free stop-commit in ChatPage, the chat_chunk and
// voice playback paths in useWebSocket) that share no React tree ancestry.
// Voice is single-session (one mic, one active slot speaking), so one set of
// marks is sufficient.

interface TurnMarks {
  endOfSpeechAt: number
  firstTokenAt: number | null
  firstAudioAt: number | null
}

let marks: TurnMarks | null = null

/**
 * A mark this old is a leftover from a voice turn that never produced a reply
 * (transcription failed, transcript judged unsendable) — attributing a later,
 * unrelated turn's first token to it would log a nonsense span. Anything a
 * live turn measures completes well inside this.
 */
const MARK_TTL_MS = 30_000

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())

function liveMarks(): TurnMarks | null {
  if (marks && now() - marks.endOfSpeechAt > MARK_TTL_MS) marks = null
  return marks
}

/** The VAD endpoint just committed the capture: the user stopped speaking. */
export function markEndOfSpeech(): void {
  marks = { endOfSpeechAt: now(), firstTokenAt: null, firstAudioAt: null }
}

/** First streamed assistant token of the turn reached the client. */
export function markFirstToken(): void {
  const marks = liveMarks()
  if (!marks || marks.firstTokenAt !== null) return
  marks.firstTokenAt = now()
  // eslint-disable-next-line no-console -- debug-level latency instrumentation
  console.debug(
    `[voice-turn] end-of-speech -> first token: ${Math.round(marks.firstTokenAt - marks.endOfSpeechAt)}ms`,
  )
}

/** First TTS audio of the turn started playing. */
export function markFirstAudio(): void {
  const marks = liveMarks()
  if (!marks || marks.firstAudioAt !== null) return
  marks.firstAudioAt = now()
  // eslint-disable-next-line no-console -- debug-level latency instrumentation
  console.debug(
    `[voice-turn] end-of-speech -> first audio: ${Math.round(marks.firstAudioAt - marks.endOfSpeechAt)}ms`,
  )
}

/** Turn over (done, barged-in, or abandoned): drop the marks. */
export function clearTurnMarks(): void {
  marks = null
}

/** Test-visible snapshot of the current marks. */
export function turnMarks(): { endOfSpeechAt: number; firstTokenAt: number | null; firstAudioAt: number | null } | null {
  return marks ? { ...marks } : null
}
