import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import ModelDropdownList from '../components/ModelDropdownList'
import { JEV_ROUTE_MODEL } from '../lib/jevRoute'
import { i18nT } from '../i18n/t'

describe('model picker labels and selection values', () => {
  it('shows the advertised name and description while selecting the wire ID', () => {
    const name = '["deepseek-official","deepseek-v4-pro"]'
    const onSelect = vi.fn()
    render(<ModelDropdownList
      models={[{ name, label: 'DeepSeek V4 Pro', description: 'Reasoning model' }]}
      activeModel={name}
      onSelect={onSelect}
    />)

    const option = screen.getByRole('option', { name: /DeepSeek V4 Pro/ })
    expect(option).toHaveAttribute('aria-selected', 'true')
    expect(option).toHaveTextContent('Reasoning model')
    expect(option).not.toHaveTextContent(name)
    expect(option.querySelector('[data-model-id]')).toHaveAttribute('data-model-id', name)
    fireEvent.click(option)
    expect(onSelect).toHaveBeenCalledWith(name)
  })

  it('uses the model half of a route when no advertised label is available', () => {
    render(<ModelDropdownList
      models={[{ name: '["deepseek-official","deepseek-v4-pro"]' }]}
      activeModel=""
      onSelect={vi.fn()}
    />)
    expect(screen.getByRole('option')).toHaveTextContent('deepseek-v4-pro')
    expect(screen.getByRole('option')).not.toHaveTextContent('deepseek-official')
  })

  it('keeps Jev routing translated even when its row carries a label', () => {
    render(<ModelDropdownList
      models={[{ name: JEV_ROUTE_MODEL, label: 'Internal routing label' }]}
      activeModel=""
      onSelect={vi.fn()}
    />)
    expect(screen.getByRole('option')).toHaveTextContent(i18nT('components.modelDropdownList.auto_jev'))
    expect(screen.getByRole('option')).not.toHaveTextContent('Internal routing label')
  })
})
