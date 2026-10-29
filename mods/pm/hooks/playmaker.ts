// Pure helpers: parsing playmaker's JSON output and formatting it for the
// pane, the status line and the wake-up brief. No `$` in here, so every
// function is testable by itself and the hooks module stays readable.

import type { Child, ClaimsFile, Owner, Quota, QuotaWindow, Quotas, Row } from '../types'

export type Event = {
  id: number
  ts: string
  session_id: string | null
  agent: string | null
  model: string | null
  batch_id: string | null
  kind: string
  status: string | null
  prev_status: string | null
  detail: Record<string, unknown> | null
}

export const TERMINAL = new Set(['done', 'failed', 'killed', 'no_changes'])

export const isTerminal = (status: string): boolean => TERMINAL.has(status)

export const shortId = (id: string): string => id.slice(0, 8)

// playmaker writes ISO timestamps with a zone (`+00:00`); older rows may
// lack one and are UTC too.
export const parseIso = (value: string | null | undefined): number | null => {
  if (!value) return null
  const text = /[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`
  const ms = Date.parse(text)

  return Number.isNaN(ms) ? null : ms
}

const asString = (value: unknown): string | null =>
  typeof value === 'string' ? value : null

const asNumber = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null

export const parseRows = (json: string): Row[] => {
  let data: unknown
  try {
    data = JSON.parse(json)
  } catch {
    return []
  }
  if (!Array.isArray(data)) return []
  const rows: Row[] = []
  for (const item of data) {
    if (typeof item !== 'object' || item === null) continue
    const r = item as Record<string, unknown>
    const id = asString(r.id)
    if (id === null) continue
    rows.push({
      id,
      agent: asString(r.agent) ?? '?',
      model: asString(r.model),
      prompt: (asString(r.prompt) ?? '').replace(/[ \t]+/g, ' ').trim(),
      status: asString(r.status) ?? 'pending',
      startedAt: asString(r.started_at),
      finishedAt: asString(r.finished_at),
      batchId: asString(r.batch_id),
      cwd: asString(r.cwd),
      durationSeconds: asNumber(r.duration_seconds),
      failReason: asString(r.fail_reason),
    })
  }

  return rows
}

// One JSON object per line; a line that is not JSON is skipped.
export const parseEvents = (text: string): Event[] => {
  const events: Event[] = []
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (trimmed === '') continue
    try {
      const e = JSON.parse(trimmed) as Record<string, unknown>
      const id = asNumber(e.id)
      const kind = asString(e.kind)
      if (id === null || kind === null) continue
      events.push({
        id,
        ts: asString(e.ts) ?? '',
        session_id: asString(e.session_id),
        agent: asString(e.agent),
        model: asString(e.model),
        batch_id: asString(e.batch_id),
        kind,
        status: asString(e.status),
        prev_status: asString(e.prev_status),
        detail:
          typeof e.detail === 'object' && e.detail !== null
            ? (e.detail as Record<string, unknown>)
            : null,
      })
    } catch {
      continue
    }
  }
  events.sort((a, b) => a.id - b.id)

  return events
}

// Every provider with every window playmaker reports, as CodexBar lists them.
export const parseQuotas = (json: string): Quotas | null => {
  let data: unknown
  try {
    data = JSON.parse(json)
  } catch {
    return null
  }
  if (typeof data !== 'object' || data === null) return null
  const top = data as Record<string, unknown>
  const providers = top.providers
  if (typeof providers !== 'object' || providers === null) return null
  const lanes: Quota[] = []
  for (const [lane, raw] of Object.entries(providers as Record<string, unknown>)) {
    if (typeof raw !== 'object' || raw === null) continue
    const p = raw as Record<string, unknown>
    const readWindows = (list: unknown): QuotaWindow[] => {
      const out: QuotaWindow[] = []
      for (const w of Array.isArray(list) ? list : []) {
        if (typeof w !== 'object' || w === null) continue
        const win = w as Record<string, unknown>
        const name = asString(win.name)
        if (name === null) continue
        out.push({ name, pct: asNumber(win.pct_left), reset: asString(win.reset_relative) })
      }

      return out
    }
    let windows = readWindows(p.windows)
    let staleAt: string | null = null
    // A failed probe: playmaker keeps the last good numbers beside the error.
    const kept = p.last_good
    if (windows.length === 0 && typeof kept === 'object' && kept !== null) {
      const k = kept as Record<string, unknown>
      windows = readWindows(k.windows)
      if (windows.length > 0) staleAt = asString(k.at) ?? ''
    }
    lanes.push({ lane, status: asString(p.status) ?? 'unknown', tier: asString(p.tier), windows, staleAt })
  }

  return { fetchedAt: asString(top.fetched_at) ?? '', lanes }
}

// The window with the least left: the one that ends a session first. For
// `claude` that is the Fable weekly.
export const binding = (q: Quota): QuotaWindow | null => {
  let best: QuotaWindow | null = null
  for (const w of q.windows) {
    if (w.pct === null) continue
    if (best === null || best.pct === null || w.pct < best.pct) best = w
  }

  return best
}

// Names, colors and order as CodexBar shows them.
export type Lane = { lane: string; name: string; color: string }

const LANES: Lane[] = [
  { lane: 'claude', name: 'Claude', color: '#D98B6A' },
  { lane: 'codex', name: 'Codex', color: '#5BA3AD' },
  { lane: 'agy', name: 'Antigravity', color: '#6FCF7A' },
  { lane: 'zai', name: 'z.ai / GLM', color: '#E05A5F' },
  { lane: 'opencode', name: 'opencode', color: '#E05A5F' },
  { lane: 'kimi', name: 'Kimi Code', color: '#F2E55C' },
  { lane: 'muse', name: 'Muse Code', color: '#2F6BFF' },
  { lane: 'claude-api', name: 'Claude Platform', color: '#D98B6A' },
  { lane: 'gemini', name: 'Gemini', color: '#6FCF7A' },
  { lane: 'ollama', name: 'Ollama', color: '#888780' },
]

export const laneOf = (lane: string): Lane =>
  LANES.find(l => l.lane === lane) ?? { lane, name: lane, color: '#888780' }

export const laneColor = (lane: string): string => laneOf(lane).color

// The lane a worker row shows: `opencode` fronts other providers, so its
// model says which one (z.ai / GLM, Ollama); any other lane is itself.
export const laneOfRow = (agent: string, model: string | null): Lane => {
  if (agent === 'opencode' && model !== null) {
    if (/^zai|glm/i.test(model)) return laneOf('zai')
    if (/^ollama\//i.test(model)) return laneOf('ollama')
  }

  return laneOf(agent)
}

// `kimi-code/k3-256k` → `k3-256k`: the model without its provider prefix.
export const modelShort = (model: string | null): string =>
  model === null ? '' : model.replace(/^[^/]+\//, '')

// The pane's view of the lanes: Claude Platform (the Console credits the
// `claude-api` lane spends) folds into the Claude block as its `Credits`
// window, since it is the same account seen from two purses.
export const paneLanes = (q: Quotas): Quota[] => {
  const claude = q.lanes.find(l => l.lane === 'claude')
  const platform = q.lanes.find(l => l.lane === 'claude-api')
  if (claude === undefined || platform === undefined) return orderedLanes(q)
  // Credits come after Claude's own windows; a Claude probe that failed with
  // nothing kept still shows them.
  const merged: Quota = { ...claude, windows: [...claude.windows, ...platform.windows] }

  return orderedLanes({ ...q, lanes: q.lanes.filter(l => l.lane !== 'claude-api') }).map(l =>
    l.lane === 'claude' ? merged : l,
  )
}

export const orderedLanes = (q: Quotas): Quota[] =>
  [...q.lanes].sort((a, b) => {
    const ia = LANES.findIndex(l => l.lane === a.lane)
    const ib = LANES.findIndex(l => l.lane === b.lane)

    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib)
  })

const windowTag = (w: QuotaWindow): string => {
  if (/fable/i.test(w.name)) return '·Fable'
  if (/week|7-day/i.test(w.name)) return '·wk'
  if (/credit/i.test(w.name)) return '·$'

  return ''
}

// `codex 97 · claude 65·Fable · kimi 93 · muse ok`: the line under the prompt
// and the one the model reads beside a dispatch.
export const quotaLine = (q: Quotas): string =>
  orderedLanes(q)
    .filter(l => l.status !== 'unsupported')
    .map(l => {
      const w = binding(l)
      if (w === null || w.pct === null) return `${l.lane} ${l.status === 'ok' ? 'ok' : '?'}`
      // Kept numbers say how old they are, so nobody reads them as current.
      const asOf = l.staleAt === null ? '' : `(as of ${clockTime(l.staleAt) || '?'})`

      return `${l.lane} ${Math.round(w.pct)}${windowTag(w)}${asOf}`
    })
    .join(' · ')

// A thin bar for the graphical surfaces: the track in the lane's color at low
// opacity, the fill at full. No viewBox, so a user unit is a pixel and the
// rounded ends stay round whatever the width; the widths are percentages.
export const svgBar = (pct: number | null, color: string): string => {
  const width = pct === null ? 0 : Math.max(0, Math.min(100, pct))

  return (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="6">' +
    `<rect x="0" y="0" width="100%" height="6" rx="3" ry="3" fill="${color}" fill-opacity="0.22"/>` +
    (width > 0 ? `<rect x="0" y="0" width="${width}%" height="6" rx="3" ry="3" fill="${color}"/>` : '') +
    '</svg>'
  )
}

export const basename = (path: string | null): string => {
  if (path === null) return ''
  const parts = path.replace(/\/+$/, '').split('/')

  return parts[parts.length - 1] ?? ''
}

export const clockTime = (iso: string | null): string => {
  const ms = parseIso(iso)
  if (ms === null) return ''
  const d = new Date(ms)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')

  return `${hh}:${mm}`
}

// `just now`, `4 min ago`, `2 h ago`: how old the quota snapshot is.
export const ago = (iso: string, nowMs: number): string => {
  const then = parseIso(iso)
  if (then === null) return ''
  const s = Math.max(0, Math.round((nowMs - then) / 1000))
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min ago`
  const h = Math.floor(m / 60)

  return `${h} h ago`
}

export const duration = (seconds: number): string => {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  const h = Math.floor(m / 60)

  return `${h}h ${String(m % 60).padStart(2, '0')}m`
}

export const elapsed = (row: Row, nowMs: number): string => {
  if (row.durationSeconds !== null && isTerminal(row.status)) {
    return duration(row.durationSeconds)
  }
  const start = parseIso(row.startedAt)
  if (start === null) return ''
  const end = parseIso(row.finishedAt) ?? nowMs

  return duration((end - start) / 1000)
}

export const statusGlyph = (status: string): string => {
  switch (status) {
    // Small, so it sits with the check and the cross.
    case 'running':
      return '•'
    case 'pending':
      return '◦'
    case 'done':
      return '✓'
    case 'failed':
      return '✗'
    case 'killed':
      return '✗'
    case 'no_changes':
      return '△'
    default:
      return '·'
  }
}

export const statusWord = (status: string): string => {
  switch (status) {
    case 'running':
      return 'Running'
    case 'pending':
      return 'Pending'
    case 'done':
      return 'Done'
    case 'failed':
      return 'Failed'
    case 'killed':
      return 'Killed'
    case 'no_changes':
      return 'No changes'
    default:
      return status
  }
}

// The indicator's color: working, finished, failed.
export const statusColor = (status: string): string => {
  switch (status) {
    case 'running':
      return '#378ADD'
    case 'done':
      return '#5DBB63'
    case 'failed':
    case 'killed':
      return '#E0515A'
    case 'no_changes':
      return '#E8D44D'
    default:
      return '#888780'
  }
}

// Rows worth a line in the pane: anything not finished, what finished in the
// last two hours, and, so the board is never empty, the latest finished ones
// up to `minRows`. Newest first, at most `cap`.
export const keepRecent = (
  rows: Row[],
  nowMs: number,
  maxAgeMs = 2 * 60 * 60_000,
  minRows = 12,
  cap = 30,
): Row[] => {
  const sorted = [...rows].sort(
    (a, b) => (parseIso(b.startedAt) ?? 0) - (parseIso(a.startedAt) ?? 0),
  )
  const kept = sorted.filter(r => {
    if (!isTerminal(r.status)) return true
    const end = parseIso(r.finishedAt)

    return end !== null && nowMs - end <= maxAgeMs
  })
  const ids = new Set(kept.map(r => r.id))
  for (const r of sorted) {
    if (kept.length >= minRows) break
    if (ids.has(r.id)) continue
    kept.push(r)
    ids.add(r.id)
  }
  kept.sort((a, b) => (parseIso(b.startedAt) ?? 0) - (parseIso(a.startedAt) ?? 0))

  return kept.slice(0, cap)
}

export type Batch = { id: string | null; rows: Row[]; startedAt: number }

export const groupByBatch = (rows: Row[]): Batch[] => {
  const map = new Map<string | null, Batch>()
  for (const row of rows) {
    const start = parseIso(row.startedAt) ?? 0
    const batch = map.get(row.batchId)
    if (batch === undefined) {
      map.set(row.batchId, { id: row.batchId, rows: [row], startedAt: start })
    } else {
      batch.rows.push(row)
      batch.startedAt = Math.max(batch.startedAt, start)
    }
  }
  const batches = [...map.values()]
  // Newest first, the batches and the workers in each.
  for (const b of batches) {
    b.rows.sort((a, c) => (parseIso(c.startedAt) ?? 0) - (parseIso(a.startedAt) ?? 0))
  }
  batches.sort((a, b) => b.startedAt - a.startedAt)

  return batches
}

export const batchCounts = (rows: Row[]): string => {
  const total = rows.length
  const done = rows.filter(r => r.status === 'done').length
  const failed = rows.filter(r => r.status === 'failed' || r.status === 'killed').length
  const noChanges = rows.filter(r => r.status === 'no_changes').length
  const running = rows.filter(r => !isTerminal(r.status)).length
  const parts = [`${done}/${total} done`]
  if (running > 0) parts.push(`${running} running`)
  if (failed > 0) parts.push(`${failed} failed`)
  if (noChanges > 0) parts.push(`${noChanges} no changes`)

  return parts.join(' · ')
}

// One line of analytics over what `playmaker list` returned: the last 24 h
// by outcome and by lane.
export const daySummary = (rows: Row[], nowMs: number): string => {
  const day = rows.filter(r => {
    const start = parseIso(r.startedAt)

    return start !== null && nowMs - start <= 24 * 60 * 60_000
  })
  if (day.length === 0) return 'Last 24 h: no runs'
  const done = day.filter(r => r.status === 'done').length
  const failed = day.filter(r => r.status === 'failed' || r.status === 'killed').length
  const running = day.filter(r => !isTerminal(r.status)).length
  const byLane = new Map<string, number>()
  for (const r of day) byLane.set(r.agent, (byLane.get(r.agent) ?? 0) + 1)
  const lanes = [...byLane.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([lane, n]) => `${lane} ${n}`)
    .join(', ')
  const parts = [`Last 24 h: ${day.length} runs`, `${done} done`]
  if (failed > 0) parts.push(`${failed} failed`)
  if (running > 0) parts.push(`${running} running`)

  return `${parts.join(' · ')} · ${lanes}`
}

// The line that names the work: the `# WP <label> — <title>` heading when the
// prompt carries a spec, else the prompt with the coach's boilerplate
// openings dropped.
const BOILERPLATE = /^(you are [^.]*\.|you implement [^.]*\.|you did not write it\.|your job is to [^.]*\.|change nothing[^.]*\.|this review is read-only\.)\s*/i

export const promptHead = (prompt: string, width: number): string => {
  const wp = /^#+\s*(WP[\w-]*\s[^\n]{1,120})/im.exec(prompt)
  const bySpec = wp === null ? /reviews\/([\w.-]+)\/spec\.md/.exec(prompt) : null
  let clean = wp !== null ? (wp[1] ?? '') : prompt.replace(/\s+/g, ' ')
  for (let i = 0; i < 6; i += 1) {
    const next = clean.replace(BOILERPLATE, '')
    if (next === clean) break
    clean = next
  }
  clean = clean.trim()
  if (wp === null && bySpec !== null) clean = `WP ${bySpec[1] ?? ''} · ${clean}`
  if (clean.length <= width) return clean

  return `${clean.slice(0, Math.max(0, width - 1))}…`
}

// The one JSON object a reviewer answers with, wherever it sits in the
// message (after a `[thinking]` preamble, after a line of prose, inside a
// fence), and where it sits; null when none.
type FoundJson = { value: Record<string, unknown>; start: number; end: number }

const extractJson = (block: string): FoundJson | null => {
  const start = block.indexOf('{')
  const end = block.lastIndexOf('}')
  if (start === -1 || end <= start) return null
  try {
    const v: unknown = JSON.parse(block.slice(start, end + 1))
    if (typeof v !== 'object' || v === null || Array.isArray(v)) return null

    return { value: v as Record<string, unknown>, start, end }
  } catch {
    return null
  }
}

// The sentence a reviewer's JSON answer carries, when it carries one.
const jsonProse = (v: Record<string, unknown> | null): string => {
  if (v === null) return ''
  for (const key of ['recommendation', 'summary', 'decision', 'result', 'strongest_counter', 'verdict']) {
    const s = asString(v[key])
    if (s !== null && s !== '') return s
  }

  return ''
}

// Markdown and other noise out of an assistant message, as running prose.
const asProse = (block: string): string =>
  block
    .replace(/```[\s\S]*?```/g, ' ')
    .split('\n')
    .map(l => l.trim())
    .filter(l => l !== '' && !l.startsWith('[thinking]') && !l.startsWith('|'))
    .map(l => l.replace(/^#+\s*/, '').replace(/^[-*]\s+/, '').replace(/^\d+\.\s+/, ''))
    .join(' ')
    // A link keeps its text; its address may hold one level of parentheses
    // (`app/(tabs)/browse.tsx`).
    .replace(/\[([^\]]+)\]\((?:[^()]|\([^()]*\))*\)/g, '$1')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .replace(/\s+/g, ' ')
    .trim()

// What is going on in a worker's session, out of `playmaker summary <id>`:
// the last assistant message as prose. When that message is a JSON answer,
// the sentence it carries (a recommendation, a summary); failing that, the
// message before it.
export const condenseSummary = (text: string, width = 320): string => {
  const blocks = text
    .split(/^--- assistant @ [^\n]*---\s*$/m)
    .map(b => b.trim())
    .filter(b => b !== '' && !/^changes:\s/.test(b))
  const last = blocks[blocks.length - 1] ?? ''
  let prose = ''
  const json = extractJson(last)
  if (json !== null) {
    // The sentence the answer carries; else the prose around the JSON; else
    // the message before it.
    const sentence = jsonProse(json.value)
    const around = asProse(last.slice(0, json.start) + ' ' + last.slice(json.end + 1))
    const before = blocks[blocks.length - 2]
    prose = sentence.length >= 24 ? sentence : around !== '' ? around : before === undefined ? sentence : asProse(before)
  } else {
    prose = asProse(last)
  }
  if (prose === '') prose = asProse(text.replace(/^changes:[^\n]*\n?/m, ''))
  if (prose === '') return 'no assistant message yet'

  return prose.length <= width ? prose : `${prose.slice(0, width - 1)}…`
}

const rowLine = (row: Row, nowMs: number): string => {
  const model = row.model ? ` ${row.model}` : ''
  const fail = row.failReason ? ` · reason: ${row.failReason}` : ''

  return `- ${row.agent}${model} ${shortId(row.id)} · ${statusWord(row.status)} · ${elapsed(row, nowMs)} · «${promptHead(row.prompt, 90)}»${fail}`
}

// The wake-up brief for a finished batch: facts only, no advice. The coach
// decides what follows.
export const batchBrief = (
  batchId: string,
  detail: Record<string, unknown> | null,
  rows: Row[],
  nowMs: number,
  quotas: string | null,
): string => {
  const total = asNumber(detail?.total) ?? rows.length
  const done = asNumber(detail?.done) ?? rows.filter(r => r.status === 'done').length
  const failed = asNumber(detail?.failed) ?? 0
  const noChanges = asNumber(detail?.no_changes) ?? 0
  const lines = [
    `Batch ${batchId} finished: ${done}/${total} done, ${failed} failed, ${noChanges} no changes.`,
    ...rows.map(r => rowLine(r, nowMs)),
  ]
  const cwds = [...new Set(rows.map(r => r.cwd).filter((c): c is string => c !== null))]
  if (cwds.length > 0) lines.push(`Directories: ${cwds.join(', ')}`)
  if (quotas) lines.push(`Quotas: ${quotas}`)
  lines.push(
    `Details: playmaker summary <id>, playmaker get <id>; events: playmaker events --batch ${batchId}`,
  )

  return lines.join('\n')
}

export const loneBrief = (row: Row, nowMs: number, quotas: string | null): string => {
  const lines = [
    `Worker ${row.agent} ${shortId(row.id)} finished: ${statusWord(row.status)}, ${elapsed(row, nowMs)}.`,
    rowLine(row, nowMs),
  ]
  if (row.cwd) lines.push(`Directory: ${row.cwd}`)
  if (quotas) lines.push(`Quotas: ${quotas}`)
  lines.push(`Details: playmaker summary ${shortId(row.id)}, playmaker get ${shortId(row.id)}`)

  return lines.join('\n')
}

// What one Bash call of this session started: batch labels it named, label
// prefixes of the review scripts' rounds, and worker ids its output printed.
// The wake-up goes only to the session whose own call started the work.
export type Claims = { batches: string[]; prefixes: string[]; sessions: string[] }

const UUID = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/g

const unquote = (s: string): string => s.replace(/^["']|["']$/g, '')

export const claimsOf = (command: string, output: string): Claims => {
  const vars = new Map<string, string>()
  for (const m of command.matchAll(/(?:^|[\s;&|(])([A-Za-z_]\w*)=("[^"]*"|'[^']*'|[^\s;&|)]+)/g)) {
    if (m[1] !== undefined && m[2] !== undefined) vars.set(m[1], unquote(m[2]))
  }
  const resolve = (raw: string): string | null => {
    const v = unquote(raw)
    const ref = /^\$\{?([A-Za-z_]\w*)\}?$/.exec(v)
    if (ref !== null) return vars.get(ref[1] ?? '') ?? null

    return v.includes('$') ? null : v
  }
  const batches: string[] = []
  const prefixes: string[] = []
  for (const m of command.matchAll(/--batch(?:=|\s+)("[^"]*"|'[^']*'|\S+)/g)) {
    const label = resolve(m[1] ?? '')
    if (label) batches.push(label)
  }
  const board = /review-board\.sh\s+((?:-\S+\s+)*)([^\s-][^\s;&|]*)/.exec(command)
  if (board !== null && board[2] !== undefined) {
    const wp = resolve(board[2])
    if (wp) prefixes.push(`review-${wp}-r`, `precheck-${wp}-r`)
  }
  const decision = /review-decision\.sh\s+([^\s-][^\s;&|]*)/.exec(command)
  if (decision !== null && decision[1] !== undefined) {
    const slug = resolve(decision[1])
    if (slug) batches.push(`decision-${slug}`)
  }
  const sessions = [...new Set([...command.matchAll(UUID), ...output.matchAll(UUID)].map(m => m[0]))]

  return { batches, prefixes, sessions }
}

export const mergeClaims = (a: Claims, b: Claims, cap = 200): Claims => ({
  batches: [...new Set([...a.batches, ...b.batches])].slice(-cap),
  prefixes: [...new Set([...a.prefixes, ...b.prefixes])].slice(-cap),
  sessions: [...new Set([...a.sessions, ...b.sessions])].slice(-cap),
})

export const isClaimed = (c: Claims, batchId: string | null, memberIds: string[]): boolean =>
  (batchId !== null && (c.batches.includes(batchId) || c.prefixes.some(p => batchId.startsWith(p)))) ||
  memberIds.some(id => c.sessions.includes(id))

// Sessions this one started, out of `start_session`'s (or a hand-off's)
// answer: `Started session "<title>" (session_id: local_…, …)`. Any other
// `local_` id in the text counts too, without a title.
export const childrenOf = (text: string): Child[] => {
  const found = new Map<string, string>()
  for (const m of text.matchAll(/session "([^"]*)" \(session_id: (local_[0-9a-f-]{36})/g)) {
    if (m[2] !== undefined) found.set(m[2], (m[1] ?? '').replace(/^[^\p{L}\p{N}]+/u, '').trim())
  }
  for (const m of text.matchAll(/local_[0-9a-f-]{36}/g)) {
    if (!found.has(m[0])) found.set(m[0], '')
  }

  return [...found].map(([id, title]) => ({ id, title }))
}

export const mergeChildren = (a: Child[], b: Child[], cap = 100): Child[] => {
  const byId = new Map(a.map(c => [c.id, c]))
  for (const c of b) {
    const had = byId.get(c.id)
    byId.set(c.id, { id: c.id, title: c.title !== '' ? c.title : (had?.title ?? '') })
  }

  return [...byId.values()].slice(-cap)
}

// What a session leaves in `~/.playmaker/mod-claims/<id>.json` for the
// session that started it.
export const parseClaimsFile = (text: string | null): ClaimsFile | null => {
  if (text === null) return null
  try {
    const d = JSON.parse(text) as Partial<ClaimsFile>
    const c = d.claims
    if (c === undefined || !Array.isArray(c.batches)) return null

    return {
      claims: { batches: c.batches, prefixes: c.prefixes ?? [], sessions: c.sessions ?? [] },
      children: Array.isArray(d.children) ? d.children : [],
    }
  } catch {
    return null
  }
}

// The first owner that claims a batch: this session (label null) before
// any child, so a row shows once under whoever started it.
export const ownerOf = (owners: Owner[], batchId: string | null, memberIds: string[]): Owner | null =>
  owners.find(o => isClaimed(o.claims, batchId, memberIds)) ?? null

// A window's name as the pane draws it. Antigravity's Argon group has a
// weekly bucket of its own; cut to fit, "Gemini 4 Argon wee…" read as the
// plain Gemini weekly beside it.
export const windowLabel = (name: string): string => name.replace(/^Gemini \d+(?:\.\d+)? Argon\b/, 'Argon')

// `playmaker dispatch <lane> …` anywhere in a Bash command; the first lane.
export const dispatchLane = (command: string): string | null => {
  const m = /\bplaymaker\s+dispatch\s+([a-z][\w-]*)/i.exec(command)

  return m === null ? null : (m[1] ?? null)
}

export const tailLines = (text: string, n: number): string[] => {
  const lines = text.replace(/\s+$/, '').split('\n')

  return lines.slice(Math.max(0, lines.length - n))
}

// The tiers section of a policy file: from the first `## …Tiers…` heading to
// the next `## `; the whole file when it has no such heading and is short.
export const tiersSection = (text: string): string | null => {
  const lines = text.split('\n')
  let start = -1
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i] ?? ''
    if (/^##\s/.test(line) && /tier|тир/i.test(line)) {
      start = i
      break
    }
  }
  if (start === -1) return text.length <= 6000 ? text : null
  let end = lines.length
  for (let i = start + 1; i < lines.length; i += 1) {
    if (/^##\s/.test(lines[i] ?? '')) {
      end = i
      break
    }
  }

  return lines.slice(start, end).join('\n').trim()
}

export const splitArgs = (args: string): string[] =>
  args
    .trim()
    .split(/\s+/)
    .filter(a => a !== '')
