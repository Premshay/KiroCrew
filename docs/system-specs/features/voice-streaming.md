# Voice Streaming

## Overview

Dashboard text-to-speech has three providers: local Piper, a locally configured
Pocket runtime, and Amazon Polly. `voice_reply.DEFAULT_PROVIDER` selects Piper
unless configuration selects a valid provider.
`chat_voice.api_voice_synthesize()` sends Piper output as one WAV chunk and
streams Polly sentence chunks as MP3; the browser queues either form for
sequential playback. Polly auto-speak starts as soon as the first sentence
finishes streaming; Pocket starts a manual replay from the first progressively
encoded Ogg Opus bytes. Sending a new message interrupts playback immediately.

## Components

| Component | Code | Responsibility |
|---|---|---|
| Dashboard routes | `dashboard.routes.sessions.register()` | Registers the synthesis, replay, configuration, and Polly voice-catalogue endpoints. |
| Voice endpoints | `dashboard.chat_voice.api_voice_config()`, `api_voice_synthesize()`, `api_voice_replay()`, and `api_voice_voices()` | Read and persist configuration, synthesize dashboard speech, mint the Pocket replay URL, and return the Polly catalogue. |
| Provider implementation | `voice_reply.synthesize_speech()`, `streaming_voice_reply()`, `stream_pocket_speech()`, and `stitch_mp3s()` | Redacts text, selects a provider, creates audio, streams Pocket Ogg Opus through the local compatibility executable, and joins completed Polly chunks. |
| Sentence cutter | `website/src/hooks/sentenceCutter.ts` | Pure boundary logic: where the next speakable span ends (see Dashboard auto-speak). |
| Streaming playback | `website/src/hooks/useWebSocket.ts` | Feeds streamed text through the cutter, serializes synthesis requests, queues audio, and handles interruption. |
| Turn-taking hold | `website/src/hooks/useHandsFreeLoop.ts` and `website/src/pages/ChatPage.tsx` | Hands-free conversation mode: the mic stays closed while the reply speaks, with barge-in on a mic tap. |
| Turn latency marks | `website/src/utils/voiceTurnMetrics.ts` | Debug-level per-turn spans: end-of-speech to first token and to first audio. |
| Settings | `website/src/pages/settings/VoicePanel.tsx` | Updates auto-speak, provider, Polly, Pocket, and Piper settings; fetches the Polly catalogue only while Polly is selected. |
| Slack reply | `slack.handler.handle_message()` and `_safe_voice_reply()` | Starts a background provider-aware voice reply when thread, global, or voice-input settings allow it. |

## Dashboard auto-speak

`useWebSocket` buffers `chat_chunk` text and, after it updates the Redux
streaming message, scans the active slot for completed sentence boundaries. It
submits only text beyond `voiceProgressRef.spokenLen` through
`enqueueVoiceSynthesis()`. The progress record is keyed by slot and message
identity: this prevents an old segment or a background slot from replaying text
or resetting the active response.

