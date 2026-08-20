import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', () => ({
  api: { phoneLoginLink: vi.fn() },
}))

import { api } from '../../api/client'
import { PhoneLoginCard } from './PhoneLoginCard'

describe('PhoneLoginCard', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('creates and copies the configured external dashboard link', async () => {
    const phoneLink = 'https://nexus.tailbfda76.ts.net/?token=abc.def'
    ;(api.phoneLoginLink as ReturnType<typeof vi.fn>).mockResolvedValue({ url: phoneLink })
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.spyOn(navigator.clipboard, 'writeText').mockImplementation(writeText)

    renderWithProviders(<PhoneLoginCard />)
    fireEvent.click(screen.getByRole('button', { name: 'Create phone sign-in link' }))

    const link = await screen.findByLabelText('Phone sign-in link')
    expect(link).toHaveValue(phoneLink)

    fireEvent.click(screen.getByRole('button', { name: 'Copy sign-in link' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(phoneLink))
    expect(await screen.findByRole('status')).toHaveTextContent('Link copied')
  })

  it('shows a retryable error when the dashboard cannot mint the link', async () => {
    ;(api.phoneLoginLink as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('offline'))

    renderWithProviders(<PhoneLoginCard />)
    fireEvent.click(screen.getByRole('button', { name: 'Create phone sign-in link' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not create a sign-in link. Try again.',
    )
  })

  it('explains how to copy manually when clipboard access is unavailable', async () => {
    ;(api.phoneLoginLink as ReturnType<typeof vi.fn>).mockResolvedValue({
      url: 'https://nexus.tailbfda76.ts.net/?token=abc.def',
    })
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('denied'))

    renderWithProviders(<PhoneLoginCard />)
    fireEvent.click(screen.getByRole('button', { name: 'Create phone sign-in link' }))
    await screen.findByLabelText('Phone sign-in link')
    fireEvent.click(screen.getByRole('button', { name: 'Copy sign-in link' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Copy failed. Select the link and copy it manually.',
    )
  })
})
