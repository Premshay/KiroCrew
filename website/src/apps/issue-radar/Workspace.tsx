// Issue Radar — three-column workspace shell.
//
//   ┌────────────┬─────────────┬──────────────────────────┐
//   │  LEFT RAIL  │ ISSUE LIST  │      ISSUE DETAIL        │
//   │ (accordion: │ (filtered   │  (metadata + body)       │
//   │  Dashboards │  by rail)   │                          │
//   │  / Filters) │             │                          │
//   └────────────┴─────────────┴──────────────────────────┘
//
// In 'dashboard' main view the list+detail split is replaced by a full-width
// dashboard page (Overview / Tagging), chosen from the
// registry. 'settings' shows the Settings page in the same area. The rail stays
// visible in every mode. All shared state comes from useIssueRadar(); this file
// owns only presentational layout (column resize).
import { useState, type ReactNode } from 'react'
import {
  ChevronLeft, CircleDot, GitPullRequest, LayoutDashboard, ListFilter, Settings, X,
  type LucideIcon,
} from 'lucide-react'
import { useIssueRadar } from './context'
import { useIsMobile } from '../../hooks/useIsMobile'
import { Btn, IconButton } from '../../components/ui'
import {
  loadListWidth, LIST_WIDTH_KEY, MIN_LIST_WIDTH, MAX_LIST_WIDTH,
  loadRailWidth, loadRailCollapsed, RAIL_WIDTH_KEY, RAIL_COLLAPSED_KEY,
  MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, COLLAPSED_RAIL_WIDTH,
} from './lib/format'
import { useColumnResize, type CollapseConfig } from '../../hooks/useColumnResize'
import LeftRail from './components/LeftRail'
import ResizeHandle from '../../components/ResizeHandle'
import IssueList from './components/IssueList'
import IssueDetail from './components/IssueDetail'
import PrList from './components/PrList'
import PrDetail from './components/PrDetail'
import RepoSwitcher from './components/RepoSwitcher'
import FiltersSection from './components/FiltersSection'
import PrFiltersSection from './components/PrFiltersSection'
import SettingsView from './views/SettingsView'
import { dashboardComponent } from './views/registry'
import { providerTerms } from './lib/links'

import { i18nT } from '../../i18n/t'
// Module-level so the hook's memoised resolver isn't invalidated every render.
const RAIL_COLLAPSE: CollapseConfig = { width: COLLAPSED_RAIL_WIDTH, storageKey: RAIL_COLLAPSED_KEY }

export default function Workspace() {
  const isMobile = useIsMobile()

  return isMobile ? <MobileWorkspace /> : <DesktopWorkspace />
}

function DesktopWorkspace() {
  const { mainView, dashboardTab, activeIssue, activePull, active } = useIssueRadar()
  // Provider vocabulary: GitLab calls these merge requests, and calling them
  // pull requests in a GitLab workspace is simply wrong copy.
  const terms = providerTerms(active)
  const rail = useColumnResize(
    RAIL_WIDTH_KEY, loadRailWidth, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, RAIL_COLLAPSE, loadRailCollapsed,
  )
  const list = useColumnResize(LIST_WIDTH_KEY, loadListWidth, MIN_LIST_WIDTH, MAX_LIST_WIDTH)

  const DashboardView = dashboardComponent(dashboardTab)

  return (
    <div className="flex h-full bg-bg text-text">
      <LeftRail width={rail.width} collapsed={rail.collapsed} onExpand={rail.expand} />

      {/* Drag handle — resize the left rail. Present in every main view, since
          the rail itself is. Dragging well past the minimum collapses it. */}
      <ResizeHandle
        handleProps={rail.handleProps}
        label={i18nT('apps.issueRadar.workspace.resize_sidebar')}
        onNudge={rail.nudge}
        value={rail.width}
        min={MIN_RAIL_WIDTH}
        max={MAX_RAIL_WIDTH}
      />

      {mainView === 'issues' ? (
        <>
          <section style={{ width: list.width }} className="flex-shrink-0 min-h-0">
            <IssueList resizing={list.dragging} />
          </section>

          {/* Drag handle — resize the issue-list column. */}
          <ResizeHandle
            handleProps={list.handleProps}
            label={i18nT('apps.issueRadar.workspace.resize_list')}
            onNudge={list.nudge}
            value={list.width}
            min={MIN_LIST_WIDTH}
            max={MAX_LIST_WIDTH}
          />

          <main className="flex-1 min-w-0 min-h-0">
            {activeIssue
              ? <IssueDetail issue={activeIssue} />
              : (
                <div className="h-full flex flex-col items-center justify-center text-muted gap-2">
                  <CircleDot size={26} strokeWidth={1.5} className="opacity-50" />
                  <div className="text-[13px]">{i18nT('apps.issueRadar.workspace.select_an_issue_to_see_its_details')}</div>
                </div>
              )}
          </main>
        </>
      ) : mainView === 'settings' ? (
        <main className="flex-1 min-w-0 min-h-0">
          <SettingsView />
        </main>
      ) : mainView === 'pulls' ? (
        <>
          <section style={{ width: list.width }} className="flex-shrink-0 min-h-0">
            <PrList resizing={list.dragging} />
          </section>

          {/* Drag handle — resize the PR-list column. */}
          <ResizeHandle
            handleProps={list.handleProps}
            label={i18nT('apps.issueRadar.workspace.resize_list')}
            onNudge={list.nudge}
            value={list.width}
            min={MIN_LIST_WIDTH}
            max={MAX_LIST_WIDTH}
          />

          <main className="flex-1 min-w-0 min-h-0">
            {activePull
              ? <PrDetail pull={activePull} />
              : (
                <div className="h-full flex flex-col items-center justify-center text-muted gap-2">
                  <GitPullRequest size={26} strokeWidth={1.5} className="opacity-50" />
                  <div className="text-[13px]">{i18nT('apps.issueRadar.workspace.select_a')} {terms.changeRequestTitle} {i18nT('apps.issueRadar.workspace.to_see_its_details')}</div>
                </div>
              )}
          </main>
        </>
      ) : (
        <main className="flex-1 min-w-0 overflow-y-auto scrollbar-none" style={{ scrollbarWidth: 'none' }}>
          <DashboardView />
        </main>
      )}
    </div>
  )
}

