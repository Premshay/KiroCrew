import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import SetupPanel from '../apps/auto-improvement/SetupPanel'
import { createTestStore } from './helpers'

vi.mock('../components/SimpleSelect', () => ({
  default: ({ options, optionLabels, value, onChange, disabled, 'aria-label': label }: {
    options: string[]; optionLabels?: string[]; value: string; onChange: (value: string) => void
    disabled?: boolean; 'aria-label'?: string
  }) => <select aria-label={label} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
    {options.map((option, i) => <option key={option} value={option}>{optionLabels?.[i] ?? option}</option>)}
  </select>,
}))

const config = {
  target_url: 'https://github.com/example/a', clone: '/clones/a', branch: 'main',
  testEnvironment: { kind: 'python', pythonExecutable: '/env/a/bin/python' },
}
const response = (body: unknown, status = 200) => ({ ok: status < 400, status, json: async () => body }) as Response
let status: string
let publication: string | undefined
let writeResponse: Response
let checkResponse: () => Promise<Response>
let writes: Array<{ path: string; body: Record<string, unknown> }>
let checkRecord: Record<string, unknown>
let survey: Record<string, unknown>

/** The server's check: the POST keeps a record of the outcome that GET returns. */
async function recordCheck(body: Record<string, unknown>): Promise<Response> {
  const reply = await checkResponse()
  if (reply.status === 409) return reply
  const result = await reply.json()
  const ok = reply.ok && result.ok !== false
  checkRecord = {
    state: ok ? 'ready' : 'blocked',
    fingerprint: body.fingerprint,
    result: ok ? result : { ok: false, diagnostic: result.diagnostic ?? result.error },
  }
  return response(checkRecord, 202)
}

function mount(initial: Record<string, unknown> | undefined = config) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const store = createTestStore()
  const tree = (value: Record<string, unknown> | undefined) => (
    <Provider store={store}><MemoryRouter><QueryClientProvider client={client}>
      <SetupPanel config={value} />
    </QueryClientProvider></MemoryRouter></Provider>
  )
  const result = render(tree(initial))
  return { ...result, client, setConfig: (value: Record<string, unknown> | undefined) => result.rerender(tree(value)) }
}
const executable = () => screen.getByLabelText('Python executable (absolute path)')
const save = () => screen.getByRole('button', { name: 'Save environment' })
const check = () => screen.getByRole('button', { name: 'Check environment' })
async function readyToEdit() { await waitFor(() => expect(check()).not.toBeDisabled()) }

beforeEach(() => {
  status = 'idle'
  publication = undefined
  writeResponse = response({ ok: true })
  checkResponse = async () => response({ ok: true, tests_collected: 4 })
  writes = []
  checkRecord = { state: 'none' }
  survey = { revision: 'abc123', evidence: ['pytest.ini'], warnings: [], workloads: [], environment: null }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (init?.method === 'POST' || init?.method === 'PUT') {
      const body = JSON.parse(String(init.body))
      writes.push({ path, body })
      return path.endsWith('/environment/check') ? recordCheck(body) : writeResponse
    }
    if (path.endsWith('/environment/check')) return response(checkRecord)
    if (path.endsWith('/repository/survey')) return response(survey)
    if (path.endsWith('/branches')) return response({ branches: ['main', 'feature'] })
    if (path.endsWith('/run')) return response({ status, stats: { publication }, ...(status === 'error' ? { error: 'Previous run failed' } : {}) })
    return response(config)
  }))
})
afterEach(() => vi.unstubAllGlobals())

