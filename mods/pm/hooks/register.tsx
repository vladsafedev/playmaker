// pm: playmaker beside the chat.
//
// Shows, carries and wakes; decides nothing. Two panes, drawn as tabs
// (Quotas, Workers), read playmaker's own JSON (`list`, `events`, `quotas`,
// `summary`); a finished batch of this project becomes a message in the
// chat with the facts; the policy tiers travel into the system prompt as
// they are; `/pm …` answers without a model turn.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderInput } from 'claude-code'

import type { Claims, Note, Quota, Quotas, Review, Row } from '../types'
import {
  basename,
  batchBrief,
  batchCounts,
  claimsOf,
  clockTime,
  condenseSummary,
  dispatchLane,
  elapsed,
  groupByBatch,
  isClaimed,
  isTerminal,
  keepRecent,
  laneOf,
  laneOfRow,
  loneBrief,
  mergeClaims,
  modelShort,
  paneLanes,
  parseEvents,
  parseQuotas,
  parseRows,
  promptHead,
  quotaLine,
  shortId,
  splitArgs,
  statusColor,
  statusGlyph,
  statusWord,
  svgBar,
  tailLines,
  tiersSection,
} from './playmaker'
import type { Event } from './playmaker'

const PANE = 'pm'
const TITLE = 'playmaker'
const USAGE = [
  '/pm board — open or hide the pane',
  '/pm quotas — probe the quotas now',
  '/pm review <wp> <base-ref> [--impl-id <id> …] — run the review board detached; the result arrives as a message',
  '/pm wp <id> — the worker’s summary (playmaker summary) into the prompt box',
  '/pm policy — re-read policy.md',
].join('\n')

const rows = atom({ plugin: 'pm', key: 'rows' } as const, [] as Row[], { shape: 'rows-1' })
// Shaped: a reload with another window layout must not read the old value.
const quotas = atom({ plugin: 'pm', key: 'quotas' } as const, null as Quotas | null, {
  shape: 'quotas-3',
})
const policy = atom({ plugin: 'pm', key: 'policy' } as const, null as string | null)
const review = atom({ plugin: 'pm', key: 'review' } as const, null as Review | null)
const error = atom({ plugin: 'pm', key: 'error' } as const, null as string | null)
const mine = atom(
  { plugin: 'pm', key: 'mine' } as const,
  { batches: [], prefixes: [], sessions: [] } as Claims,
  { shape: 'mine-1' },
)
const notes = atom({ plugin: 'pm', key: 'notes' } as const, {} as Record<string, Note>, {
  shape: 'notes-2',
})

type Engine = EngineInterface
// The distributive form: a check of `e.surface` narrows `e` and its table.
type PaneInput = RenderInput<'Pane'>

// Module state: starts over on a reload; the atoms above survive one.
let bin: string | null = null
let home = ''
let cwd = ''
let lastEventId = -1
let isPolling = false
let isProbing = false
let tick = 0
let wake = true
let wakeLone = false

const setError = async ($: Engine, text: string | null): Promise<void> => {
  const current = await read($, error)
  if (current !== text) await update($, error, () => text)
}

// One playmaker call: stdout on success, null (and the error line) otherwise.
const run = async (
  $: Engine,
  args: string[],
  timeoutMs: number,
  isQuiet = false,
): Promise<string | null> => {
  if (bin === null) return null
  const what = `playmaker ${args[0] ?? ''}`
  try {
    const ran = await $.process.run([bin, ...args], { timeoutMs })
    if (ran.exitCode !== 0) {
      if (!isQuiet) await setError($, `${what}: exit ${ran.exitCode} ${ran.stderr.trim().slice(0, 160)}`)

      return null
    }
    if (!isQuiet) await setError($, null)

    return ran.stdout
  } catch (err) {
    if (!isQuiet) await setError($, `${what}: ${String(err).slice(0, 160)}`)

    return null
  }
}

const findBinary = async ($: Engine): Promise<string> => {
  try {
    const ran = await $.process.run(['sh', '-lc', 'command -v playmaker'], { timeoutMs: 10000 })
    const found = (ran.stdout.trim().split('\n')[0] ?? '').trim()
    if (ran.exitCode === 0 && found !== '') return found
  } catch {
    // the fallback below
  }

  return `${home}/.local/bin/playmaker`
}