/** Phones use one working pane so a list or detail never falls below a usable width. */
function MobileWorkspace() {
  const [filtersOpen, setFiltersOpen] = useState(false)
  const {
    mainView, dashboardTab, activeIssue, activePull, active,
    openDashboard, openIssues, openPulls, openSettings,
    setSelectedIssue, setSelectedPull,
  } = useIssueRadar()
  const terms = providerTerms(active)
  const DashboardView = dashboardComponent(dashboardTab)
  const showIssueDetail = mainView === 'issues' && activeIssue
  const showPullDetail = mainView === 'pulls' && activePull
  const filtersLabel = mainView === 'pulls'
    ? i18nT('apps.issueRadar.components.prFiltersSection.filters')
    : i18nT('apps.issueRadar.components.filtersSection.filters')

  return (
    <div className="relative flex h-full min-h-0 flex-col bg-bg text-text">
      <header className="flex-shrink-0 border-b border-border bg-bg px-3 py-2">
        <RepoSwitcher />
        <nav className="mt-2 flex items-center gap-1 overflow-x-auto scrollbar-none" aria-label={i18nT('apps.issueRadar.components.leftRail.issue_radar')} style={{ scrollbarWidth: 'none' }}>
          <MobileNavButton active={mainView === 'dashboard'} icon={LayoutDashboard} label={i18nT('apps.issueRadar.components.leftRail.dashboards')} onClick={() => openDashboard(dashboardTab)} />
          <MobileNavButton active={mainView === 'issues'} icon={CircleDot} label={i18nT('apps.issueRadar.components.leftRail.issues')} onClick={() => openIssues()} />
          <MobileNavButton active={mainView === 'pulls'} icon={GitPullRequest} label={terms.changeRequestPluralTitle} onClick={() => openPulls()} />
          <MobileNavButton active={mainView === 'settings'} icon={Settings} label={i18nT('apps.issueRadar.components.leftRail.settings')} onClick={() => openSettings()} />
          {(mainView === 'issues' || mainView === 'pulls') && !showIssueDetail && !showPullDetail && (
            <Btn
              type="button"
              className="ml-auto min-h-10 shrink-0 rounded-lg px-3 text-[13px]"
              onClick={() => setFiltersOpen(true)}
              aria-label={filtersLabel}
            >
              <ListFilter size={15} /> {filtersLabel}
            </Btn>
          )}
        </nav>
      </header>

      <div className="min-h-0 flex-1 overflow-hidden">
        {showIssueDetail ? (
          <MobileDetail onBack={() => setSelectedIssue(null)} label={i18nT('apps.issueRadar.components.leftRail.issues')}>
            <IssueDetail issue={activeIssue} />
          </MobileDetail>
        ) : showPullDetail ? (
          <MobileDetail onBack={() => setSelectedPull(null)} label={terms.changeRequestPluralTitle}>
            <PrDetail pull={activePull} />
          </MobileDetail>
        ) : mainView === 'issues' ? <IssueList />
          : mainView === 'pulls' ? <PrList />
            : mainView === 'settings' ? <SettingsView />
              : <div className="h-full overflow-y-auto scrollbar-none" style={{ scrollbarWidth: 'none' }}><DashboardView /></div>}
      </div>

      {filtersOpen && (
        <section className="absolute inset-0 z-20 flex min-h-0 flex-col bg-bg" aria-label={filtersLabel}>
          <header className="flex flex-shrink-0 items-center justify-between border-b border-border px-4 py-3">
            <span className="text-[14px] font-semibold text-text">{filtersLabel}</span>
            <IconButton
              className="min-h-10 min-w-10 rounded-lg"
              onClick={() => setFiltersOpen(false)}
              aria-label={i18nT('apps.issueRadar.components.refSheet.close')}
            >
              <X size={18} />
            </IconButton>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto pb-5">
            {mainView === 'pulls' ? <PrFiltersSection /> : <FiltersSection />}
          </div>
        </section>
      )}
    </div>
  )
}

function MobileNavButton({
  active, icon: Icon, label, onClick,
}: {
  active: boolean
  icon: LucideIcon
  label: string
  onClick: () => void
}) {
  return (
    <Btn
      type="button"
      className={`min-h-10 shrink-0 rounded-lg px-3 text-[13px] ${
        active ? 'bg-accent-subtle text-text font-medium' : 'text-muted hover:bg-bg-hover hover:text-text'
      }`}
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
    >
      <Icon size={15} /> {label}
    </Btn>
  )
}

function MobileDetail({ onBack, label, children }: { onBack: () => void; label: string; children: ReactNode }) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <Btn
        type="button"
        className="min-h-11 flex-shrink-0 justify-start border-0 px-4 text-left text-[13px]"
        onClick={onBack}
        aria-label={i18nT('apps.issueRadar.welcomeCarousel.back')}
      >
        <ChevronLeft size={17} /> {label}
      </Btn>
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  )
}
