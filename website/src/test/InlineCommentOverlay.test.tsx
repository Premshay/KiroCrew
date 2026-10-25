import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { useRef } from 'react'
import { InlineCommentOverlay } from '../components/InlineCommentOverlay'
import type { ArtifactComment } from '../types'
import { codeBlock } from './pierreDom'

class FakeRO {
  observe() {}
  unobserve() {}
  disconnect() {}
}

let origRaf: typeof globalThis.requestAnimationFrame
let origCaf: typeof globalThis.cancelAnimationFrame

beforeEach(() => {
  globalThis.ResizeObserver = FakeRO
  origRaf = globalThis.requestAnimationFrame
  origCaf = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 1 }) as typeof globalThis.requestAnimationFrame
  globalThis.cancelAnimationFrame = (() => {}) as typeof globalThis.cancelAnimationFrame
})

afterEach(() => {
  globalThis.requestAnimationFrame = origRaf
  globalThis.cancelAnimationFrame = origCaf
})

function mk(over: Partial<ArtifactComment> = {}): ArtifactComment {
  return {
    id: 'c1', origin: 'local', scope: 'private', author: 'alice', is_agent: false,
    body: 'b', thread_id: 'c1', status: 'open', sync_state: 'local_only',
    created_at: '', updated_at: '', ...over,
  }
}

function Harness({ comments }: { comments: ArtifactComment[] }) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const textRef = useRef<HTMLDivElement>(null)
  return (
    <div ref={scrollRef} style={{ position: 'relative' }}>
      <div ref={textRef}>The quick brown fox jumps over the lazy dog</div>
      <InlineCommentOverlay
        scrollRef={scrollRef}
        textRef={textRef}
        comments={comments}
        activeId={null}
        onActivate={vi.fn()}
      />
    </div>
  )
}

describe('InlineCommentOverlay', () => {
  it('paints a native anchor when the scroll root and overlay mount together', () => {
    const rect = vi.spyOn(Range.prototype, 'getClientRects').mockReturnValue([
      new DOMRect(10, 20, 30, 16),
    ] as unknown as DOMRectList)
    function NativeHarness() {
      const scrollRef = useRef<HTMLDivElement>(null)
      const textRef = useRef<HTMLDivElement>(null)
      return <div ref={scrollRef}>
        <div ref={node => {
          textRef.current = node
          if (node && !node.firstChild) node.append(codeBlock([['same'], ['same']]))
        }} />
        <InlineCommentOverlay scrollRef={scrollRef} textRef={textRef} sourceContent={'same\nsame'}
          comments={[mk({ anchor: { quote: 'same', start_offset: 5 } })]} activeId={null} onActivate={vi.fn()} />
      </div>
    }
    try {
      const { container } = render(<NativeHarness />)
      expect(container.querySelector('.mc-cmt-rect')).not.toBeNull()
    } finally { rect.mockRestore() }
  })

  it('mounts the overlay layer for an anchored comment without crashing', () => {
    const c = mk({ id: 'a1', anchor: { quote: 'quick brown' } })
    const { container } = render(<Harness comments={[c]} />)
    expect(container.querySelector('.mc-cmt-overlay')).not.toBeNull()
  })

  it('paints no anchor for a resolved comment', () => {
    const c = mk({ id: 'a1', anchor: { quote: 'quick brown' }, status: 'resolved' })
    const { container } = render(<Harness comments={[c]} />)
    expect(container.querySelector('.mc-cmt-rect')).toBeNull()
    expect(container.querySelector('.mc-cmt-bubble')).toBeNull()
  })
})