const readFile = async ($: Engine, path: string): Promise<string | null> => {
  try {
    const text = await $.fs.read(path)

    return typeof text === 'string' ? text : null
  } catch {
    return null
  }
}

// The tiers sections of the owner's policy files, copied as they are.
const loadPolicy = async ($: Engine): Promise<number> => {
  const personal = await readFile($, `${home}/.playmaker/policy.md`)
  const repo = cwd === '' ? null : await readFile($, `${cwd}/.playmaker/policy.md`)
  const parts: string[] = []
  const personalTiers = personal === null ? null : tiersSection(personal)
  if (personalTiers) {
    parts.push(`## Personal policy (~/.playmaker/policy.md), tiers section, verbatim\n\n${personalTiers}`)
  }
  const repoTiers = repo === null ? null : tiersSection(repo)
  if (repoTiers) {
    parts.push(`## Repo policy (./.playmaker/policy.md), tiers section, verbatim\n\n${repoTiers}`)
  }
  if (parts.length === 0) {
    await update($, policy, () => null)

    return 0
  }
  const files = [
    personal === null ? null : '~/.playmaker/policy.md',
    repo === null ? null : './.playmaker/policy.md',
  ].filter((f): f is string => f !== null)
  const text = [
    '# playmaker policy, carried by the pm mod',
    `These sections are copied from the owner's policy files as they are; the mod decides nothing. Full files: ${files.join(', ')}. The coach reads the rest of them itself before planning, as the playmaker-coach skill says.`,
    ...parts,
  ].join('\n\n')
  await update($, policy, () => text)

  return text.length
}

const pollQuotas = async ($: Engine, refresh = false): Promise<void> => {
  if (isProbing || bin === null) return
  isProbing = true
  try {
    const args = refresh ? ['quotas', '--refresh', '--json'] : ['quotas', '--json']
    const out = await run($, args, 120000)
    if (out === null) return
    const parsed = parseQuotas(out)
    if (parsed === null) {
      await setError($, 'playmaker quotas: unreadable answer')

      return
    }
    await update($, quotas, () => parsed)
    $.ui.status(`pm · ${quotaLine(parsed)}`)
  } finally {
    isProbing = false
  }
}

// What each worker is doing, from `playmaker summary`: once for a finished
// one, every half minute for one still running; at most eight calls a round.
const refreshNotes = async ($: Engine, kept: Row[], now: number): Promise<void> => {
  const current = await read($, notes)
  const next: Record<string, Note> = {}
  let fetched = 0
  for (const r of kept) {
    const have = current[r.id]
    const isFresh =
      have !== undefined && have.status === r.status && (isTerminal(r.status) || now - have.at < 25000)
    if (isFresh || r.status === 'pending' || fetched >= 8) {
      if (have !== undefined) next[r.id] = have
      continue
    }
    fetched += 1
    const out = await run($, ['summary', shortId(r.id)], 15000, true)
    const text = out === null ? (have?.text ?? '') : condenseSummary(out)
    next[r.id] = { status: r.status, text, at: now }
  }
  if (JSON.stringify(next) !== JSON.stringify(current)) await update($, notes, () => next)
}

// playmaker's state is machine-wide and many sessions share one directory, so
// a session is woken only for work its own Bash calls started (`mine`). Every
// other batch shows as a toast at most.
const isOurs = async ($: Engine, batchId: string | null, members: Row[]): Promise<boolean> =>
  isClaimed(await read($, mine), batchId, members.map(r => r.id))

