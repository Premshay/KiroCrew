/**
 * The main composer's voice host routes an off-screen transcript for a crew
 * window key (one that settles after the window closed) into that crew
 * session's own draft, never into a local draft no screen shows. The store
 * side is pinned in crewWindowTranscript.test.ts; this pins that the host
 * actually takes that route (composerHost.ts, `voiceDeliverOffScreen`).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import { createTestStore } from './helpers'
import { useComposerVoiceHost } from '../pages/chat/page/composerHost'
import { readCrewDraft, writeCrewDraft } from '../pages/chat/crew-window/crewWindowStore'
import { crewWindowSlot } from '../pages/chat/crew-window/crewWindowRenderers'
import { setVoiceBusy } from '../store/chatSlice'

const target = { instanceId: 'devbox', key: 'chat-7' }
const append = (text: string) => (base: string) => (base ? base + ' ' + text : text)

function renderHost() {
  const drafts = { current: {} as Record<string, string> }
  const saveDrafts = vi.fn()
  const inputRef = { current: '' }
  const store = createTestStore()
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const view = renderHook(() => useComposerVoiceHost({
    drafts, saveDrafts, inputRef,
    activeSlotRef: { current: 'chat-1' }, composerSlotRef: { current: 'chat-1' },
  }), { wrapper })
  return { deliver: view.result.current.composerVoiceOptions.deliverOffScreen!, drafts, saveDrafts, inputRef, view, store }
}

describe('useComposerVoiceHost off-screen delivery', () => {
  beforeEach(() => { writeCrewDraft(target, '') })

  it('lands a closed crew window\'s transcript in that session\'s crew draft', () => {
    writeCrewDraft(target, 'typed first')
    const h = renderHost()
    h.deliver(crewWindowSlot(target.instanceId, target.key), append('then spoken'))
    expect(readCrewDraft(target)).toBe('typed first then spoken')
    // No local draft is written for a key no local slot owns.
    expect(h.drafts.current).toEqual({})
    expect(h.saveDrafts).not.toHaveBeenCalled()
    expect(h.inputRef.current).toBe('')
  })

  it('keeps a local slot\'s transcript in the local draft', () => {
    const h = renderHost()
    h.deliver('chat-3', append('local words'))
    expect(h.drafts.current['chat-3']).toBe('local words')
    expect(h.saveDrafts).toHaveBeenCalledTimes(1)
    expect(readCrewDraft(target)).toBe('')
  })
})

describe('main composer spoken conversation', () => {
  it('offers hands-free capture and sends spoken text without replacing a draft', () => {
    const h = renderHost()
    h.inputRef.current = 'unfinished keyboard draft'
    const send = vi.fn()
    h.view.result.current.voiceConversationSendRef.current = send
    const conversation = h.view.result.current.composerVoiceOptions.handsFree!
    expect(conversation).toBeDefined()
    expect(conversation.assistantHoldsFloor).toBe(false)
    conversation.send('spoken request')
    expect(send).toHaveBeenCalledWith('spoken request')
    expect(h.inputRef.current).toBe('unfinished keyboard draft')
  })

  it('keeps the microphone closed while reply audio is busy', () => {
    const h = renderHost()
    act(() => { h.store.dispatch(setVoiceBusy(true)) })
    expect(h.view.result.current.composerVoiceOptions.handsFree?.assistantHoldsFloor).toBe(true)
    act(() => { h.store.dispatch(setVoiceBusy(false)) })
    expect(h.view.result.current.composerVoiceOptions.handsFree?.assistantHoldsFloor).toBe(false)
  })
})
