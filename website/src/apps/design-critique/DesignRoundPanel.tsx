import { useState } from 'react'
import { ClipboardCopy, ExternalLink, PackageCheck, Play, Send, Sparkle } from 'lucide-react'

import { S } from './styles'
import type { DesignRound, ReviewBrief, Report } from './types'

interface Props {
  brief: ReviewBrief
  report: Report
  reviewRunId: string
  rounds: DesignRound[]
  busy: boolean
  onPrepare: (input: {
    mode: 'generate-design' | 'generate-prototype'
    target: string
    claude_design_url: string
    handoff_path: string
  }) => void
  onUpdate: (roundId: string, update: Partial<DesignRound>) => void
}

const statusLabel: Record<DesignRound['status'], string> = {
  prepared: 'Prepared — no external work started',
  owner_send_confirmed: 'Open in Claude Design — send remains your explicit action',
  building: 'Building — watch through DesignSync file listing',
  ready_to_harvest: 'Ready to harvest — verify the bundle before importing',
  harvested: 'Harvested',
  interrupted: 'Interrupted — resume from the same Claude Design chat',
  failed: 'Failed',
}

export default function DesignRoundPanel(p: Props) {
  const [mode, setMode] = useState<'generate-design' | 'generate-prototype'>('generate-design')
  const [target, setTarget] = useState(p.brief.targets)
  const [designUrl, setDesignUrl] = useState('')
  const [handoffPath, setHandoffPath] = useState('')
  const [evidenceFiles, setEvidenceFiles] = useState('')
  const [evidenceNote, setEvidenceNote] = useState('')
  const latest = p.rounds[0]

  const copy = async (text: string) => {
    try { await navigator.clipboard.writeText(text) } catch { /* Clipboard permissions are browser-owned. */ }
  }

  const confirmAndOpen = (designRound: DesignRound) => {
    if (!designRound.claude_design_url) return
    window.open(designRound.claude_design_url, '_blank', 'noopener,noreferrer')
    void copy(designRound.prompt)
    p.onUpdate(designRound.id, { status: 'owner_send_confirmed' })
  }

  return (
    <details style={{ ...S.contextBuilder, marginTop: '12px' }}>
      <summary style={S.contextSummary}>Claude Design round</summary>
      <div style={{ display: 'grid', gap: '9px', paddingTop: '10px' }}>
        <p style={{ ...S.cardHint, margin: 0 }}>
          Prepare an immutable, grounded round from this critique. Opening Claude Design and sending it are explicit owner actions; DesignSync’s file list is the source of truth while it builds.
        </p>
        <select aria-label="Claude Design output mode" style={S.contextInput} value={mode} disabled={p.busy} onChange={(event) => setMode(event.target.value as typeof mode)}>
          <option value="generate-design">Static high-fidelity design</option>
          <option value="generate-prototype">Interactive prototype</option>
        </select>
        <textarea
          style={{ ...S.contextInput, minHeight: '56px', resize: 'vertical' }} value={target}
          disabled={p.busy} placeholder="What should this round design or transform?"
          aria-label="Claude Design round target" onChange={(event) => setTarget(event.target.value)}
        />
        <input
          style={S.contextInput} value={designUrl} disabled={p.busy}
          placeholder="https://claude.ai/design/p/<project-id>"
          aria-label="Claude Design project URL" onChange={(event) => setDesignUrl(event.target.value)}
        />
        <input
          style={S.contextInput} value={handoffPath} disabled={p.busy}
          placeholder="Repository handoff directory (optional)"
          aria-label="Repository handoff directory" onChange={(event) => setHandoffPath(event.target.value)}
        />
        <div style={S.contextActions}>
          <button
            style={S.linkBtn} disabled={p.busy}
            onClick={() => p.onPrepare({ mode, target, claude_design_url: designUrl, handoff_path: handoffPath })}
          ><Sparkle size={13} />Prepare round</button>
        </div>

        {latest ? (
          <div style={{ borderTop: '1px solid var(--border)', paddingTop: '10px', display: 'grid', gap: '8px' }}>
            <div style={{ fontSize: '12px', color: 'var(--muted)' }}>{statusLabel[latest.status]}</div>
            <textarea readOnly style={{ ...S.contextInput, minHeight: '168px', resize: 'vertical', fontFamily: 'var(--mono, monospace)', fontSize: '11px' }} value={latest.prompt} aria-label="Prepared Claude Design brief" />
            <div style={S.contextActions}>
              <button style={S.linkBtn} onClick={() => void copy(latest.prompt)}><ClipboardCopy size={13} />Copy brief</button>
              {latest.status === 'prepared' ? <button style={S.linkBtn} disabled={!latest.claude_design_url || p.busy} onClick={() => confirmAndOpen(latest)}><ExternalLink size={13} />Confirm and open</button> : null}
              {latest.status === 'owner_send_confirmed' ? <button style={S.linkBtn} disabled={p.busy} onClick={() => p.onUpdate(latest.id, { status: 'building' })}><Send size={13} />I sent it</button> : null}
              {latest.status === 'building' ? <button style={S.linkBtn} disabled={p.busy} onClick={() => p.onUpdate(latest.id, { status: 'ready_to_harvest' })}><Play size={13} />Bundle is ready</button> : null}
            </div>
            {latest.status === 'ready_to_harvest' || latest.status === 'harvested' ? (
              <>
                <textarea
                  style={{ ...S.contextInput, minHeight: '54px', resize: 'vertical' }} value={evidenceFiles}
                  placeholder="Observed bundle files, one per line"
                  aria-label="Observed handoff bundle files" onChange={(event) => setEvidenceFiles(event.target.value)}
                />
                <textarea
                  style={{ ...S.contextInput, minHeight: '54px', resize: 'vertical' }} value={evidenceNote}
                  placeholder="Harvest location and verification note"
                  aria-label="Harvest verification note" onChange={(event) => setEvidenceNote(event.target.value)}
                />
                <button style={S.linkBtn} disabled={p.busy} onClick={() => p.onUpdate(latest.id, {
                  status: 'harvested', evidence: { files: evidenceFiles.split('\n').map((file) => file.trim()).filter(Boolean), note: evidenceNote },
                })}><PackageCheck size={13} />Record harvested bundle</button>
              </>
            ) : null}
          </div>
        ) : null}
      </div>
    </details>
  )
}