const handleEvents = async ($: Engine, events: Event[], all: Row[], now: number): Promise<void> => {
  // First sight: remember where the stream stands, replay nothing.
  if (lastEventId < 0) {
    lastEventId = events.length === 0 ? 0 : (events[events.length - 1]?.id ?? 0)

    return
  }
  const q = await read($, quotas)
  const qLine = q === null ? null : quotaLine(q)
  for (const ev of events) {
    if (ev.id <= lastEventId) continue
    lastEventId = ev.id
    const who = `${ev.agent ?? '?'} ${ev.session_id === null ? '' : shortId(ev.session_id)}`.trim()
    const inBatch = ev.batch_id === null ? '' : ` · ${ev.batch_id}`
    if (ev.kind === 'batch' && ev.batch_id !== null) {
      const members = all.filter(r => r.batchId === ev.batch_id)
      const ours = await isOurs($, ev.batch_id, members)
      $.ui.toast(
        `batch ${ev.batch_id}: ${batchCounts(members)}${ours ? '' : ` · ${basename(members[0]?.cwd ?? null)}`}`,
      )
      if (wake && ours) void $.prompt.submit({ text: batchBrief(ev.batch_id, ev.detail, members, now, qLine) })
      continue
    }
    if (ev.kind === 'status' && ev.status !== null) {
      if (ev.status === 'running') {
        $.ui.toast(`● ${who} started${inBatch}`)
        continue
      }
      if (!isTerminal(ev.status)) continue
      $.ui.toast(`${statusGlyph(ev.status)} ${who} ${statusWord(ev.status).toLowerCase()}${inBatch}`)
      if (ev.batch_id === null && wake && wakeLone) {
        const row = all.find(r => r.id === ev.session_id)
        if (row !== undefined && (await isOurs($, null, [row]))) void $.prompt.submit({ text: loneBrief(row, now, qLine) })
      }
      continue
    }
    if (ev.kind === 'fallback') {
      const lane = typeof ev.detail?.lane === 'string' ? ev.detail.lane : '?'
      const reason = typeof ev.detail?.reason === 'string' ? ev.detail.reason : ''
      $.ui.toast(`↷ ${who} → ${lane}${reason === '' ? '' : ` (${reason})`}${inBatch}`)
    }
  }
}

// A review board started by `/pm review` runs detached; its exit file says
// when it ended, and the log's tail goes to the chat.
const checkReview = async ($: Engine): Promise<void> => {
  const current = await read($, review)
  if (current === null || current.isDone) return
  const exit = await readFile($, `${current.log}.exit`)
  const log = (await readFile($, current.log)) ?? ''
  if (exit === null) {
    const tail = tailLines(log, 12)
    if (tail.join('\n') !== current.tail.join('\n')) {
      await update($, review, r => (r === null ? null : { ...r, tail }))
    }

    return
  }
  const code = exit.trim()
  await update($, review, r =>
    r === null ? null : { ...r, isDone: true, exitCode: code, tail: tailLines(log, 12) },
  )
  $.ui.toast(`review board ${current.wp}: finished, exit ${code}`)
  if (wake) {
    void $.prompt.submit({
      text: [
        `Review board ${current.wp} finished, exit code ${code}. Log: ${current.log}`,
        'Last lines of the log:',
        ...tailLines(log, 40),
      ].join('\n'),
    })
  }
}

const pollSessions = async ($: Engine): Promise<void> => {
  if (isPolling || bin === null) return
  isPolling = true
  try {
    tick += 1
    const now = await $.clock.now()
    let all: Row[] = []
    let kept: Row[] = []
    const listed = await run($, ['list', '--json', '--limit', '80'], 20000)
    if (listed !== null) {
      all = parseRows(listed)
      kept = keepRecent(all, now)
      const current = await read($, rows)
      const isLive = kept.some(r => !isTerminal(r.status))
      if (isLive || JSON.stringify(current) !== JSON.stringify(kept)) {
        await update($, rows, () => kept)
      }
    }
    const printed = await run($, ['events', '--last', '200'], 20000)
    if (printed !== null) await handleEvents($, parseEvents(printed), all, now)
    await checkReview($)
    if (tick % 10 === 1) await refreshNotes($, kept, now)
  } finally {
    isPolling = false
  }
}

const summaryToPrompt = async ($: Engine, id: string): Promise<string> => {
  const out = await run($, ['summary', id], 30000)
  if (out === null) return `playmaker summary ${id} did not answer.`
  const text = `Worker ${id} summary (playmaker summary):\n${out.trim()}\n`
  const filled = await $.prompt.fill({ text, mode: 'append' })

  return filled.isFilled ? `Summary of ${id} is in the prompt box.` : text
}

