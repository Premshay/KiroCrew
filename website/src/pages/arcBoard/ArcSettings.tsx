/**
 * The arc's two owner settings: the GitHub repository whose merged PRs tagged
 * "Arc: <id>" the board records, and the coordinator (one of the lanes) who
 * may sign a milestone off for the owner after the owner confirmed it with
 * them. Both are owner-only writes.
 */

import { useState } from 'react'
import { Btn, Card, CardTitle } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import { i18nT } from '../../i18n/t'
import type { ArcBoardArc } from '../../api/client/arcBoard'
import { C } from './theme'

export default function ArcSettings({
  arc,
  onSave,
  pending,
  error,
}: {
  arc: ArcBoardArc
  onSave: (settings: { repo: string; coordinator: string }) => void
  pending: boolean
  error: string | null
}) {
  const [repo, setRepo] = useState(arc.repo ?? '')
  const [coordinator, setCoordinator] = useState(arc.coordinator ?? '')
  const keys = Object.keys(arc.members)
  const changed = repo.trim() !== (arc.repo ?? '') || coordinator !== (arc.coordinator ?? '')

  return (
    <Card className="p-3">
      <CardTitle>{i18nT('pages.arcBoard.settings')}</CardTitle>
      <div className="mb-1 text-[11px]" style={{ color: C.dim }}>
        {i18nT('pages.arcBoard.settings_repo')}
      </div>
      <input
        value={repo}
        onChange={e => setRepo(e.target.value)}
        aria-label={i18nT('pages.arcBoard.settings_repo')}
        placeholder="owner/name"
        className="w-full rounded-md border bg-transparent px-2 py-1 font-mono text-[12px]"
        style={{ borderColor: C.border, color: C.text }}
      />
      <div className="mt-1 text-[11px]" style={{ color: C.dim }}>
        {i18nT('pages.arcBoard.settings_repo_hint', { tag: `Arc: ${arc.id}` })}
      </div>
      <div className="mb-1 mt-3 text-[11px]" style={{ color: C.dim }}>
        {i18nT('pages.arcBoard.settings_coordinator')}
      </div>
      <SimpleSelect
        options={['', ...keys]}
        optionLabels={[i18nT('pages.arcBoard.settings_no_coordinator'), ...keys.map(k => arc.members[k].role)]}
        value={coordinator}
        onChange={setCoordinator}
        aria-label={i18nT('pages.arcBoard.settings_coordinator')}
      />
      <div className="mt-1 text-[11px]" style={{ color: C.dim }}>
        {i18nT('pages.arcBoard.settings_coordinator_hint')}
      </div>
      <Btn
        type="button"
        primary
        disabled={!changed || pending}
        onClick={() => onSave({ repo: repo.trim(), coordinator })}
        className="mt-2 px-2 py-1 text-[11px]"
      >
        {i18nT('pages.arcBoard.settings_save')}
      </Btn>
      {error ? (
        <div className="mt-1 text-[11px]" style={{ color: C.warn }}>
          {error}
        </div>
      ) : null}
    </Card>
  )
}
