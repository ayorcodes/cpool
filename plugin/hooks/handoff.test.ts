import type { AgentInfo, AgentSpawnInput, On } from 'claude-code'
import type { Engine } from 'claude-code/testing'
import { expect, mock, test } from 'claude-code/testing'

const HOME = '/pool'
const RUN = 'run-1'
const FILE = `${HOME}/handoff/${RUN}.json`

type World = { files: Map<string, string>; compacts: number; exits: number; rows: AgentInfo[] }

// The engine beneath the mod, in memory: a file system, a session that
// compacts (or is vetoed with `skip`), and the exit command.
function world(on: On, env: Record<string, string>, opts: { skip?: boolean; compactThrows?: boolean; writeThrows?: number; state?: object } = {}): World {
  const w: World = { files: new Map(), compacts: 0, exits: 0, rows: [] }
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
  on('agent.list', () => ({ value: w.rows }))
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
const DONE = { turnId: 't1', answer: '', durationMs: 1, isAborted: false, reason: 'answer' as const }

async function finishTurn($: Engine, clock: { advance: (ms: number) => Promise<void> }) {
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

test('on leave: compacts, writes the handoff file without exiting, once', async ($, on) => {
  const clock = mock.clock(on, { now: 1_762_000_200_000 })
  const w = world(on, ENV, { state: leaving('soft') })
  await finishTurn($, clock)
  expect(JSON.parse(w.files.get(FILE) ?? '{}')).toEqual({
    version: 1, run: RUN, session_id: 'sess-1', from: 'main', model: 'claude-opus-5-5', effort: 'high',
    reason: 'soft', compacted: true, ts: 1_762_000_200, agents: [],
  })
  expect(w.compacts).toBe(1)
  expect(w.exits).toBe(0)
  await finishTurn($, clock)
  expect(w.compacts).toBe(1)
  expect(w.exits).toBe(0)
})

test('a vetoed compaction still hands off, marked uncompacted', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { skip: true, state: leaving('projected') })
  await finishTurn($, clock)
  const h = JSON.parse(w.files.get(FILE) ?? '{}')
  expect(h.compacted).toBe(false)
  expect(h.reason).toBe('projected')
  expect(w.exits).toBe(0)
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

test('a throwing compaction still hands off, marked uncompacted, without exiting', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { compactThrows: true, state: leaving('soft') })
  await finishTurn($, clock)
  expect(JSON.parse(w.files.get(FILE) ?? '{}').compacted).toBe(false)
  expect(w.exits).toBe(0)
})

test('without CPOOL_HOME the handoff goes to $HOME/.cpool/handoff', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, { CPOOL_RUN: RUN, HOME: '/h' })
  w.files.set('/h/.cpool/state.json', JSON.stringify(leaving('soft')))
  await finishTurn($, clock)
  expect(w.files.has(`/h/.cpool/handoff/${RUN}.json`)).toBe(true)
  expect(w.exits).toBe(0)
})

test('a failed handoff write resets the guard so a later turn retries', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { writeThrows: 1, state: leaving('soft') })
  await finishTurn($, clock)
  expect(w.files.has(FILE)).toBe(false)
  expect(w.exits).toBe(0)
  await finishTurn($, clock)
  expect(w.files.has(FILE)).toBe(true)
  expect(w.exits).toBe(0)
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
  expect(w.exits).toBe(0)
})

const AGENTS = `${HOME}/agents/${RUN}.json`
const row = (id: string, status: AgentInfo['status'] = 'running', parentId?: string): AgentInfo => ({ id, status, description: id, type: 'general-purpose', ...(parentId ? { parentId } : {}) })

test('soft leave defers for top-level agents until completion', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { state: leaving('soft') })
  w.rows = [row('a1')]
  await finishTurn($, clock)
  expect(w.compacts).toBe(0)
  expect(w.files.has(FILE)).toBe(false)
  expect(JSON.parse(w.files.get(AGENTS) ?? '{}').deferred).toBe(1)
  w.rows = [row('a1', 'completed')]
  await finishTurn($, clock)
  expect(w.files.has(FILE)).toBe(true)
  expect(JSON.parse(w.files.get(AGENTS) ?? '{}').deferred).toBe(0)
})

for (const scenario of ['hard', 'projected', 'nested']) {
  test(`${scenario} does not defer handoff`, async ($, on) => {
    const clock = mock.clock(on, { now: 1_000_000 })
    const state = { ...leaving(scenario === 'projected' ? 'projected' : 'soft'), accounts: { main: { five_hour: { used: 96, resets_at: null } } } }
    const w = world(on, { ...ENV, CPOOL_HARD: '95' }, { state: scenario === 'hard' ? state : leaving(scenario === 'projected' ? 'projected' : 'soft') })
    w.rows = [row('a1', 'running', scenario === 'nested' ? 'parent' : undefined)]
    await finishTurn($, clock)
    expect(w.compacts).toBe(1)
    expect(w.files.has(FILE)).toBe(true)
    expect(w.exits).toBe(0)
  })
}

