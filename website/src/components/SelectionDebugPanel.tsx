import { useSyncExternalStore } from 'react'
import { selectionDebugEnabled, selectionDebugEntries, subscribeSelectionDebug } from '../utils/selectionDebug'

/** Fixed readout of the selection trace; renders nothing unless `?debugSelection=1`. */
export default function SelectionDebugPanel({ windowRange }: { windowRange: { start: number; end: number } }) {
  const entries = useSyncExternalStore(subscribeSelectionDebug, selectionDebugEntries, selectionDebugEntries)
  if (!selectionDebugEnabled()) return null
  return (
    <div
      data-testid="selection-debug-panel"
      className="fixed left-1 right-1 top-14 z-[60] pointer-events-none select-none rounded-md bg-bg-elevated/90 border border-border p-1.5 font-mono text-[10px] leading-[13px] text-text"
    >
      {/* i18n-ignore: developer diagnostic, not product copy */}
      <div>{`window ${windowRange.start}-${windowRange.end}`}</div>
      {entries.map((e, i) => (
        <div key={i}>{`${e.t} a:${e.anchor} f:${e.focus} o:${e.offsets} r:${e.retained}`}</div>
      ))}
    </div>
  )
}
