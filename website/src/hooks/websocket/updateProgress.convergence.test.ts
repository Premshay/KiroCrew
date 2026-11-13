import { describe, expect, it } from 'vitest'
import { handleUpdateProgress } from './bundleReload'
import { store } from '../../store'
import { setUpdateProgress, sseStatus } from '../../store/dashboardSlice'
import type { StatusData } from '../../types'
import type { UpdateProgress } from '../../types'

const maintenance = {
  active: true,
  ready: false,
  required: ['dashboard:owner', 'dashboard:peer'],
  pending: ['dashboard:peer'],
  unmanaged_busy: ['channel:worker'],
}
const blocked: UpdateProgress = { step: 'blocked', detail: 'Restart blocked', maintenance }
const status: StatusData = {
  uptime: '1m', sessions: 2, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
  update_progress: blocked,
}

describe('update progress maintenance', () => {
  it('preserves the barrier in live frames and status snapshots, then clears it', () => {
    store.dispatch(setUpdateProgress(null))
    handleUpdateProgress(store.dispatch, blocked)
    expect(store.getState().dashboard.updateProgress).toEqual(blocked)

    store.dispatch(setUpdateProgress({ step: 'building', detail: 'Building' }))
    store.dispatch(sseStatus(status))
    expect(store.getState().dashboard.updateProgress).toEqual(blocked)

    store.dispatch(sseStatus({ ...status, update_progress: undefined }))
    expect(store.getState().dashboard.updateProgress).toEqual(blocked)
    store.dispatch(sseStatus({ ...status, update_progress: null }))
    expect(store.getState().dashboard.updateProgress).toBeNull()
  })
})
