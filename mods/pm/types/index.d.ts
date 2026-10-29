// The pm mod's state contract: what it keeps in `$.state` under `pm`.

export type Row = {
  id: string
  agent: string
  model: string | null
  prompt: string
  status: string
  startedAt: string | null
  finishedAt: string | null
  batchId: string | null
  cwd: string | null
  durationSeconds: number | null
  failReason: string | null
}

export type QuotaWindow = {
  name: string
  pct: number | null
  reset: string | null
}

export type Quota = {
  lane: string
  status: string
  tier: string | null
  windows: QuotaWindow[]
  // When the probe failed and the windows are the last good ones playmaker
  // kept: when those were measured. Null for fresh windows.
  staleAt: string | null
}

export type Quotas = { fetchedAt: string; lanes: Quota[] }

// What this session's own Bash calls started: the batches, the review
// scripts' round prefixes and the worker ids. Only these wake it.
export type Claims = { batches: string[]; prefixes: string[]; sessions: string[] }

// A session this one started (`start_session`), by its desktop id.
export type Child = { id: string; title: string }

// What each session leaves for the one that started it.
export type ClaimsFile = { claims: Claims; children: Child[] }

// Whose workers the pane shows: this session (label null) and its children.
export type Owner = { label: string | null; claims: Claims }

// One line per worker out of `playmaker summary`, by session id.
export type Note = { status: string; text: string; at: number }

export type Review = {
  wp: string
  pid: number
  log: string
  startedAt: number
  isDone: boolean
  exitCode: string | null
  tail: string[]
}

declare module 'claude-code' {
  interface PluginState {
    pm: {
      rows: Shaped<Row[]>
      quotas: Shaped<Quotas | null>
      policy: string | null
      review: Review | null
      error: string | null
      notes: Shaped<Record<string, Note>>
      mine: Shaped<Claims>
      children: Shaped<Child[]>
      owners: Shaped<Owner[]>
    }
  }
}
