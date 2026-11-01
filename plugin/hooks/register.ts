// cpool's side of a session handoff (docs/design/2026-11-01-cpool-contract.md).
//
// Inert unless `cpool run` set CPOOL_RUN. At a turn boundary, when the
// statusline has stored `sessions.<sid>.leave` in <home>/state.json, the
// session compacts itself, writes <home>/handoff/<CPOOL_RUN>.json for the
// wrapper and exits. The handoff file is the only signal: the mod cannot set
// an exit code. It never submits a prompt and touches nothing else in home.

import type { EngineInterface, Register } from 'claude-code'

const LEAVING = { plugin: 'cpool', key: 'leaving' } as const
const COMPACTED = { plugin: 'cpool', key: 'compacted' } as const // 'yes' | 'no' once this process has compacted
const DEFER_MS = 500 // compact rejects mid-turn; let the turn settle first

type Session = { leave?: unknown; leave_manual?: unknown; model?: unknown }

async function homeOf($: EngineInterface): Promise<string> {
  const home = await $.env.get('CPOOL_HOME')
  if (home) return home
  return `${(await $.env.get('HOME')) ?? ''}/.cpool`
}

async function leaveOf($: EngineInterface, home: string, sid: string): Promise<Session | null> {
  try {
    const state = JSON.parse(String(await $.fs.read(`${home}/state.json`)))
    const sess: Session | undefined = state?.sessions?.[sid]
    if (!sess) return null
    if (typeof sess.leave === 'string' && sess.leave !== '') return sess
    return sess.leave_manual === true ? { ...sess, leave: 'manual' } : null
  } catch {
    return null
  }
}

function report($: EngineInterface, what: string, err: unknown): void {
  $.ui.log(`cpool: ${what}: ${err instanceof Error ? err.message : String(err)}`)
}

async function handoff($: EngineInterface, home: string, run: string, sid: string, sess: Session): Promise<void> {
  try {
    // A retry after a failed write must not compact a second time.
    const { value: prior } = await $.state.get(COMPACTED)
    let compacted = prior === 'yes'
    if (prior !== 'yes' && prior !== 'no') {
      try {
        const res = await $.session.compact()
        compacted = !res.skip
      } catch (err) {
        compacted = false
        report($, 'compact failed', err)
      }
      await $.state.set(COMPACTED, compacted ? 'yes' : 'no')
    }
    const record = {
      version: 1,
      run,
      session_id: sid,
      from: (await $.env.get('CPOOL_ACCOUNT')) ?? null,
      model: typeof sess.model === 'string' ? sess.model : null,
      reason: sess.leave,
      compacted,
      ts: Math.floor((await $.clock.now()) / 1000),
    }
    // $.fs has no rename; a single write is all the API allows.
    await $.fs.write(`${home}/handoff/${run}.json`, JSON.stringify(record))
  } catch (err) {
    // No handoff file: nothing tells the wrapper, so let a later turn retry.
    await $.state.set(LEAVING, false)
    report($, 'handoff not written, will retry', err)
    return
  }
  // The file is written, so it is the signal; the guard stays set even if exit fails.
  try {
    await $.command.run({ command: 'exit', args: '' })
  } catch (err) {
    report($, 'handoff written but exit failed', err)
  }
}

export const register: Register = on => {
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    // Subagent turns (agentId set) fire this hook too; only the main loop may leave.
    if (e.isAborted || e.agentId) return result
    const run = await $.env.get('CPOOL_RUN')
    if (!run) return result
    const { value: leaving } = await $.state.get(LEAVING)
    if (leaving) return result
    const sid = await $.session.id()
    const home = await homeOf($)
    const sess = await leaveOf($, home, sid)
    if (!sess) return result
    await $.state.set(LEAVING, true)
    $.clock.after(DEFER_MS, () => {
      void handoff($, home, run, sid, sess).catch(err => report($, 'handoff failed', err))
    })
    return result
  })
}
