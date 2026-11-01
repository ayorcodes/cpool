import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const HOME = '/pool'
const RUN = 'run-1'
const FILE = `${HOME}/handoff/${RUN}.json`

type World = { files: Map<string, string>; compacts: number; exits: number }

// The engine beneath the mod, in memory: a file system, a session that
// compacts (or is vetoed with `skip`), and the exit command.
function world(on: On, env: Record<string, string>, opts: { skip?: boolean; compactThrows?: boolean; writeThrows?: number; state?: object } = {}): World {
  const w: World = { files: new Map(), compacts: 0, exits: 0 }
  if (opts.state) w.files.set(`${HOME}/state.json`, JSON.stringify(opts.state))
  mock.env(on, env)
  on('fs.read', (_$, e) => {
    const text = w.files.get(e.path)
    if (text === undefined) throw new Error(`ENOENT: ${e.path}`)
    return { value: text }
  })
  let writeFailures = opts.writeThrows ?? 0
  on('fs.write', (_$, e) => {
    if (writeFailures > 0) {
      writeFailures -= 1
      throw new Error('EACCES')
    }
    w.files.set(e.path, e.text)
    return { value: undefined }
  })
  on('session.id', () => ({ value: 'sess-1' }))
  on('turn.complete', () => ({ text: '' }))
  on('session.compact', () => {
    w.compacts += 1
    if (opts.compactThrows) throw new Error('busy')
    return opts.skip ? { skip: 'off' } : { messages: [{ role: 'user', text: 'summary', toolUses: [] }], tokensBefore: 90_000, tokensAfter: 4_000 }
  })
  on('command.run', (_$, e) => {
    if (e.command === 'exit') w.exits += 1
    return {}
  })
  return w
}

const leaving = (leave?: string, manual = false) => ({
  sessions: { 'sess-1': { account: 'main', model: 'claude-opus-5-5', effort: 'high', ...(leave ? { leave } : {}), ...(manual ? { leave_manual: true } : {}) } },
})
const ENV = { CPOOL_RUN: RUN, CPOOL_HOME: HOME, CPOOL_ACCOUNT: 'main' }
const DONE = { turnId: 't1', answer: '', durationMs: 1, isAborted: false }

async function finishTurn($: any, clock: { advance: (ms: number) => Promise<void> }) {
  await $.turn.complete(DONE)
  await clock.advance(600)
}

test('does nothing without CPOOL_RUN', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, { CPOOL_HOME: HOME }, { state: leaving('soft') })
  await finishTurn($, clock)
  expect(w.compacts).toBe(0)
  expect(w.exits).toBe(0)
  expect(w.files.has(FILE)).toBe(false)
})

test('does nothing while the statusline has not asked the session to leave', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { state: leaving() })
  await finishTurn($, clock)
  expect(w.compacts).toBe(0)
  expect(w.exits).toBe(0)
  expect(w.files.has(FILE)).toBe(false)
})

test('on leave: compacts, writes the handoff file the wrapper reads, exits, once', async ($, on) => {
  const clock = mock.clock(on, { now: 1_762_000_200_000 })
  const w = world(on, ENV, { state: leaving('soft') })
  await finishTurn($, clock)
  expect(JSON.parse(w.files.get(FILE) ?? '{}')).toEqual({
    version: 1, run: RUN, session_id: 'sess-1', from: 'main', model: 'claude-opus-5-5', effort: 'high',
    reason: 'soft', compacted: true, ts: 1_762_000_200,
  })
  expect(w.compacts).toBe(1)
  expect(w.exits).toBe(1)
  await finishTurn($, clock)
  expect(w.compacts).toBe(1)
  expect(w.exits).toBe(1)
})

test('a vetoed compaction still hands off, marked uncompacted', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { skip: true, state: leaving('projected') })
  await finishTurn($, clock)
  const h = JSON.parse(w.files.get(FILE) ?? '{}')
  expect(h.compacted).toBe(false)
  expect(h.reason).toBe('projected')
  expect(w.exits).toBe(1)
})

test('a subagent turn does not trigger the handoff', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { state: leaving('soft') })
  await $.turn.complete({ ...DONE, agentId: 'agent-1' })
  await clock.advance(600)
  expect(w.compacts).toBe(0)
  expect(w.exits).toBe(0)
  expect(w.files.has(FILE)).toBe(false)
})

test('a throwing compaction still hands off, marked uncompacted, and exits', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { compactThrows: true, state: leaving('soft') })
  await finishTurn($, clock)
  expect(JSON.parse(w.files.get(FILE) ?? '{}').compacted).toBe(false)
  expect(w.exits).toBe(1)
})

test('without CPOOL_HOME the handoff goes to $HOME/.cpool/handoff', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, { CPOOL_RUN: RUN, HOME: '/h' })
  w.files.set('/h/.cpool/state.json', JSON.stringify(leaving('soft')))
  await finishTurn($, clock)
  expect(w.files.has(`/h/.cpool/handoff/${RUN}.json`)).toBe(true)
  expect(w.exits).toBe(1)
})

test('a failed handoff write resets the guard so a later turn retries', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { writeThrows: 1, state: leaving('soft') })
  await finishTurn($, clock)
  expect(w.files.has(FILE)).toBe(false)
  expect(w.exits).toBe(0)
  await finishTurn($, clock)
  expect(w.files.has(FILE)).toBe(true)
  expect(w.exits).toBe(1)
})

test('a compaction error is logged', async ($, on) => {
  const clock = mock.clock(on)
  const logs: string[] = []
  on('ui.log', (_$, e: any) => { logs.push(String(e.message ?? e.text ?? JSON.stringify(e))); return { value: undefined } })
  world(on, ENV, { compactThrows: true, state: leaving('soft') })
  await finishTurn($, clock)
  expect(logs.some(l => l.startsWith('cpool: compact failed:'))).toBe(true)
})

test('a retry after a failed write does not compact again', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { writeThrows: 1, state: leaving('soft') })
  await finishTurn($, clock)
  await finishTurn($, clock)
  expect(w.compacts).toBe(1)
  expect(JSON.parse(w.files.get(FILE) ?? '{}').compacted).toBe(true)
})

test('a manual switch (leave_manual) hands off with reason manual', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { state: leaving(undefined, true) })
  await finishTurn($, clock)
  expect(JSON.parse(w.files.get(FILE) ?? '{}').reason).toBe('manual')
  expect(w.exits).toBe(1)
})
