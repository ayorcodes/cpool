// cpool's side of a session handoff (docs/design/2026-11-01-cpool-contract.md).
// Inert unless CPOOL_RUN is set. Tracks agents in agents/<run>.json, defers
// soft handoffs for active agents, and submits one continuation prompt on resume.
// The handoff file signals the wrapper to terminate this process without exit's dialog.

import type { AgentInfo, EngineInterface, Register } from 'claude-code'
import type { AgentRecord, HandoffRecord } from '../types'

const LEAVING = { plugin: 'cpool', key: 'leaving' } as const
const COMPACTED = { plugin: 'cpool', key: 'compacted' } as const
const RESUMED = { plugin: 'cpool', key: 'resumed' } as const
const DEFER_MS = 500 // compact rejects mid-turn; let the turn settle first
const ACTIVE = new Set(['pending', 'running', 'waiting'])
const agents = new Map<string, AgentRecord>()
let deferred = 0
let writes = Promise.resolve()
let loaded: Promise<void> | undefined

function loadAgents($: EngineInterface, home: string, run: string): Promise<void> {
  loaded ??= (async () => {
    try {
      const saved: { agents: Record<string, AgentRecord>; deferred: number } = JSON.parse(String(await $.fs.read(`${home}/agents/${run}.json`)))
      for (const [id, record] of Object.entries(saved.agents)) {
        if (!agents.has(id)) agents.set(id, record)
      }
      deferred = saved.deferred
    } catch {
      // Missing or unreadable tracking files start with an empty map.
    }
  })()
  return loaded
}

type Session = { leave?: unknown; leave_manual?: unknown; model?: unknown; effort?: unknown; hard: boolean }

async function homeOf($: EngineInterface): Promise<string> {
  const home = await $.env.get('CPOOL_HOME')
  if (home) return home
  return `${(await $.env.get('HOME')) ?? ''}/.cpool`
}

function report($: EngineInterface, what: string, err: unknown): void {
  $.ui.log(`cpool: ${what}: ${err instanceof Error ? err.message : String(err)}`)
}

function writeAgents($: EngineInterface, home: string, run: string): Promise<void> {
  const text = JSON.stringify({ version: 1, agents: Object.fromEntries(agents), deferred })
  writes = writes.then(() => $.fs.write(`${home}/agents/${run}.json`, text)).then(() => undefined).catch(err => report($, 'agents not written', err))
  return writes
}

async function leaveOf($: EngineInterface, home: string, sid: string): Promise<Session | null> {
  try {
    const state = JSON.parse(String(await $.fs.read(`${home}/state.json`)))
    const sess = state?.sessions?.[sid]
    if (!sess) return null
    const account = await $.env.get('CPOOL_ACCOUNT')
    const usage = state.accounts?.[account ?? '']?.five_hour
    const hard = Number(await $.env.get('CPOOL_HARD')) || 97
    const now = (await $.clock.now()) / 1000
    const atHard = typeof usage?.used === 'number' && (usage.resets_at === null || usage.resets_at > now) && usage.used >= hard
    if (typeof sess.leave === 'string' && sess.leave !== '') return { ...sess, hard: atHard }
    return sess.leave_manual === true ? { ...sess, leave: 'manual', hard: atHard } : null
  } catch {
    return null
  }
}

async function handoff($: EngineInterface, home: string, run: string, sid: string, sess: Session, rows: AgentInfo[]): Promise<void> {
  try {
    // A retry after a failed write must not compact a second time.
    const { value: prior } = await $.state.get(COMPACTED)
    let compacted = prior === 'yes'
    if (prior !== 'yes' && prior !== 'no') {
      try {
        const res = await $.session.compact()
        compacted = !res.skip
      } catch (err) {
        report($, 'compact failed', err)
      }
      await $.state.set(COMPACTED, compacted ? 'yes' : 'no')
    }
    const live = new Map(rows.map(r => [r.id, r]))
    const record = {
      version: 1,
      run,
      session_id: sid,
      from: (await $.env.get('CPOOL_ACCOUNT')) ?? null,
      model: typeof sess.model === 'string' ? sess.model : null,
      effort: typeof sess.effort === 'string' ? sess.effort : null,
      reason: sess.leave,
      compacted,
      ts: Math.floor((await $.clock.now()) / 1000),
      agents: [...agents.values()].filter(r => {
        const row = live.get(r.agentId)
        return row ? ACTIVE.has(row.status) : !r.done
      }).map(({ done, ...r }) => r),
    }
    // The engine has no rename; a single write is all its filesystem API allows.
    await $.fs.write(`${home}/handoff/${run}.json`, JSON.stringify(record))
  } catch (err) {
    await $.state.set(LEAVING, false)
    report($, 'handoff not written, will retry', err)
  }
}

