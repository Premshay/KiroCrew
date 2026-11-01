import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import ClampText from './ClampText'

function layout(cut: boolean) {
  vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(cut ? 500 : 100)
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(100)
}

afterEach(() => vi.restoreAllMocks())

describe('ClampText', () => {
  it('opens cut text on a click and cuts it again on the next', () => {
    layout(true)
    const onRow = vi.fn()
    render(
      // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
      <div onClick={onRow}>
        <ClampText text="A very long goal that does not fit" />
      </div>,
    )
    const btn = screen.getByRole('button')
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(btn)
    expect(btn.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(btn)
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false')
    // The toggle never doubles as a click on its row.
    expect(onRow).not.toHaveBeenCalled()
  })

  it('leaves text that fits inert', () => {
    layout(false)
    render(<ClampText text="short" />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})
