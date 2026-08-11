import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
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

vi.mock('../api/client', () => ({
  api: {
    voiceConfig: () => Promise.resolve(voiceConfig),
    updateVoiceConfig: () => Promise.resolve(voiceConfig),
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
})
