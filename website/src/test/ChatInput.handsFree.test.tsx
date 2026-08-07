import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
/**
 * Hands-free (car mode) composer surfaces: the mode toggle next to the mic and
 * the loop-state strip. The test DOM does not evaluate media queries, so the
 * hover-none touch-target utilities are pinned as classes, same as the other
 * mobile fixes.
 */

const base = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false,
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
})

describe('ChatInput — hands-free toggle', () => {
  it('renders next to the mic when available, and fires its handler', () => {
    const onHandsFreeToggle = vi.fn()
    renderWithProviders(
      <ChatInput {...base} handsFreeAvailable onHandsFreeToggle={onHandsFreeToggle} onVoiceToggle={vi.fn()} />,
    )
    const btn = screen.getByTestId('handsfree-toggle')
    expect(btn).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(btn)
    expect(onHandsFreeToggle).toHaveBeenCalledTimes(1)
  })

  it('is absent when hands-free is unavailable (streaming STT, config off)', () => {
    renderWithProviders(
      <ChatInput {...base} handsFreeAvailable={false} onHandsFreeToggle={vi.fn()} onVoiceToggle={vi.fn()} />,
    )
    expect(screen.queryByTestId('handsfree-toggle')).toBeNull()
  })

  it('reflects the persisted preference via aria-pressed', () => {
    renderWithProviders(
      <ChatInput {...base} handsFreeAvailable handsFreeOn onHandsFreeToggle={vi.fn()} onVoiceToggle={vi.fn()} />,
    )
    expect(screen.getByTestId('handsfree-toggle')).toHaveAttribute('aria-pressed', 'true')
  })

  it('grows to the 40px touch floor where the pointer cannot hover', () => {
    renderWithProviders(
      <ChatInput {...base} handsFreeAvailable onHandsFreeToggle={vi.fn()} onVoiceToggle={vi.fn()} />,
    )
    expect(screen.getByTestId('handsfree-toggle').className)
      .toContain('[@media(hover:none)]:w-10')
  })
})

describe('ChatInput — hands-free status strip', () => {
  it('is absent while the loop is not armed', () => {
    renderWithProviders(
      <ChatInput {...base} handsFreeAvailable onHandsFreeToggle={vi.fn()} onVoiceToggle={vi.fn()} />,
    )
    expect(screen.queryByTestId('handsfree-status')).toBeNull()
  })

  it('cycles listening → processing → sent with the stop hint always present', () => {
    const props = { ...base, handsFreeAvailable: true, onHandsFreeToggle: vi.fn(), onVoiceToggle: vi.fn() }
    const { rerender } = renderWithProviders(
      <ChatInput {...props} handsFreePhase="listening" voiceRecording />,
    )
    expect(screen.getByTestId('handsfree-status')).toHaveTextContent('Listening, pause to send')
    expect(screen.getByTestId('handsfree-status')).toHaveTextContent('Tap the mic to stop')

    rerender(<ChatInput {...props} handsFreePhase="processing" voiceTranscribing />)
    expect(screen.getByTestId('handsfree-status')).toHaveTextContent('Transcribing')

    rerender(<ChatInput {...props} handsFreePhase="sent" />)
    expect(screen.getByTestId('handsfree-status')).toHaveTextContent('Sent')
  })

  it('keeps the mic tappable during hands-free transcription (it is the loop stop control)', () => {
    renderWithProviders(
      <ChatInput
        {...base}
        handsFreeAvailable
        onHandsFreeToggle={vi.fn()}
        onVoiceToggle={vi.fn()}
        voiceTranscribing
        handsFreePhase="processing"
      />,
    )
    expect(screen.getByLabelText('Stop hands-free listening')).toBeEnabled()
  })

  it('still disables the mic during a plain (non-hands-free) transcription', () => {
    renderWithProviders(
      <ChatInput {...base} onVoiceToggle={vi.fn()} voiceTranscribing />,
    )
    expect(screen.getByLabelText('Transcribing…')).toBeDisabled()
  })
})