describe('test environment setup', () => {
  it('stages a suggested bug scope without executing anything or replacing the environment', async () => {
    survey.workloads = [{ id: 'bugs', label: 'Import bugs', description: 'Check imports', source: 'declaration', config: { track: 'bug', editAllowlist: ['imports/**'], focusedTestPaths: ['tests/imports'] } }]
    survey.environment = { kind: 'runner', runnerPath: 'scripts/tests.sh', variables: { IMAGE: 'suggested' } }
    const view = mount({ ...config, track: 'perf', benchmarkCommand: 'old benchmark', benchmarkCanaryCommand: 'old control', benchmarkProtectedPaths: ['old'], maxHours: 3 })
    await readyToEdit()
    const apply = await screen.findByRole('button', { name: 'Use workload' })
    expect(writes).toEqual([])
    fireEvent.click(apply)
    expect(screen.getByLabelText(/Files the agent may edit/)).toHaveValue('imports/**')
    expect(screen.getByLabelText(/Benchmark command/)).toHaveValue('')
    expect(screen.getByLabelText(/Slowed control command/)).toHaveValue('')
    expect(screen.getByLabelText(/Protected benchmark files/)).toHaveValue('')
    expect(executable()).toHaveValue('/env/a/bin/python')
    expect(screen.queryByRole('button', { name: 'Calibrate' })).not.toBeInTheDocument()
    expect(writes).toEqual([])
    expect(apply).toBeDisabled()
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0]).toMatchObject({ path: '/api/apps/auto-improvement/config', body: { track: 'bug', editAllowlist: ['imports/**'], focusedTestPaths: ['tests/imports'], benchmarkCommand: '', scopeDiffBase: '', maxHours: 3, measureReps: null, testEnvironment: { kind: 'python', pythonExecutable: '/env/a/bin/python' } } })
    const saved = { ...config, ...writes[0].body }
    view.unmount()
    mount(saved)
    await readyToEdit()
    expect(screen.getByLabelText(/Files the agent may edit/)).toHaveValue('imports/**')
    expect(screen.getByLabelText(/Focused pytest paths/)).toHaveValue('tests/imports')
  })

  it('applies a performance workload with its own protected benchmark and clears old diff scope', async () => {
    survey.workloads = [{ id: 'perf', label: 'Import throughput', description: 'Measure fixed imports', source: 'declaration', config: { track: 'perf', benchmarkCommand: 'python bench.py', benchmarkCanaryCommand: 'python bench.py --slow', benchmarkProtectedPaths: ['bench.py'], benchmarkResultMode: 'structured', editAllowlist: ['imports/**'] } }]
    mount({ ...config, scopeDiffBase: 'HEAD~20', benchmarkCommand: 'stale' })
    await readyToEdit()
    fireEvent.click(await screen.findByRole('button', { name: 'Use workload' }))
    expect(screen.getByLabelText(/Review changes since/)).toHaveValue('')
    expect(screen.getByLabelText(/Benchmark command/)).toHaveValue('python bench.py')
    expect(screen.getByLabelText(/Protected benchmark files/)).toHaveValue('bench.py')
    expect(screen.getByRole('button', { name: 'Calibrate' })).toBeDisabled()
    expect(writes).toEqual([])
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body).toMatchObject({ track: 'perf', benchmarkCommand: 'python bench.py', benchmarkResultMode: 'structured', scopeDiffBase: '' })
  })

  it('requires an explicit action to stage a suggested environment', async () => {
    survey.environment = { kind: 'runner', runnerPath: 'scripts/tests.sh', variables: { IMAGE: 'suggested' } }
    mount()
    await readyToEdit()
    fireEvent.click(await screen.findByText('Suggested test environment'))
    expect(executable()).toHaveValue('/env/a/bin/python')
    fireEvent.click(screen.getByRole('button', { name: 'Use environment' }))
    expect(screen.getByLabelText(/Nonsecret variables/)).toHaveValue('IMAGE=suggested')
    expect(writes).toEqual([])
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body.testEnvironment).toMatchObject({ kind: 'runner', runnerPath: 'scripts/tests.sh', variables: { IMAGE: 'suggested' } })
  })

  it('preserves the current runner pin and host variables when applying suggested defaults', async () => {
    survey.environment = { kind: 'runner', runnerPath: 'scripts/tests.sh', variables: { ATLAS_TEST_TIMEOUT_SECONDS: '2400', ATLAS_TEST_IMAGE: 'untrusted-suggestion' } }
    mount({ ...config, testEnvironment: { kind: 'runner', runnerPath: 'scripts/tests.sh', runnerCommit: 'a'.repeat(40), pythonExecutable: 'python3', variables: { ATLAS_TEST_IMAGE: 'sha256:approved' } } })
    await readyToEdit()
    fireEvent.click(await screen.findByText('Suggested test environment'))
    fireEvent.click(screen.getByRole('button', { name: 'Use environment' }))
    expect(screen.getByLabelText(/Nonsecret variables/)).toHaveValue('ATLAS_TEST_TIMEOUT_SECONDS=2400\nATLAS_TEST_IMAGE=sha256:approved')
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body.testEnvironment).toMatchObject({ runnerCommit: 'a'.repeat(40), pythonExecutable: 'python3', variables: { ATLAS_TEST_IMAGE: 'sha256:approved', ATLAS_TEST_TIMEOUT_SECONDS: '2400' } })
  })

  it('refreshes repository evidence without executing or applying a workload', async () => {
    mount()
    await readyToEdit()
    await screen.findByText(/No workload found/)
    survey = { ...survey, workloads: [{ id: 'new', label: 'Newly discovered area', description: 'Latest local revision', source: 'detected', config: { track: 'bug' } }] }
    fireEvent.click(screen.getByRole('button', { name: 'Refresh suggestions' }))
    expect(await screen.findByText('Latest local revision')).toBeInTheDocument()
    expect(writes).toEqual([])
  })

  it('protects unsaved edits from late survey data and refuses applying a preset during a run', async () => {
    const view = mount()
    await readyToEdit()
    fireEvent.change(screen.getByLabelText(/Files the agent may edit/), { target: { value: 'my-area/**' } })
    survey = { ...survey, workloads: [{ id: 'bugs', label: 'Other area', description: '', source: 'detected', config: { editAllowlist: ['other/**'] } }] }
    await act(async () => { await view.client.invalidateQueries({ queryKey: ['auto-improvement-repository-survey'] }) })
    expect(screen.getByLabelText(/Files the agent may edit/)).toHaveValue('my-area/**')
    expect(await screen.findByRole('button', { name: 'Use workload' })).toBeDisabled()
    view.unmount()
    status = 'running'
    mount()
    expect(await screen.findByRole('button', { name: 'Use workload' })).toBeDisabled()
    expect(writes).toEqual([])
  })

  it('saves numeric budgets and timeouts and rejects invalid counts without a request', async () => {
    mount()
    await readyToEdit()
    fireEvent.click(screen.getByText('Advanced settings'))
    fireEvent.change(screen.getByLabelText('Maximum cycles'), { target: { value: '1.5' } })
    fireEvent.click(save())
    expect(await screen.findByText(/Maximum cycles must be between/)).toBeInTheDocument()
    expect(writes).toEqual([])
    fireEvent.change(screen.getByLabelText('Maximum cycles'), { target: { value: '7' } })
    fireEvent.change(screen.getByLabelText('Candidate check timeout (seconds)'), { target: { value: '2400' } })
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body).toMatchObject({ maxCycles: 7, suiteTimeoutSeconds: 2400, measureReps: null })
  })

  it('shows measured check duration without claiming collection predicts suite execution', async () => {
    checkResponse = async () => response({ ok: true, tests_collected: 4, duration_seconds: 42, timings: { interpreter: 2, collection: 40 } })
    mount()
    await readyToEdit()
    fireEvent.click(check())
    expect(await screen.findByText(/Last check:.*Collection time is not suite execution time/)).toBeInTheDocument()
  })
  it.each([
    ['running', 'pending', 'Checking full regression before publication'],
    ['done', 'published', 'Published'],
  ])('shows publication outcome for %s', async (runStatus, outcome, label) => {
    status = runStatus
    publication = outcome
    mount()
    expect(await screen.findByText(label)).toBeInTheDocument()
  })

  it('persists trusted benchmark paths as repository-relative arrays', async () => {
    mount({ ...config, benchmarkProtectedPaths: ['controls'] })
    await readyToEdit()
    const paths = screen.getByLabelText(/Protected benchmark files/)
    expect(paths).toHaveValue('controls')
    fireEvent.change(paths, { target: { value: 'controls\nbench/helpers.py' } })
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body.benchmarkProtectedPaths).toEqual(['controls', 'bench/helpers.py'])
  })

  it('checks and saves gateway variables without dropping them', async () => {
    mount({ ...config, testEnvironment: { kind: 'gateway', variables: { MODE: 'test' } } })
    await readyToEdit()
    expect(screen.getByLabelText(/Nonsecret variables/)).toHaveValue('MODE=test')
    fireEvent.click(check())
    await screen.findByText('Ready')
    expect(writes[0].body.testEnvironment).toEqual({ kind: 'gateway', variables: { MODE: 'test' } })
    fireEvent.change(screen.getByLabelText(/Nonsecret variables/), { target: { value: 'MODE=other' } })
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect(writes[1].body.testEnvironment).toEqual({ kind: 'gateway', variables: { MODE: 'other' } })
  })

  it('loads async config and preserves an unsaved draft across config refresh and branch change', async () => {
    const view = mount(undefined)
    view.setConfig(config)
    await readyToEdit()
    fireEvent.change(executable(), { target: { value: '/draft/python' } })
    expect(writes).toHaveLength(0)
    view.setConfig({ ...config, branch: 'feature', testEnvironment: { kind: 'python', pythonExecutable: '/server/python' } })
    expect(executable()).toHaveValue('/draft/python')
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body.testEnvironment).toEqual({ kind: 'python', pythonExecutable: '/draft/python', variables: {} })
  })

  it('does not copy repository A draft or readiness into repository B', async () => {
    const view = mount()
    await readyToEdit()
    fireEvent.change(executable(), { target: { value: '/draft/a' } })
    fireEvent.click(check())
    await screen.findByText('Ready')
    view.setConfig({ ...config, target_url: 'https://github.com/example/b', clone: '/clones/b', testEnvironment: { kind: 'gateway' } })
    expect(screen.getByLabelText('Test environment')).toHaveValue('gateway')
    expect(screen.getByText('Unchecked')).toBeInTheDocument()
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
    expect(save()).toBeDisabled()
  })

  it('checks a draft without saving or starting a run and applies the result only to that draft', async () => {
    mount()
    await readyToEdit()
    fireEvent.click(check())
    await screen.findByText('Ready')
    expect(writes.map((w) => w.path)).toEqual(['/api/apps/auto-improvement/environment/check'])
    fireEvent.change(executable(), { target: { value: '/other/python' } })
    expect(screen.getByText('Unchecked')).toBeInTheDocument()
    fireEvent.change(executable(), { target: { value: '/env/a/bin/python' } })
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })

  it('hides readiness and measured results after changing the workload, restoring them when undone', async () => {
    checkResponse = async () => response({ ok: true, tests_collected: 4, duration_seconds: 42 })
    mount({ ...config, track: 'perf', focusedTestPaths: ['tests/imports'] })
    await readyToEdit()
    fireEvent.click(check())
    await screen.findByText('Ready')
    expect(screen.getByText('4 tests collected')).toBeInTheDocument()
    expect(screen.getByText(/Last check:/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Calibrate' })).toBeEnabled()
    fireEvent.change(screen.getByLabelText(/Focused pytest paths/), { target: { value: 'tests/other' } })
    expect(screen.getByText('Unchecked')).toBeInTheDocument()
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
    expect(screen.queryByText('4 tests collected')).not.toBeInTheDocument()
    expect(screen.queryByText(/Last check:/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Calibrate' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Focused pytest paths/), { target: { value: 'tests/imports' } })
    expect(screen.getByText('Ready')).toBeInTheDocument()
    expect(screen.getByText('4 tests collected')).toBeInTheDocument()
    expect(screen.getByText(/Last check:/)).toBeInTheDocument()
  })

  it('shows the last check after the page is opened again', async () => {
    const first = mount()
    await readyToEdit()
    fireEvent.click(check())
    await screen.findByText('Ready')
    expect(screen.getByText('4 tests collected')).toBeInTheDocument()
    first.unmount()
    mount()
    expect(await screen.findByText('Ready')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Calibrate/ })).not.toBeInTheDocument()
  })

  it('keeps the form busy while the server is still checking and says so', async () => {
    const first = mount()
    await readyToEdit()
    fireEvent.click(check())
    await screen.findByText('Ready')
    first.unmount()
    checkRecord = { ...checkRecord, state: 'checking' }
    mount()
    expect(await screen.findByText(/keeps running if you leave this page/)).toBeInTheDocument()
    expect(screen.getByText('Checking…')).toBeInTheDocument()
    expect(check()).toBeDisabled()
    expect(check().querySelector('.animate-spin')).not.toBeNull()
  })

  it('reports a check cut off by a restart', async () => {
    const first = mount()
    await readyToEdit()
    fireEvent.click(check())
    await screen.findByText('Ready')
    first.unmount()
    checkRecord = { ...checkRecord, state: 'interrupted' }
    mount()
    expect(await screen.findByText(/cut off before it finished/)).toBeInTheDocument()
  })

  it('applies a late check result only to the branch it checked', async () => {
    let resolve!: (result: Response) => void
    checkResponse = () => new Promise((done) => { resolve = done })
    const view = mount()
    await readyToEdit()
    fireEvent.click(check())
    await screen.findByText('Checking…')
    view.setConfig({ ...config, branch: 'feature' })
    await act(async () => resolve(response({ ok: true })))
    expect(await screen.findByText('Unchecked')).toBeInTheDocument()
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
    view.setConfig(config)
    expect(await screen.findByText('Ready')).toBeInTheDocument()
  })

  it('keeps the draft and shows backend check and save failures', async () => {
    mount()
    await readyToEdit()
    fireEvent.change(executable(), { target: { value: '/draft/python' } })
    checkResponse = async () => response({ diagnostic: 'Interpreter unavailable', ok: false }, 422)
    fireEvent.click(check())
    await screen.findByText('Interpreter unavailable')
    expect(screen.getByRole('alert')).toHaveTextContent('Interpreter unavailable')
    expect(screen.getByText('Blocked')).toBeInTheDocument()
    expect(executable()).toHaveValue('/draft/python')
    writeResponse = response({ error: 'A run owns this repository' }, 409)
    fireEvent.click(save())
    await screen.findByText('A run owns this repository')
    expect(screen.getByRole('alert')).toHaveTextContent('A run owns this repository')
    expect(executable()).toHaveValue('/draft/python')
    expect(save()).not.toBeDisabled()
  })

  it.each(['running', 'calibrating', 'stopping'])('locks environment mutations while %s', async (active) => {
    status = active
    mount()
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    expect(executable()).toBeDisabled()
    expect(check()).toBeDisabled()
    expect(save()).toBeDisabled()
  })

  it('saves runner defaults and explicitly entered nonsecret variables', async () => {
    mount()
    await readyToEdit()
    fireEvent.change(screen.getByLabelText('Test environment'), { target: { value: 'runner' } })
    fireEvent.change(screen.getByLabelText(/Runner executable/), { target: { value: '/tools/repo-check' } })
    fireEvent.change(screen.getByLabelText(/Nonsecret variables/), { target: { value: 'MODE=test\nURL=http://db:5432?a=b' } })
    expect(screen.getByRole('button', { name: /start|run/i })).toBeDisabled()
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body.testEnvironment).toEqual({ kind: 'runner', pythonExecutable: 'python', runnerExecutable: '/tools/repo-check', variables: { MODE: 'test', URL: 'http://db:5432?a=b' } })
  })

  it('checks the runner draft, explains its responsibility, and retains backend metadata', async () => {
    const metadata = { version: 1, services: ['database'] }
    mount({ ...config, testEnvironment: { kind: 'runner', runnerExecutable: '/tools/check', metadata } })
    await readyToEdit()
    expect(screen.getByRole('option', { name: 'Repository runner' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /container/i })).not.toBeInTheDocument()
    expect(screen.getByText(/must provide the target repository’s test environment/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(/Python executable/), { target: { value: 'python3' } })
    fireEvent.click(check())
    await screen.findByText('Ready')
    expect(writes).toEqual([{ path: '/api/apps/auto-improvement/environment/check', body: {
      testEnvironment: { kind: 'runner', runnerExecutable: '/tools/check', pythonExecutable: 'python3', variables: {}, metadata },
      fingerprint: expect.any(String),
    } }])
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect(writes[1].body.testEnvironment).toEqual(writes[0].body.testEnvironment)
  })

  it.each([
    { runnerExecutable: '' },
    { runnerExecutable: 'tools/check' },
    { runnerPath: '../check' },
    { runnerPath: 'tools//check' },
  ])('rejects runner location %j without making a request', async (location) => {
    mount({ ...config, testEnvironment: { kind: 'runner', ...location } })
    await readyToEdit()
    fireEvent.click(check())
    const hint = await screen.findByText(
      "Enter the runner's path in the repository, or an absolute path to an installed runner.",
    )
    expect(hint).toHaveAttribute('role', 'status')
    expect(hint).toHaveAttribute('aria-live', 'polite')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(writes).toHaveLength(0)
  })

  it('saves a repository runner path for the app to pin', async () => {
    mount()
    await readyToEdit()
    fireEvent.change(screen.getByLabelText('Test environment'), { target: { value: 'runner' } })
    fireEvent.change(screen.getByLabelText(/Runner executable/), { target: { value: 'scripts/dev/run_full_suite.sh' } })
    expect(screen.getByText('Saving pins the runner to the current commit of main.')).toBeInTheDocument()
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body.testEnvironment).toEqual({ kind: 'runner', pythonExecutable: 'python', runnerPath: 'scripts/dev/run_full_suite.sh', variables: {} })
  })

  it('shows the pinned runner commit and unpins it to update', async () => {
    const commit = 'a'.repeat(40)
    mount({ ...config, testEnvironment: { kind: 'runner', runnerPath: 'scripts/run.sh', runnerCommit: commit } })
    await readyToEdit()
    expect(screen.getByText(/Pinned to commit aaaaaaaaaaaa\./)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Update runner to main' }))
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body.testEnvironment).toEqual({ kind: 'runner', pythonExecutable: 'python', runnerPath: 'scripts/run.sh', variables: {} })
  })

  it('drops the pin when the runner path changes', async () => {
    mount({ ...config, testEnvironment: { kind: 'runner', runnerPath: 'scripts/run.sh', runnerCommit: 'b'.repeat(40) } })
    await readyToEdit()
    fireEvent.change(screen.getByLabelText(/Runner executable/), { target: { value: 'scripts/other.sh' } })
    expect(screen.queryByRole('button', { name: /Update runner/ })).not.toBeInTheDocument()
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0].body.testEnvironment).toEqual({ kind: 'runner', pythonExecutable: 'python', runnerPath: 'scripts/other.sh', variables: {} })
  })

  it('retains runner edits through refresh and locks the form while checking', async () => {
    let resolve!: (result: Response) => void
    checkResponse = () => new Promise((done) => { resolve = done })
    const saved = { ...config, testEnvironment: { kind: 'runner', runnerExecutable: '/tools/check' } }
    const view = mount(saved)
    await readyToEdit()
    fireEvent.change(screen.getByLabelText(/Runner executable/), { target: { value: '/tools/draft' } })
    view.setConfig({ ...saved, branch: 'feature' })
    expect(screen.getByLabelText(/Runner executable/)).toHaveValue('/tools/draft')
    fireEvent.click(check())
    await screen.findByText('Checking…')
    expect(check().querySelector('.animate-spin')).not.toBeNull()
    expect(save().querySelector('.animate-spin')).toBeNull()
    expect(screen.getByLabelText(/Runner executable/)).toBeDisabled()
    expect(screen.getByLabelText(/Python executable/)).toBeDisabled()
    expect(screen.getByLabelText('Test environment')).toBeDisabled()
    expect(save()).toBeDisabled()
    expect(check()).toBeDisabled()
    expect(screen.getByRole('button', { name: /start|run/i })).toBeDisabled()
    await act(async () => resolve(response({ ok: false, diagnostic: { stderr: 'Database setup failed' } }, 422)))
    const diagnostic = await screen.findByText('Database setup failed')
    expect(diagnostic).toHaveClass('max-h-40', 'overflow-auto', 'select-text')
    expect(screen.getByLabelText(/Runner executable/)).toHaveValue('/tools/draft')
    expect(save()).not.toBeDisabled()
    expect(check().querySelector('.animate-spin')).toBeNull()
  })

  it.each(['running', 'calibrating', 'stopping'])('locks the runner while %s', async (active) => {
    status = active
    mount({ ...config, testEnvironment: { kind: 'runner', runnerExecutable: '/tools/check' } })
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    expect(screen.getByLabelText(/Runner executable/)).toBeDisabled()
    expect(screen.getByLabelText('Test environment')).toBeDisabled()
    expect(check()).toBeDisabled()
    expect(save()).toBeDisabled()
    expect(writes).toHaveLength(0)
  })

  it.each(['', 'python'])('shows hints for Python path %j and duplicate variables without an alert or request', async (path) => {
    mount()
    await readyToEdit()
    fireEvent.change(executable(), { target: { value: path } })
    fireEvent.click(check())
    const hint = await screen.findByText('Enter an absolute path to the Python executable.')
    expect(hint).toHaveAttribute('role', 'status')
    expect(hint).toHaveAttribute('aria-live', 'polite')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    fireEvent.change(executable(), { target: { value: '/env/python' } })
    fireEvent.change(screen.getByLabelText(/Nonsecret variables/), { target: { value: 'MODE=a\nMODE=b' } })
    expect(hint).toBeEmptyDOMElement()
    fireEvent.click(save())
    const variablesHint = await screen.findByText(/Use one NAME=value per line/)
    expect(variablesHint).toHaveAttribute('role', 'status')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(writes).toHaveLength(0)
  })

  it('shows a rejected start and leaves the run query uninvalidated', async () => {
    writeResponse = response({ error: 'No tests collected' }, 422)
    const view = mount()
    await readyToEdit()
    const invalidate = vi.spyOn(view.client, 'invalidateQueries')
    const start = screen.getByRole('button', { name: /start|run/i })
    fireEvent.click(start)
    await screen.findByText('No tests collected')
    expect(invalidate).not.toHaveBeenCalled()
  })

  it('keeps a failed branch save on the persisted branch', async () => {
    writeResponse = response({ error: 'Branch is unavailable' }, 400)
    mount()
    await readyToEdit()
    fireEvent.change(screen.getByLabelText('Base branch'), { target: { value: 'feature' } })
    await screen.findByText('Branch is unavailable')
    expect(screen.getByLabelText('Base branch')).toHaveValue('main')
  })

  it('blocks a 200 response with ok false, retaining the draft', async () => {
    mount()
    await readyToEdit()
    fireEvent.change(executable(), { target: { value: '/draft/python' } })
    checkResponse = async () => response({ ok: false, diagnostic: 'Collection failed' })
    fireEvent.click(check())
    await screen.findByText('Collection failed')
    expect(screen.getByText('Blocked')).toBeInTheDocument()
    expect(executable()).toHaveValue('/draft/python')
  })

  it('invalidates readiness when a run begins and does not revive it after stopping', async () => {
    const view = mount()
    await readyToEdit()
    fireEvent.click(check())
    await screen.findByText('Ready')
    checkRecord = { state: 'none' } // the server clears it when the run starts
    act(() => { view.client.setQueryData(['auto-improvement-run'], { status: 'running' }) })
    await waitFor(() => expect(check()).toBeDisabled())
    act(() => { view.client.setQueryData(['auto-improvement-run'], { status: 'idle' }) })
    await readyToEdit()
    expect(screen.getByText('Unchecked')).toBeInTheDocument()
  })

  it('adopts refreshed persisted settings when there is no draft', async () => {
    const view = mount()
    await readyToEdit()
    view.setConfig({ ...config, testEnvironment: { kind: 'python', pythonExecutable: '/new/python' } })
    expect(executable()).toHaveValue('/new/python')
    expect(save()).toBeDisabled()
  })


  it('renders structured backend diagnostics instead of an object string', async () => {
    mount()
    await readyToEdit()
    checkResponse = async () => response({ ok: false, diagnostic: { stage: 'pytest', stderr: 'No module named pytest', stdout: '' } }, 400)
    fireEvent.click(check())
    await screen.findByText('No module named pytest')
    expect(screen.queryByText('[object Object]')).not.toBeInTheDocument()
  })

  it('allows fixing setup after a previous run failed', async () => {
    status = 'error'
    mount()
    await readyToEdit()
    expect(executable()).not.toBeDisabled()
  })

})


it('saves the selected performance track and structured benchmark mode', async () => {
  mount()
  await readyToEdit()
  fireEvent.change(screen.getByLabelText('Track'), { target: { value: 'perf' } })
  fireEvent.change(screen.getByLabelText('Benchmark result'), { target: { value: 'structured' } })
  fireEvent.change(screen.getByLabelText('Benchmark command (optional)'), { target: { value: 'python bench.py' } })
  fireEvent.change(screen.getByLabelText('Slowed control command'), { target: { value: 'python bench.py --slow' } })
  fireEvent.click(save())
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0].body).toMatchObject({ track: 'perf', benchmarkResultMode: 'structured', benchmarkCommand: 'python bench.py', benchmarkCanaryCommand: 'python bench.py --slow' })
})