const togglePane = async ($: Engine): Promise<string> => {
  const isUp = (await $.ui.panes()).some(pane => pane.id === PANE)
  if (isUp) {
    await $.ui.close({ id: PANE })

    return 'Pane hidden.'
  }
  const opened = await $.ui.open({ id: PANE, title: TITLE, focus: true })

  return opened.isPlaced ? 'Pane open.' : `The pane opens once there is room: ${opened.reason}`
}

const startReview = async ($: Engine, args: string[]): Promise<string> => {
  const [wp, base, ...rest] = args
  if (wp === undefined || base === undefined) {
    return 'Usage: /pm review <wp> <base-ref> [--impl-id <id>] [review-board.sh options]'
  }
  const script = `${home}/.claude/skills/playmaker-coach/scripts/review-board.sh`
  const startedAt = await $.clock.now()
  const log = `${home}/.playmaker/logs/pm-review-${wp}-${startedAt}.log`
  // nohup'd: the command returns at once; the exit file marks the end.
  const wrapper =
    'nohup sh -c \'"$@" > "$PM_LOG" 2>&1; echo $? > "$PM_LOG.exit"\' sh "$@" > /dev/null 2>&1 & echo $!'
  try {
    const ran = await $.process.run(
      ['sh', '-c', wrapper, 'sh', script, '--auto', wp, base, ...rest],
      { cwd, env: { PM_LOG: log }, timeoutMs: 15000 },
    )
    const pid = Number(ran.stdout.trim())
    await update($, review, () => ({
      wp,
      pid: Number.isFinite(pid) ? pid : 0,
      log,
      startedAt,
      isDone: false,
      exitCode: null,
      tail: [],
    }))

    return `Review board ${wp} started detached (pid ${ran.stdout.trim()}). Log: ${log}. The result arrives as a message in the chat.`
  } catch (err) {
    return `Could not start the review board: ${String(err)}`
  }
}

// CodexBar's thin bar: an SVG on the graphical surfaces, colored cells on
// the terminal.
const drawBar = ($: Engine, e: PaneInput, pct: number | null, color: string) => {
  const width = pct === null ? 0 : Math.max(0, Math.min(100, Math.round(pct)))
  if (e.surface === 'terminal') {
    const { Box } = $.ui.resolve(e)

    return (
      <Box flexDirection="row" height={1}>
        <Box width={`${width}%`} backgroundColor={color} />
        <Box flexGrow={1} backgroundColor="subtle" />
      </Box>
    )
  }
  const { Svg } = $.ui.resolve(e)

  return <Svg source={svgBar(pct, color)} alt={`${width}% left`} height={6} />
}

const pctLabel = (pct: number | null): string => (pct === null ? '—' : `${Math.round(pct)}%`)

// Two columns that each flow on their own, the next lane going to the
// shorter one, so a lane with one window leaves no hole beside a tall one.
const splitColumns = (lanes: Quota[]): [Quota[], Quota[]] => {
  const left: Quota[] = []
  const right: Quota[] = []
  let leftHeight = 0
  let rightHeight = 0
  for (const l of lanes) {
    const height = 2 + Math.max(1, l.windows.length)
    if (leftHeight <= rightHeight) {
      left.push(l)
      leftHeight += height
    } else {
      right.push(l)
      rightHeight += height
    }
  }

  return [left, right]
}

