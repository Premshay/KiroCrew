// @vitest-environment jsdom
// happy-dom drops SVG stylesheet siblings; only this SVG fixture needs jsdom.
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { Window } from 'happy-dom'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { api } from '../api/client'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { SvgViewer } from '../components/FileRenderers'
import type { Artifact } from '../types'
import { svgSelectionAnchor, svgSelectionRange } from '../utils/svgSelection'
import { renderWithProviders } from './helpers'
import { svgShadow } from './svgShadow'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const INPUT = 'Comment on the selected text'
const BODY = 'alpha beta gamma'
const STYLED_SVG = '<svg width="200" height="30"><style>text { fill: red }</style><text x="0" y="20" onclick="alert(1)">alpha beta gamma</text><script>alert(1)</script><foreignObject><p>unsafe</p></foreignObject></svg>'

// jsdom has no constructable stylesheet parser or range layout.
const cssParser = new Window()
const rangeRect = Object.getOwnPropertyDescriptor(Range.prototype, 'getBoundingClientRect')
beforeAll(() => {
  vi.stubGlobal('CSSStyleSheet', cssParser.CSSStyleSheet)
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true, value: () => new DOMRect(10, 10, 50, 20),
  })
})
afterAll(async () => {
  vi.unstubAllGlobals()
  if (rangeRect) Object.defineProperty(Range.prototype, 'getBoundingClientRect', rangeRect)
  else Reflect.deleteProperty(Range.prototype, 'getBoundingClientRect')
  await cssParser.happyDOM.close()
})
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).artifact = vi.fn().mockResolvedValue({
    slug: 'notes', name: 'Notes', kind: 'svg', content: STYLED_SVG,
    source: 'chat', description: '', tags: [], version: 2,
    created_at: '2026-05-21T22:00:00.000000+00:00',
    updated_at: '2026-05-21T22:30:00.000000+00:00',
  } satisfies Artifact)
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'notes', versions: [1, 2] })
  vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'notes', events: [] })
  vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
  vi.mocked(api).postArtifactComment = vi.fn().mockResolvedValue({ ok: true })
})

async function renderPage(outside = false) {
  const result = renderWithProviders(<>
    {outside && <SvgViewer content="<svg><text>outside selection</text></svg>" />}
    <Routes><Route path="/artifacts/:slug" element={<ArtifactDetailPage />} /></Routes>
  </>, { route: '/artifacts/notes' })
  await screen.findByLabelText('Toggle agent chat')
  const shadows = Array.from(result.container.querySelectorAll('[data-guide-untrusted]'))
    .map(el => el.shadowRoot).filter(root => root?.querySelector('svg'))
  return { ...result, shadow: shadows.at(-1)! }
}

function textRange(node: Node, word: string): Range {
  const start = node.textContent!.indexOf(word)
  expect(start).toBeGreaterThanOrEqual(0)
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + word.length)
  return range
}

function select(range: Range, collapsed = false, composed = true, renderedText = range.toString()) {
  const documentRange = document.createRange()
  const root = range.startContainer.getRootNode() as ShadowRoot
  documentRange.selectNode(root.host)
  documentRange.collapse(true)
  vi.spyOn(window, 'getSelection').mockReturnValue({
    isCollapsed: collapsed, rangeCount: 1,
    anchorNode: collapsed ? documentRange.startContainer : range.startContainer,
    getRangeAt: () => collapsed ? documentRange : range,
    getComposedRanges: composed ? () => [range] : undefined,
    removeAllRanges: () => undefined, addRange: () => undefined,
    toString: () => renderedText,
  } as unknown as Selection)
  const target = range.startContainer instanceof Element ? range.startContainer : range.startContainer.parentElement!
  fireEvent.mouseDown(target, { composed: true })
  fireEvent.mouseUp(target, { composed: true })
}

async function post(expected: { quote: string; prefix: string; suffix: string }) {
  fireEvent.change(await screen.findByLabelText(INPUT), { target: { value: 'clarify this step' } })
  fireEvent.click(screen.getByLabelText('Add comment'))
  await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.postArtifactComment).mock.calls[0]).toEqual(['notes', {
    text: 'clarify this step', scope: 'private', anchor: expected,
  }])
}

