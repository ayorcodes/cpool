export interface AgentRecord {
  agentId: string
  description: string
  subagentType: string
  model: string | null
  background: boolean
  prompt: string
  report: string | null
  done: boolean
  ts: number
}

export interface HandoffRecord {
  from: string | null
  reason: string
  agents?: Omit<AgentRecord, 'done'>[]
}

declare module 'claude-code' {
  interface PluginState {
    cpool: { leaving: boolean; compacted: 'yes' | 'no'; resumed: boolean }
  }
}