// The quotas block at the top of the pane: one block per lane as CodexBar
// draws it, in two columns that flow on their own where there is room.
const drawQuotas = async ($: Engine, e: PaneInput) => {
  const { Box, Text } = $.ui.resolve(e)
  const q = await read($, quotas)
  const isTwoColumns = e.surface !== 'terminal' || e.props.bodyColumns >= 100
  // The terminal's cell is one character; a graphical surface draws caption
  // text narrower than its cell, so the number columns take fewer there.
  const isCells = e.surface === 'terminal'

  const block = (l: Quota, i: number) => {
    const lane = laneOf(l.lane)

    return (
      <Box flexDirection="column" marginTop={i === 0 ? 0 : 'lg'}>
        <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
          <Text weight="semibold" size="caption" color={lane.color}>
            {lane.name}
          </Text>
          <Text dimColor size="caption">
            {l.status === 'ok' ? '' : l.staleAt !== null ? `as of ${clockTime(l.staleAt) || '?'}` : l.status}
          </Text>
        </Box>
        {l.windows.length === 0 && (
          <Text dimColor size="caption">
            {l.status === 'ok' ? 'no windows reported' : l.status}
          </Text>
        )}
        {l.windows.map(w => (
          <Box flexDirection="column" marginTop="xs">
            <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
              <Text size="caption" wrap="truncate-end">
                {w.name}
              </Text>
              <Box flexDirection="row" flexShrink={0} gap={1}>
                <Box width={isCells ? 4 : 3} justifyContent="flex-end">
                  <Text size="caption" tabular>
                    {pctLabel(w.pct)}
                  </Text>
                </Box>
                <Box width={isCells ? 6 : 5} justifyContent="flex-end">
                  <Text dimColor size="caption" tabular>
                    {w.reset ?? ''}
                  </Text>
                </Box>
              </Box>
            </Box>
            {drawBar($, e, w.pct, lane.color)}
          </Box>
        ))}
      </Box>
    )
  }

  const lanes = q === null ? [] : paneLanes(q).filter(l => l.status !== 'unsupported')
  const [left, right] = splitColumns(lanes)

  return (
    <Box flexDirection="column">
      {q === null && <Text dimColor>probing playmaker quotas…</Text>}
      {q !== null && !isTwoColumns && <Box flexDirection="column">{lanes.map(block)}</Box>}
      {q !== null && isTwoColumns && (
        <Box flexDirection="row" justifyContent="space-between" alignItems="flex-start">
          <Box flexDirection="column" width="47%">
            {left.map(block)}
          </Box>
          <Box flexDirection="column" width="47%">
            {right.map(block)}
          </Box>
        </Box>
      )}
    </Box>
  )
}

