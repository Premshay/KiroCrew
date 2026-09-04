import { Save, Trash2 } from 'lucide-react'

import SimpleSelect from '../../components/SimpleSelect'
import { S } from './styles'
import type { ProjectContext, ReviewBrief, ReviewIntent } from './types'
import { i18nT } from '../../i18n/t'

interface Props {
  contexts: ProjectContext[]
  brief: ReviewBrief
  busy: boolean
  onChange: (patch: Partial<ReviewBrief>) => void
  onSelectContext: (contextId: string) => void
  onSaveContext: () => void
  onDeleteContext: () => void
}

export default function ScopeBuilder(p: Props) {
  const { brief } = p
  return (
    <details style={S.contextBuilder}>
      <summary style={S.contextSummary}>{i18nT('apps.designCritique.scopeBuilder.project_context_and_review_scope')}</summary>
      <div style={S.contextGrid}>
        <SimpleSelect
          style={S.contextInput}
          value={brief.contextId}
          disabled={p.busy}
          aria-label={i18nT('apps.designCritique.scopeBuilder.saved_project_context')}
          clearLabel={i18nT('apps.designCritique.scopeBuilder.no_saved_project_context')}
          options={p.contexts.map((context) => context.id)}
          optionLabels={p.contexts.map((context) => context.name)}
          onChange={p.onSelectContext}
        />
        <input
          style={S.contextInput}
          value={brief.projectName}
          disabled={p.busy}
          placeholder={i18nT('apps.designCritique.scopeBuilder.project_name')}
          aria-label={i18nT('apps.designCritique.scopeBuilder.project_name')}
          onChange={(event) => p.onChange({ projectName: event.target.value })}
        />
        <input
          style={S.contextInput}
          value={brief.repository}
          disabled={p.busy}
          placeholder={i18nT('apps.designCritique.scopeBuilder.repository_or_local_path')}
          aria-label={i18nT('apps.designCritique.scopeBuilder.repository_or_local_path')}
          onChange={(event) => p.onChange({ repository: event.target.value })}
        />
        <SimpleSelect
          style={S.contextInput}
          value={brief.intent}
          disabled={p.busy}
          aria-label={i18nT('apps.designCritique.scopeBuilder.review_intent')}
          options={['ground', 'reference', 'invent']}
          optionLabels={[
            i18nT('apps.designCritique.scopeBuilder.ground_existing_work'),
            i18nT('apps.designCritique.scopeBuilder.reference_existing_system'),
            i18nT('apps.designCritique.scopeBuilder.explore_new_direction'),
          ]}
          onChange={(value) => p.onChange({ intent: value as ReviewIntent })}
        />
        <textarea
          style={{ ...S.contextInput, minHeight: '62px', resize: 'vertical' }}
          value={brief.contextPaths}
          disabled={p.busy}
          placeholder={i18nT('apps.designCritique.scopeBuilder.supporting_files_one_path_per_line')}
          aria-label={i18nT('apps.designCritique.scopeBuilder.supporting_files_one_path_per_line')}
          onChange={(event) => p.onChange({ contextPaths: event.target.value })}
        />
        <textarea
          style={{ ...S.contextInput, minHeight: '62px', resize: 'vertical' }}
          value={brief.notes}
          disabled={p.busy}
          placeholder={i18nT('apps.designCritique.scopeBuilder.constraints_known_decisions_or_design_system_context')}
          aria-label={i18nT('apps.designCritique.scopeBuilder.constraints_known_decisions_or_design_system_context')}
          onChange={(event) => p.onChange({ notes: event.target.value })}
        />
        <textarea
          style={{ ...S.contextInput, minHeight: '62px', resize: 'vertical', gridColumn: '1 / -1' }}
          value={brief.targets}
          disabled={p.busy}
          placeholder={i18nT('apps.designCritique.scopeBuilder.review_target_placeholder')}
          aria-label={i18nT('apps.designCritique.scopeBuilder.review_target')}
          onChange={(event) => p.onChange({ targets: event.target.value })}
        />
      </div>
      <div style={S.contextActions}>
        <button style={S.linkBtn} disabled={p.busy || !brief.projectName.trim()} onClick={p.onSaveContext}>
          <Save size={13} />{i18nT('apps.designCritique.scopeBuilder.save_project_context')}
        </button>
        {brief.contextId ? (
          <button style={S.linkBtn} disabled={p.busy} onClick={p.onDeleteContext}>
            <Trash2 size={13} />{i18nT('apps.designCritique.scopeBuilder.delete_project_context')}
          </button>
        ) : null}
      </div>
    </details>
  )
}
