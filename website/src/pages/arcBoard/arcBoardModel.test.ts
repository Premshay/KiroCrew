import { describe, expect, it } from 'vitest'
import type { ChatSlot } from '../../types'
import type { ArcBoardEntry, ArcBoardLane } from '../../api/client/arcBoard'
import { feedView, laneKey, laneViews, newEntries, pickerSessions, sessionGroup, splitLink, suggestedRole } from './arcBoardModel'

const slot = (over: Partial<ChatSlot>): ChatSlot => ({ key: 'chat-1-1', messages: 0, running: false, ...over }) as ChatSlot
const entry = (id: string, kind: string, author = 'chat-a'): ArcBoardEntry => ({ id, ts: '2026-11-01T10:00:00+00:00', author, kind, text: id })

describe('splitLink', () => {
  it('makes only a leading http(s) URL clickable', () => {
    expect(splitLink('https://github.com/o/r/pull/1 — both joiners')).toEqual({
      href: 'https://github.com/o/r/pull/1',
      text: 'both joiners',
    })
    expect(splitLink('Branch tenon/m1: see notes')).toEqual({ href: null, text: 'Branch tenon/m1: see notes' })
  })
  it.each(['javascript:alert(1)', ' JavaScript:x', 'data:text/html,x', 'vbscript:x', '//evil.example'])(
    'never turns %s into an href',
    link => {
      expect(splitLink(link).href).toBeNull()
    },
  )
})

describe('session groups', () => {
  it('folds dashboard prefixes to the ledger spelling', () => {
    expect(laneKey('dashboard:chat-9-1')).toBe('chat-9-1')
    expect(laneKey('dashboard_chat-9-1')).toBe('chat-9-1')
    expect(laneKey('chat-9-1')).toBe('chat-9-1')
  })
  it('groups mates, kits and other sessions', () => {
    expect(sessionGroup({ agent: 'baton', agent_kind: 'member' })).toBe('mates')
    expect(sessionGroup({ agent: 'kit-import', agent_kind: 'template' })).toBe('kits')
    expect(sessionGroup({ agent: 'crew-claude', agent_kind: '' })).toBe('other')
  })
  it('suggests a role from the mate or kit', () => {
    expect(suggestedRole({ key: 'k', agent: 'atelier', agent_kind: 'member', title: 'timetable walk' })).toBe('Atelier')
    expect(suggestedRole({ key: 'k', agent: 'kit-import', agent_kind: 'template' })).toBe('Import')
    expect(suggestedRole({ key: 'k', agent: 'crew-claude', title: 'Fork sync' })).toBe('Fork sync')
  })
  it('lists every session except current members, sorted per group', () => {
    const groups = pickerSessions(
      [
        slot({ key: 'dashboard:chat-2', agent: 'tenon', agent_kind: 'member', title: 'Tenon' }),
        slot({ key: 'chat-1', agent: 'baton', agent_kind: 'member', title: 'Baton' }),
        slot({ key: 'chat-3', agent: 'kit-evaluation', title: 'Gate' }),
        slot({ key: 'chat-4', agent: 'crew-claude', title: 'Scratch' }),
      ],
      ['dashboard_chat-2'],
    )
    expect(groups.mates.map(s => s.name)).toEqual(['Baton'])
    expect(groups.kits.map(s => s.key)).toEqual(['chat-3'])
    expect(groups.other.map(s => s.key)).toEqual(['chat-4'])
  })
})

describe('laneViews', () => {
  const lane: ArcBoardLane = { key: 'chat-1', role: 'Tenon', joined_at: '', entries: 0, last_ts: null, last_kind: null, claim: null }
  it('reads state and nudge from the live session and loops', () => {
    const [v] = laneViews([lane], [slot({ key: 'dashboard:chat-1', running: true, agent: 'tenon' })], ['dashboard:chat-1'])
    expect(v.state).toBe('working')
    expect(v.nudge).toBe(true)
    expect(v.agent).toBe('tenon')
  })
  it('marks a lane gone only once the session list has loaded', () => {
    expect(laneViews([lane], [], [])[0].state).toBe('gone')
    expect(laneViews([lane], undefined, [])[0].state).toBe('idle')
  })
})

describe('feedView', () => {
  const entries = [entry('a', 'CLAIM'), entry('b', 'FACT', 'chat-b'), entry('c', 'FAIL'), entry('d', 'ASK')]
  const all = new Set(['CLAIM', 'FACT', 'FAIL', 'ASK', 'DONE'])
  it('is newest first and LIVE shows everything', () => {
    expect(feedView(entries, { kinds: all, lane: null, cursor: null }).map(e => e.id)).toEqual(['d', 'c', 'b', 'a'])
  })
  it('replays up to the cursor', () => {
    expect(feedView(entries, { kinds: all, lane: null, cursor: 2 }).map(e => e.id)).toEqual(['b', 'a'])
    expect(feedView(entries, { kinds: all, lane: null, cursor: 0 })).toEqual([])
  })
  it('filters by kind and lane', () => {
    expect(feedView(entries, { kinds: new Set(['FAIL', 'FACT']), lane: null, cursor: null }).map(e => e.id)).toEqual(['c', 'b'])
    expect(feedView(entries, { kinds: all, lane: 'chat-b', cursor: null }).map(e => e.id)).toEqual(['b'])
  })
})

describe('newEntries', () => {
  it('animates nothing on the first read, then only what is new', () => {
    const e = [entry('a', 'FACT'), entry('b', 'FACT')]
    expect(newEntries(null, e)).toEqual([])
    expect(newEntries(new Set(['a']), e).map(x => x.id)).toEqual(['b'])
  })
})
