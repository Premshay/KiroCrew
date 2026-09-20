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
let writeResponse: Response
let checkResponse: () => Promise<Response>
let writes: Array<{ path: string; body: Record<string, unknown> }>

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
  writeResponse = response({ ok: true })
  checkResponse = async () => response({ ok: true, tests_collected: 4 })
  writes = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (init?.method === 'POST' || init?.method === 'PUT') {
      writes.push({ path, body: JSON.parse(String(init.body)) })
      return path.endsWith('/environment/check') ? checkResponse() : writeResponse
    }
    if (path.endsWith('/branches')) return response({ branches: ['main', 'feature'] })
    if (path.endsWith('/run')) return response({ status, ...(status === 'error' ? { error: 'Previous run failed' } : {}) })
    return response(config)
  }))
})
afterEach(() => vi.unstubAllGlobals())

describe('test environment setup', () => {
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
    expect(writes[0].body).toEqual({ testEnvironment: { kind: 'python', pythonExecutable: '/draft/python', variables: {} } })
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

  it('checks a draft without saving or starting a run and invalidates after edits, including reverting', async () => {
    mount()
    await readyToEdit()
    fireEvent.click(check())
    await screen.findByText('Ready')
    expect(writes.map((w) => w.path)).toEqual(['/api/apps/auto-improvement/environment/check'])
    fireEvent.change(executable(), { target: { value: '/other/python' } })
    fireEvent.change(executable(), { target: { value: '/env/a/bin/python' } })
    expect(screen.getByText('Unchecked')).toBeInTheDocument()
  })

  it('ignores a late check result after branch changes and does not revive it on switching back', async () => {
    let resolve!: (result: Response) => void
    checkResponse = () => new Promise((done) => { resolve = done })
    const view = mount()
    await readyToEdit()
    fireEvent.click(check())
    await screen.findByText('Checking…')
    view.setConfig({ ...config, branch: 'feature' })
    await act(async () => resolve(response({ ok: true })))
    view.setConfig(config)
    expect(screen.getByText('Unchecked')).toBeInTheDocument()
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
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
    fireEvent.change(screen.getByLabelText('Runner executable (absolute path)'), { target: { value: '/tools/repo-check' } })
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
    } }])
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect(writes[1].body).toEqual(writes[0].body)
  })

  it.each(['', 'tools/check'])('rejects runner path %j without making a request', async (path) => {
    mount({ ...config, testEnvironment: { kind: 'runner', runnerExecutable: path } })
    await readyToEdit()
    fireEvent.click(check())
    const hint = await screen.findByText('Enter an absolute path to the runner executable.')
    expect(hint).toHaveAttribute('role', 'status')
    expect(hint).toHaveAttribute('aria-live', 'polite')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(writes).toHaveLength(0)
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