test('spawn preserves the first brief and report; forced handoff excludes done and terminal agents', async ($, on) => {
  const clock = mock.clock(on, { now: 1_762_000_200_000 })
  const w = world(on, ENV, { state: leaving('projected') })
  const spawn: AgentSpawnInput = { tool_use_id: 'tool-1', prompt: '', description: '', subagentType: 'general-purpose', provider: { plugin: 'engine', tier: 'core' }, parentModel: 'sonnet', background: true, fork: false }
  let id = 'a1'
  on('agent.spawn', () => ({ agentId: id, model: 'sonnet' }))
  await $.agent.spawn({ ...spawn, prompt: 'Report: reports/first.md\nDo the work', description: 'first', subagentType: 'general-purpose', background: true })
  await $.agent.spawn({ ...spawn, prompt: 'notification' })
  expect(JSON.parse(w.files.get(AGENTS) ?? '{}').agents.a1).toEqual({ agentId: 'a1', description: 'first', subagentType: 'general-purpose', model: 'sonnet', background: true, prompt: 'Report: reports/first.md\nDo the work', report: 'reports/first.md', done: false, ts: 1_762_000_200 })
  w.rows = [row('a1', 'completed')]
  await $.turn.complete({ ...DONE, agentId: 'a1' })
  expect(JSON.parse(w.files.get(AGENTS) ?? '{}').agents.a1.done).toBe(true)
  id = 'a2'
  await $.agent.spawn({ ...spawn, prompt: 'second brief', description: 'second' })
  id = 'a3'
  await $.agent.spawn({ ...spawn, prompt: 'third brief' })
  w.rows = [row('a2'), row('a3', 'failed')]
  await finishTurn($, clock)
  const stopped = JSON.parse(w.files.get(FILE) ?? '{}').agents
  expect(stopped.length).toBe(1)
  expect(stopped[0].agentId).toBe('a2')
  expect(stopped[0].done).toBe(undefined)
})

test('a waiting subagent turn remains unfinished and is included in a forced handoff', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, ENV, { state: leaving('projected') })
  on('agent.spawn', () => ({ agentId: 'a1', model: 'sonnet' }))
  await $.agent.spawn({ tool_use_id: 'tool-1', prompt: 'Keep working', description: 'worker', subagentType: 'general-purpose', provider: { plugin: 'engine', tier: 'core' }, parentModel: 'sonnet', background: true, fork: false })
  w.rows = [row('a1', 'waiting')]
  await $.turn.complete({ ...DONE, agentId: 'a1' })
  expect(JSON.parse(w.files.get(AGENTS) ?? '{}').agents.a1.done).toBe(false)
  await finishTurn($, clock)
  expect(JSON.parse(w.files.get(FILE) ?? '{}').agents.map((agent: { agentId: string }) => agent.agentId)).toEqual(['a1'])
})

test('hot reload preserves earlier agents when spawning before a forced handoff', async ($, on) => {
  const clock = mock.clock(on, { now: 1_762_000_200_000 })
  const w = world(on, ENV, { state: leaving('projected') })
  w.files.set(AGENTS, JSON.stringify({ version: 1, deferred: 0, agents: {
    a1: { agentId: 'a1', description: 'first', subagentType: 'general-purpose', model: 'sonnet', background: true, prompt: 'first brief', report: null, done: false, ts: 1_762_000_100 },
  } }))
  on('agent.spawn', () => ({ agentId: 'a2', model: 'sonnet' }))
  await $.agent.spawn({ tool_use_id: 'tool-2', prompt: 'second brief', description: 'second', subagentType: 'general-purpose', provider: { plugin: 'engine', tier: 'core' }, parentModel: 'sonnet', background: true, fork: false })
  await finishTurn($, clock)
  expect(JSON.parse(w.files.get(FILE) ?? '{}').agents).toEqual([
    { agentId: 'a1', description: 'first', subagentType: 'general-purpose', model: 'sonnet', background: true, prompt: 'first brief', report: null, ts: 1_762_000_100 },
    { agentId: 'a2', description: 'second', subagentType: 'general-purpose', model: 'sonnet', background: true, prompt: 'second brief', report: null, ts: 1_762_000_200 },
  ])
})

for (const reason of ['soft', 'hard']) {
  test(`resume ${reason} submits the stopped brief once after 1.5 seconds`, async ($, on) => {
    const clock = mock.clock(on)
    const path = `${HOME}/handoff/done/previous.json`
    const w = world(on, { ...ENV, CPOOL_RESUMED: path })
    w.files.set(path, JSON.stringify({ from: 'previous', reason, agents: [{ description: 'repair parser', subagentType: 'general-purpose', model: 'sonnet', report: 'reports/parser.md', prompt: 'Fix parsing\nKeep data safe' }] }))
    const submits: string[] = []
    on('session.start', (_$, e) => ({ cwd: e.cwd }))
    on('prompt.submit', (_$, e) => { submits.push(e.text); return { text: e.text } })
    await $.session.start({ cwd: '/', surface: null, isInteractive: true })
    await $.session.start({ cwd: '/', surface: null, isInteractive: true })
    await clock.advance(1499)
    expect(submits.length).toBe(0)
    await clock.advance(1)
    expect(submits.length).toBe(1)
    expect(submits.join('').includes('repair parser')).toBe(true)
    expect(submits.join('').includes('reports/parser.md')).toBe(true)
    expect(submits.join('').includes('    Fix parsing\n    Keep data safe')).toBe(true)
    expect(submits.join('').includes('redo it.')).toBe(reason === 'hard')
  })
}

test('session start without CPOOL_RESUMED never submits', async ($, on) => {
  const clock = mock.clock(on)
  world(on, ENV)
  const submits: string[] = []
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('prompt.submit', (_$, e) => { submits.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: null, isInteractive: true })
  await clock.advance(2000)
  expect(submits.length).toBe(0)
})