export const register: Register = (on, options) => {
  wake = options.wake !== false
  wakeLone = options.wakeLone === true
  const quotaMinutes = Math.max(1, Number(options.quotaMinutes ?? 5) || 5)
  const policyInPrompt = options.policyInPrompt !== false
  const quotaOnDispatch = options.quotaOnDispatch !== false

  on('session.start', async ($, e, next) => {
    cwd = e.cwd
    home = (await $.env.get('HOME')) ?? ''
    await $.command.register({
      name: 'pm',
      description: 'playmaker: board, quotas, review board, worker summary',
      argumentHint: 'board | quotas | review <wp> <base> | wp <id> | policy',
    })
    bin = await findBinary($)
    await loadPolicy($)
    // The earlier version's second tab; nothing draws it any more.
    try {
      if ((await $.ui.panes()).some(pane => pane.id === 'pm-quotas')) await $.ui.close({ id: 'pm-quotas' })
    } catch {
      // no surface to ask; nothing to close
    }
    void $.ui.open({ id: PANE, title: TITLE })
    void pollSessions($)
    void pollQuotas($)
    $.clock.every(3000, () => {
      void pollSessions($)
    })
    $.clock.every(quotaMinutes * 60_000, () => {
      void pollQuotas($)
    })

    return next(e)
  })

  on('command.run', { command: 'pm' }, async ($, e) => {
    const [sub, ...rest] = splitArgs(e.args)
    switch (sub) {
      case 'board':
        return { text: await togglePane($) }
      case 'quotas': {
        await pollQuotas($, true)
        const q = await read($, quotas)

        return {
          text: q === null ? 'No quotas yet, see the pane.' : `Quotas (${clockTime(q.fetchedAt)}): ${quotaLine(q)}`,
        }
      }
      case 'review':
        return { text: await startReview($, rest) }
      case 'wp': {
        const id = rest[0]

        return { text: id === undefined ? 'Usage: /pm wp <id>' : await summaryToPrompt($, id) }
      }
      case 'policy': {
        const size = await loadPolicy($)

        return {
          text: size > 0 ? `Policy re-read: ${size} characters in the pm:policy section.` : 'No policy files found.',
        }
      }
      default:
        return { text: USAGE }
    }
  })

  // The policy tiers, as the owner wrote them, after the engine's own sections.
  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    const text = await read($, policy)
    if (!policyInPrompt || text === null) return composed

    return { sections: [...composed.sections, { id: 'pm:policy', text, scope: 'session' }] }
  })

  // The quota line rides with the person's prompt as hidden context.
  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind === 'plugin') return next(e)
    const q = await read($, quotas)
    if (q === null) return next(e)
    const line = `playmaker quotas ${clockTime(q.fetchedAt)}: ${quotaLine(q)}`

    return next({ ...e, context: [...(e.context ?? []), line] })
  }).catch(($, e, next) => next(e))

  // A dispatch runs as the coach asked; the model reads the quotas beside
  // its result. Never a deny.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const lane = dispatchLane(e.command)
    const starts = lane !== null || /review-(board|decision)\.sh/.test(e.command)
    const ran = await next(e)
    if (starts) {
      // Remember what this session started, so its batches wake it and only it.
      const claims = claimsOf(e.command, typeof ran.text === 'string' ? ran.text : '')
      if (claims.batches.length + claims.prefixes.length + claims.sessions.length > 0) {
        await update($, mine, held => mergeClaims(held, claims))
      }
    }
    if (!quotaOnDispatch || lane === null || ran.deny !== undefined) return ran
    const q = await read($, quotas)
    if (q === null) return ran
    const line = `pm: quotas at dispatch time (${clockTime(q.fetchedAt)}): ${quotaLine(q)}`

    return { ...ran, context: [...(ran.context ?? []), line] }
  }).catch(($, e, next) => next(e))

  // The pane: the quotas block, then every recent worker by batch.
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const quotasTree = await drawQuotas($, e)
    const list = await read($, rows)
    const current = await read($, review)
    const err = await read($, error)
    const noted = await read($, notes)
    const now = await $.clock.now()
    // The terminal cuts by cells; a graphical surface truncates by itself.
    const head = e.surface === 'terminal' ? Math.max(16, e.props.bodyColumns - 4) : 240
    const batches = groupByBatch(list)
    return (
      <Box flexDirection="column">
        {quotasTree}
        {batches.length === 0 && (
          <Box marginTop="xl">
            <Text dimColor>no workers yet</Text>
          </Box>
        )}
        {batches.map(b => (
          <Box flexDirection="column" marginTop="xl">
            <Box flexDirection="row" gap={1} alignItems="center">
              <Text weight="semibold">{b.id ?? 'no batch'}</Text>
              <Text dimColor size="caption">
                {batchCounts(b.rows)} · {basename(b.rows[0]?.cwd ?? null)}
              </Text>
            </Box>
            {b.rows.map(r => {
              const note = noted[r.id]
              const when = isTerminal(r.status) ? clockTime(r.finishedAt) : ''
              const meta = [elapsed(r, now), when].filter(s => s !== '').join(' · ')
              const lane = laneOfRow(r.agent, r.model)
              const model = modelShort(r.model)

              return (
                <Box flexDirection="column" marginTop="md">
                  <Box flexDirection="row" gap={1} alignItems="center">
                    <Text color={statusColor(r.status)}>{statusGlyph(r.status)}</Text>
                    <Text weight="semibold" color={lane.color}>
                      {lane.name}
                    </Text>
                    {model !== '' && (
                      <Text dimColor size="caption">
                        {model}
                      </Text>
                    )}
                    <Text dimColor mono size="caption">
                      {shortId(r.id)}
                    </Text>
                    <Text dimColor tabular size="caption">
                      {meta}
                    </Text>
                  </Box>
                  <Box paddingLeft={2}>
                    <Text size="footnote" wrap="truncate-end">
                      {promptHead(r.prompt, head)}
                    </Text>
                  </Box>
                  {note !== undefined && note.text !== '' && (
                    <Box paddingLeft={2}>
                      <Text dimColor size="caption" wrap="wrap">
                        {note.text}
                      </Text>
                    </Box>
                  )}
                </Box>
              )
            })}
          </Box>
        ))}
        {current !== null && (
          <Box flexDirection="column" marginTop="lg">
            <Text weight="semibold">
              {`Review board ${current.wp} ${current.isDone ? `· exit ${current.exitCode ?? '?'}` : '· running'}`}
            </Text>
            {current.tail.map(line => (
              <Text dimColor size="caption" wrap="truncate-end">
                {line}
              </Text>
            ))}
          </Box>
        )}
        {err !== null && <Text color="error">{err}</Text>}
      </Box>
    )
  })
}