`sentenceCutter.ts` owns the boundary rule. It cuts on terminal punctuation
(`[.!?]` followed by whitespace or end-of-text) only, and never cuts on a
guarded terminal: abbreviations (`e.g.`, `Dr.`), bare list enumerators (`1.`,
`a.` opening a line), and any terminal inside an unbalanced ``` fence — code is
held whole for the completion pass, whose server-side strip sees the balanced
fence and speaks its placeholder. A span shorter than `MIN_TTS_CHARS` (10) is
not spoken.

`flushVoiceTail()` handles the remaining eligible text at `chat_segment` and
`chat_done`. It marks the message consumed even when the tail does not meet the
speech floor, so a later completion event cannot retry it. The floor and
boundary rule are implemented in `useWebSocket.ts`; they are not duplicated
here.

`enqueueVoiceSynthesis()` appends each request to `synthChainRef`. This keeps
requests in source order even if a provider finishes them out of order, which is
load-bearing because the playback queue cannot reconstruct the intended
sentence order after receiving audio.

For Polly, `api_voice_synthesize()` iterates
`voice_reply.streaming_voice_reply()`, broadcasts each `voice_chunk`, then
uses `stitch_mp3s()` to broadcast `voice_complete`. For Piper,
`_synthesize_nonstreaming()` broadcasts one WAV `voice_chunk` and one
`voice_complete`. `useWebSocket` decodes `voice_chunk` audio into blob URLs and
plays the queue one item at a time. `voice_complete` also updates the Redux
`voiceAudio` field; `UseWebSocketCoverage.test.tsx` covers that state update.

## Interruption

`ChatPage` dispatches `voice-stop` when it sends a message, and its Speak
handler dispatches the same event while audio is playing. `useWebSocket` maps
the event to `stopVoice()`, which pauses the active audio element, revokes
queued blob URLs, clears the queue, and sets `voiceMutedRef`.

`ChatPage` also dispatches `voice-stop` on the barge-in mic tap during a
hands-free reply. `stopVoice()` additionally aborts every in-flight
`POST /api/voice/synthesize` through an `AbortController` (`synthAbortRef`, one
per speaking span), so an interrupt cancels work that has not yet produced
audio rather than only silencing what already has.

## Manual replay

The Speak button appears on hover over assistant messages of at least 50
characters. It first asks `api.voiceReplay(slot, content)` for a provider-aware
playback path: Pocket returns a one-time Ogg Opus URL the browser plays as it
grows, while Piper and Polly fall back to `api.voiceSynthesize(slot, content)`.
Manual replay works independently of auto-speak.

While muted, `voice_chunk` frames are discarded and the `chat_segment`/
`chat_done` tail paths do not synthesize more text. `voiceProgressFor()` clears
the muted state only when it sees a different message identity. This identity
boundary is load-bearing: it prevents late audio from an interrupted response
from being played as though it belonged to the next response.

## Hands-free conversation turn-taking

With hands-free dictation armed AND auto-speak on, the loop runs fast
turn-taking (half-duplex — never listening while speaking):

- **Hold.** `chat.voiceBusy` is true while the TTS pipeline is active
  end-to-end: a synthesis POST in flight (`synthInFlightRef`), chunks queued, or
  audio playing. `ChatPage` passes `hold = autoSpeak && (slotRunning ||
  voiceBusy)` into `useHandsFreeLoop`, whose re-arm cycle will not open the mic
  while held. A capture already in progress is never interrupted by a hold
  arriving mid-utterance. `voiceBusy` (not `voicePlaying`) is the hold signal
  because playing goes false in the gap between requesting a sentence's
  synthesis and its audio arriving.
- **Re-arm.** When the turn ends and the audio queue drains, the hold drops
  and the ordinary re-arm cycle (400 ms delay) reopens the mic.
- **Barge-in.** While the reply speaks, the mic button is the interrupt: the
  tap fires `voice-stop` (stops audio, flushes the queue, aborts synthesis)
  and sets a turn-scoped `bargedIn` flag so the hold does not re-assert while
  the interrupted turn is still streaming; the loop re-arms the mic instead
  of exiting. The flag clears when the turn and pipeline are fully idle.
- **Phase.** The composer strip shows a `speaking` phase (with a
  tap-to-interrupt hint) whenever the loop is armed and held.
- **Latency marks.** The endpointer's auto-commit anchors a per-turn clock
  (`voiceTurnMetrics.ts`); the first `chat_chunk` for the active slot and the
  first audio `play()` log debug-level `end-of-speech → first token / first
  audio` spans. Marks expire after 30 s; dead-end auto-sends clear them.

With auto-speak off, hands-free keeps its dictate-anytime behavior — no hold.

## Configuration and API

Configuration is stored under `voice_reply` in the Crew configuration file.
`slack.handler.load_voice_reply_config()` loads the live `_VoiceConfig`, and
`api_voice_config()` merges a partial update back into that section rather than
replacing it. The merge preserves voice settings owned by other channels.

| Setting | Meaning |
|---|---|
| `provider` | Validated by `voice_reply.synthesis_settings()` and `slack.handler.load_voice_reply_config()`; invalid values fall back to `voice_reply.DEFAULT_PROVIDER`. |
| `enabled` | Enables global Slack voice replies. |
| `auto_speak` | Enables dashboard auto-speak; `api_voice_config()` exposes it as `autoSpeak`. |
| `voice_id`, `engine`, `rate`, `pitch` | Polly synthesis settings, also usable as request overrides for the dashboard synthesis endpoint. |
| `aws_profile`, `region` | Passed to the AWS CLI by the Polly provider. |
| `piper_binary`, `piper_model`, `piper_model_config`, `piper_length_scale` | Piper executable, model, optional model configuration, and validated speed setting. `validate_length_scale()` rejects invalid or non-positive values. |
| `provider` — Pocket | `pocket` is a locally supplied runtime reached through a Piper-compatible bridge, so it reuses `piper_binary` / `piper_model` and speaks the supplied local `michael` voice. It needs no AWS credential. |

`dashboard.routes.sessions.register()` registers:

* `GET` and `PUT /api/voice/config`
* `POST /api/voice/synthesize`
* `POST /api/voice/replay` — `{ slot, text }`; returns either `legacy` or a
  one-time Pocket Ogg Opus URL
* `GET /api/voice/replay/{job_id}` — streams that single-use Pocket replay; it
  expires after two minutes
* `GET /api/voice/voices`

`api_voice_voices()` caches a successful Polly catalogue in process, sorts it by
language code and name, and does not cache the empty result produced when the
AWS CLI is unavailable. It checks that Polly is the active provider and that
`aws_consent.refuse_and_log()` grants consent before it invokes
`aws polly describe-voices`. Those gates keep a direct API request from
silently using ambient AWS credentials for a provider the operator did not
select or authorize.

## Provider safety

`voice_reply.synthesize_speech()` redacts credentials and suspicious URLs before
provider selection. `text_to_ssml()` and `strip_markdown()` then produce
speakable text. `strip_markdown()` replaces fenced code, diff blocks, widgets,
tables, path-like inline code, and links with spoken placeholders or labels and
removes option markers, emoji, formatting markers, and diff hunk headers. The
thresholds and pattern details remain in `voice_reply.strip_markdown()`.

`_synthesize_polly()` calls `aws_consent.refuse_and_log()` before resolving or
spawning the AWS CLI. It returns no audio when consent is absent, which lets its
callers retain their text response rather than spending through an unattended
path.

`_synthesize_polly()` and `_synthesize_piper()` run their commands through
`wrap_argv_async(..., _prepare=wrap_argv)` and catch
`SandboxUnavailableError` separately from provider failures. They log the
sandbox error kind and its own message. The distinction is load-bearing because
only the sandbox layer can distinguish a missing backend from transient
pressure or an existing outer sandbox, and therefore provides the applicable
remedy.

## Slack voice replies

`slack.handler` accepts `!voice` thread commands for enabling and disabling a
thread, toggling global replies, and choosing a voice, engine, speed, or pitch.
`handle_message()` starts `_safe_voice_reply()` as a background task when a
thread or global setting enables replies, or when voice-input reply settings
allow a transcribed voice message to receive audio. `_safe_voice_reply()` calls
the provider-aware `voice_reply.voice_reply()` path, so Slack replies follow
the selected provider rather than assuming Polly.
