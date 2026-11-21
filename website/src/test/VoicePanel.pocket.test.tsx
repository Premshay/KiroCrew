import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const voiceConfig = {
  enabled: false,
  provider: 'pocket',
  voice: 'michael',
  engine: 'generative',
  rate: '100%',
  autoSpeak: false,
  aws_profile: '',
  region: '',
  piper_binary: '/home/test/bin/kokoro-piper',
  piper_model: '/home/test/models/kokoro.onnx',
  piper_model_config: '',
  piper_length_scale: 1,
}

const { updateVoiceConfig } = vi.hoisted(() => ({ updateVoiceConfig: vi.fn() }))

vi.mock('../api/client', () => ({
  api: {
    voiceConfig: () => Promise.resolve(voiceConfig),
    updateVoiceConfig,
  },
}))

vi.mock('../pages/settings/SttSettings', () => ({ default: () => null }))

import { VoicePanel } from '../pages/settings/VoicePanel'

describe('VoicePanel Pocket settings', () => {
  it('shows Pocket voice without Piper-only controls', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><VoicePanel /></QueryClientProvider>)

    expect(await screen.findByDisplayValue('michael')).toBeInTheDocument()
    expect(screen.queryByText('Piper Model')).not.toBeInTheDocument()
    expect(screen.queryByText('Piper Binary')).not.toBeInTheDocument()
    expect(screen.queryByText('Piper speech speed (length scale)')).not.toBeInTheDocument()
  })

  it('keeps the Pocket voice draft when the save fails', async () => {
    updateVoiceConfig.mockRejectedValueOnce(new Error('save failed'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><VoicePanel /></QueryClientProvider>)

    const field = await screen.findByDisplayValue('michael')
    fireEvent.change(field, { target: { value: 'draft voice' } })
    fireEvent.blur(field)

    expect(await screen.findByText('Failed to save voice config')).toBeInTheDocument()
    expect(updateVoiceConfig).toHaveBeenCalledWith({ voice: 'draft voice' })
    expect(field).toHaveValue('draft voice')
    await waitFor(() => expect(client.getQueryData(['voiceConfig'])).toEqual(voiceConfig))
  })
})
