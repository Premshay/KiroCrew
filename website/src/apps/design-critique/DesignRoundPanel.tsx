import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ClipboardCopy, ExternalLink, PackageCheck, Play, Send, Sparkle } from 'lucide-react'

import SimpleSelect from '../../components/SimpleSelect'
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

export default function DesignRoundPanel(p: Props) {
  const { t } = useTranslation()
  const statusLabel: Record<DesignRound['status'], string> = {
    prepared: t('designSync.prepared'),
    owner_send_confirmed: t('designSync.owner_send_confirmed'),
    building: t('designSync.building'),
    ready_to_harvest: t('designSync.ready_to_harvest'),
    harvested: t('designSync.harvested'),
    interrupted: t('designSync.interrupted'),
    failed: t('designSync.failed'),
  }
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
      <summary style={S.contextSummary}>{t('designSync.round')}</summary>
      <div style={{ display: 'grid', gap: '9px', paddingTop: '10px' }}>
        <p style={{ ...S.cardHint, margin: 0 }}>
          {t('designSync.round_help')}
        </p>
        <SimpleSelect
          aria-label={t('designSync.output_mode')}
          style={S.contextInput}
          value={mode}
          disabled={p.busy}
          options={['generate-design', 'generate-prototype']}
          optionLabels={[t('designSync.static_design'), t('designSync.prototype')]}
          onChange={(value) => setMode(value as typeof mode)}
        />
        <textarea
          style={{ ...S.contextInput, minHeight: '56px', resize: 'vertical' }} value={target}
          disabled={p.busy} placeholder={t('designSync.target_placeholder')}
          aria-label={t('designSync.target')} onChange={(event) => setTarget(event.target.value)}
        />
        <input
          style={S.contextInput} value={designUrl} disabled={p.busy}
          placeholder="https://claude.ai/design/p/<project-id>"
          aria-label={t('designSync.project_url')} onChange={(event) => setDesignUrl(event.target.value)}
        />
        <input
          style={S.contextInput} value={handoffPath} disabled={p.busy}
          placeholder={t('designSync.handoff_placeholder')}
          aria-label={t('designSync.handoff')} onChange={(event) => setHandoffPath(event.target.value)}
        />
        <div style={S.contextActions}>
          <button
            style={S.linkBtn} disabled={p.busy}
            onClick={() => p.onPrepare({ mode, target, claude_design_url: designUrl, handoff_path: handoffPath })}
          ><Sparkle size={13} />{t('designSync.prepare')}</button>
        </div>

        {latest ? (
          <div style={{ borderTop: '1px solid var(--border)', paddingTop: '10px', display: 'grid', gap: '8px' }}>
            <div style={{ fontSize: '12px', color: 'var(--muted)' }}>{statusLabel[latest.status]}</div>
            <textarea readOnly style={{ ...S.contextInput, minHeight: '168px', resize: 'vertical', fontFamily: 'var(--mono, monospace)', fontSize: '11px' }} value={latest.prompt} aria-label={t('designSync.prepared_brief')} />
            <div style={S.contextActions}>
              <button style={S.linkBtn} onClick={() => void copy(latest.prompt)}><ClipboardCopy size={13} />{t('designSync.copy')}</button>
              {latest.status === 'prepared' ? <button style={S.linkBtn} disabled={!latest.claude_design_url || p.busy} onClick={() => confirmAndOpen(latest)}><ExternalLink size={13} />{t('designSync.confirm_open')}</button> : null}
              {latest.status === 'owner_send_confirmed' ? <button style={S.linkBtn} disabled={p.busy} onClick={() => p.onUpdate(latest.id, { status: 'building' })}><Send size={13} />{t('designSync.sent')}</button> : null}
              {latest.status === 'building' ? <button style={S.linkBtn} disabled={p.busy} onClick={() => p.onUpdate(latest.id, { status: 'ready_to_harvest' })}><Play size={13} />{t('designSync.bundle_ready')}</button> : null}
            </div>
            {latest.status === 'ready_to_harvest' || latest.status === 'harvested' ? (
              <>
                <textarea
                  style={{ ...S.contextInput, minHeight: '54px', resize: 'vertical' }} value={evidenceFiles}
                  placeholder={t('designSync.files_placeholder')}
                  aria-label={t('designSync.files')} onChange={(event) => setEvidenceFiles(event.target.value)}
                />
                <textarea
                  style={{ ...S.contextInput, minHeight: '54px', resize: 'vertical' }} value={evidenceNote}
                  placeholder={t('designSync.note_placeholder')}
                  aria-label={t('designSync.note')} onChange={(event) => setEvidenceNote(event.target.value)}
                />
                <button style={S.linkBtn} disabled={p.busy} onClick={() => p.onUpdate(latest.id, {
                  status: 'harvested', evidence: { files: evidenceFiles.split('\n').map((file) => file.trim()).filter(Boolean), note: evidenceNote },
                })}><PackageCheck size={13} />{t('designSync.record_bundle')}</button>
              </>
            ) : null}
          </div>
        ) : null}
      </div>
    </details>
  )
}