export const register: Register = on => {
  on('agent.spawn', async ($, e, next) => {
    const res = await next(e)
    try {
      const run = await $.env.get('CPOOL_RUN')
      if (!run || !('agentId' in res) || !res.agentId) return res
      const home = await homeOf($)
      await loadAgents($, home, run)
      if (!agents.has(res.agentId)) {
        agents.set(res.agentId, {
          agentId: res.agentId,
          description: e.description,
          subagentType: e.subagentType,
          model: res.model ?? e.model ?? null,
          background: e.background,
          prompt: e.prompt,
          report: /^Report:\s*(\S+)/m.exec(e.prompt)?.[1] ?? null,
          done: false,
          ts: Math.floor((await $.clock.now()) / 1000),
        })
        await writeAgents($, home, run)
      }
    } catch (err) { report($, 'agent tracking failed', err) }
    return res
  })

  on('session.start', async ($, e, next) => {
    const res = await next(e)
    const run = await $.env.get('CPOOL_RUN')
    const path = await $.env.get('CPOOL_RESUMED')
    if (!run || !path || (await $.state.get(RESUMED)).value) return res
    await $.state.set(RESUMED, true)
    let resumed: HandoffRecord = { from: 'unknown', reason: 'unknown', agents: [] }
    try { resumed = JSON.parse(String(await $.fs.read(path))) }
    catch (err) { report($, 'resume handoff unreadable', err) }
    const records = resumed.agents ?? []
    const lines = [`cpool moved this session from ${resumed.from} to ${await $.env.get('CPOOL_ACCOUNT')} (${resumed.reason}).`]
    if (records.length) {
      lines.push('These subagents were stopped before they finished:')
      for (const r of records) lines.push(`- ${r.description} — type ${r.subagentType}, model ${r.model}, report ${r.report ?? 'none'}\n  Brief:\n${r.prompt.split('\n').map(line => `    ${line}`).join('\n')}`)
      lines.push('For each one, read its report if it exists and is complete; otherwise spawn it again with the same brief, type and model.')
    }
    if (resumed.reason === 'hard') lines.push('The last turn hit the usage limit before it finished; redo it.')
    lines.push(records.length ? 'Then continue the task where the summary left off.' : 'Continue the task where the summary left off.')
    $.clock.after(1500, () => { void $.prompt.submit({ text: lines.join('\n') }).catch(err => report($, 'resume submit failed', err)) })
    return res
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    const run = await $.env.get('CPOOL_RUN')
    if (!run) return result
    const home = await homeOf($)
    await loadAgents($, home, run)
    // Subagent turns (agentId set) fire this hook too; only the main loop may leave.
    if (e.agentId) {
      const agent = agents.get(e.agentId)
      if (agent) {
        try {
          const row = (await $.agent.list()).find(r => r.id === e.agentId)
          if (row && (row.status === 'completed' || row.status === 'failed' || row.status === 'killed')) {
            agent.done = true
            await writeAgents($, home, run)
          }
        } catch (err) { report($, 'agent list failed', err) }
      }
      return result
    }
    if (e.isAborted || (await $.state.get(LEAVING)).value) return result
    const sid = await $.session.id()
    const sess = await leaveOf($, home, sid)
    if (!sess) return result
    let rows: AgentInfo[] = []
    try { rows = await $.agent.list() }
    catch (err) { report($, 'agent list failed', err) }
    const active = rows.filter(r => !r.parentId && ACTIVE.has(r.status))
    const forced = sess.leave === 'projected' || sess.leave === 'model' || sess.hard
    const count = forced ? 0 : active.length
    if (deferred !== count) { deferred = count; await writeAgents($, home, run) }
    if (count) return result
    await $.state.set(LEAVING, true)
    $.clock.after(DEFER_MS, () => { void handoff($, home, run, sid, sess, rows).catch(err => report($, 'handoff failed', err)) })
    return result
  })
}