describe('SVG selection comments', () => {
  it.each([
    ['collapsed composed range', true, true],
    ['noncollapsed composed range', false, true],
    ['noncollapsed native range without composed API', false, false],
  ] as const)('anchors actual SVG shadow text: %s', async (_shape, collapsed, composed) => {
    const { shadow } = await renderPage()
    expect(document.querySelector('svg text')).toBeNull()
    expect(shadow.querySelector('script, foreignObject, [onclick]')).toBeNull()
    expect(shadow.querySelector('svg style')?.textContent).toContain('fill: red')
    const range = textRange(shadow.querySelector('text')!.firstChild!, 'beta')
    select(range, collapsed, composed)
    expect(svgSelectionRange(window.getSelection()!, shadow.host.parentElement!)?.toString()).toBe('beta')
    expect(range.startContainer.textContent).toBe(BODY)
    await post({ quote: 'beta', prefix: 'alpha ', suffix: ' gamma' })
  })

  it.each([true, false])('ignores another SVG shadow root (collapsed=%s)', async collapsed => {
    const { container } = await renderPage(true)
    const outside = svgShadow(container)!.querySelector('text')!.firstChild!
    vi.useFakeTimers()
    select(textRange(outside, 'outside'), collapsed)
    await act(() => vi.runOnlyPendingTimersAsync())
    expect(screen.queryByLabelText(INPUT)).not.toBeInTheDocument()
    expect(api.postArtifactComment).not.toHaveBeenCalled()
  })

  it('keeps context for a nested span selected through text element boundaries', async () => {
    const { shadow } = await renderPage()
    const label = shadow.querySelector('text')!
    const before = document.createTextNode('alpha ')
    const span = document.createElementNS('http://www.w3.org/2000/svg', 'tspan')
    span.textContent = 'beta'
    label.replaceChildren(before, span, document.createTextNode(' gamma'))
    const range = document.createRange()
    range.setStart(label, 1)
    range.setEnd(label, 2)
    expect(svgSelectionAnchor(range)).toEqual({ quote: 'beta', prefix: 'alpha ', suffix: ' gamma' })
    select(range, true)
    await post({ quote: 'beta', prefix: 'alpha ', suffix: ' gamma' })
  })

  it('excludes stylesheet text between selected labels from the quote and context', async () => {
    const { shadow } = await renderPage()
    const svg = shadow.querySelector('svg')!
    const label = svg.querySelector('text')!
    label.textContent = 'alpha beta '
    const last = label.cloneNode() as Element
    last.textContent = 'gamma delta'
    svg.append(svg.querySelector('style')!, last)
    const range = document.createRange()
    range.setStart(label.firstChild!, 6)
    range.setEnd(last.firstChild!, 5)
    expect(svgSelectionAnchor(range)).toEqual({ quote: 'beta gamma', prefix: 'alpha ', suffix: ' delta' })
    select(range, true, true, 'beta gamma')
    await post({ quote: 'beta gamma', prefix: 'alpha ', suffix: ' delta' })
  })

  it('trims a selection while keeping its whitespace in the surrounding context', async () => {
    const { shadow } = await renderPage()
    select(textRange(shadow.querySelector('text')!.firstChild!, ' beta '), true)
    await post({ quote: 'beta', prefix: 'alpha ', suffix: ' gamma' })
  })

  it('keeps a typed draft anchored to its original SVG selection', async () => {
    const { shadow } = await renderPage()
    const node = shadow.querySelector('text')!.firstChild!
    select(textRange(node, 'beta'), true)
    fireEvent.change(await screen.findByLabelText(INPUT), { target: { value: 'clarify this step' } })
    vi.useFakeTimers()
    select(textRange(node, 'gamma'), true)
    await act(() => vi.runOnlyPendingTimersAsync())
    vi.useRealTimers()
    expect(screen.getByLabelText(INPUT)).toHaveValue('clarify this step')
    fireEvent.click(screen.getByLabelText('Add comment'))
    await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.postArtifactComment).mock.calls[0][1].anchor).toEqual({
      quote: 'beta', prefix: 'alpha ', suffix: ' gamma',
    })
  })

  it('restores a draft only for the same occurrence of a repeated SVG quote', async () => {
    const first = await renderPage()
    const node = first.shadow.querySelector('text')!.firstChild!
    node.textContent = 'alpha beta gamma beta delta'
    select(textRange(node, 'beta'), true)
    fireEvent.change(await screen.findByLabelText(INPUT), { target: { value: 'first occurrence' } })
    first.unmount()

    const second = await renderPage()
    const again = second.shadow.querySelector('text')!.firstChild!
    again.textContent = 'alpha beta gamma beta delta'
    const range = document.createRange()
    range.setStart(again, 17)
    range.setEnd(again, 21)
    select(range, true)
    expect(await screen.findByLabelText(INPUT)).toHaveValue('')
    fireEvent.keyDown(screen.getByLabelText(INPUT), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByLabelText(INPUT)).toBeNull())
    select(textRange(again, 'beta'), true)
    expect(await screen.findByLabelText(INPUT)).toHaveValue('first occurrence')
  })

  it('rejects a collapsed document range when no composed range API is available', async () => {
    const { shadow } = await renderPage()
    vi.useFakeTimers()
    select(textRange(shadow.querySelector('text')!.firstChild!, 'beta'), true, false)
    await act(() => vi.runOnlyPendingTimersAsync())
    expect(svgSelectionRange(window.getSelection()!, shadow.host.parentElement!)).toBeNull()
    expect(screen.queryByLabelText(INPUT)).not.toBeInTheDocument()
  })
})
