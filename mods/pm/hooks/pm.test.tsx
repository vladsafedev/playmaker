import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const ROWS = JSON.stringify([
  {
    id: 'c977dbdb-c1f9-488c-838b-86f9826acbe2',
    agent: 'codex',
    model: 'gpt-5.6-terra',
    prompt: 'review af-money, lens correctness',
    status: 'done',
    started_at: '2026-10-28T13:36:26+00:00',
    finished_at: '2026-10-28T13:37:14+00:00',
    batch_id: 'review-af-money-r1',
    cwd: '/repo',
    duration_seconds: 48,
  },
  {
    id: '0a2ffe97-983b-4cd9-8ce8-b0eb0401e705',
    agent: 'kimi',
    model: 'kimi-code/k3-256k',
    prompt: 'WP af-money — AppsFlyer as a destination of money events',
    status: 'running',
    started_at: '2026-10-28T13:30:00+00:00',
    finished_at: null,
    batch_id: 'af-money',
    cwd: '/repo',
    duration_seconds: null,
  },
  // `playmaker continue` on the review worker: a row of its own, no batch.
  {
    id: '9d8c7b6a-1e2f-4a3b-8c4d-5e6f7a8b9c0d',
    agent: 'codex',
    model: 'gpt-5.6-terra',
    prompt: 'Fix the finding from the board',
    status: 'running',
    started_at: '2026-10-28T13:39:00+00:00',
    finished_at: null,
    batch_id: null,
    parent_id: 'c977dbdb-c1f9-488c-838b-86f9826acbe2',
    cwd: '/repo',
    duration_seconds: null,
  },
  // Started by some other session in the same directory.
  {
    id: '5b1e0c3a-2f44-4d1e-9a0b-7d2c6e8f9a10',
    agent: 'zai',
    model: 'glm-5.3',
    prompt: 'WP stranger — unrelated work',
    status: 'running',
    started_at: '2026-10-28T13:31:00+00:00',
    finished_at: null,
    batch_id: 'stranger',
    cwd: '/repo',
    duration_seconds: null,
  },
])

const SELF = 'local_2f223b17-9156-4d6a-a4b8-7c2907ae2883'
const CHILD = 'local_1e176179-0619-4c28-926d-7e2f8ef7eebe'
const claimsAt = (id: string) => `/Users/vlad/.playmaker/mod-claims/${id}.json`

const EVENT_STATUS = JSON.stringify({
  id: 1,
  ts: '2026-10-28T13:37:14+00:00',
  session_id: 'c977dbdb-c1f9-488c-838b-86f9826acbe2',
  agent: 'codex',
  model: null,
  batch_id: 'review-af-money-r1',
  kind: 'status',
  status: 'done',
  prev_status: 'running',
  detail: null,
})

const EVENT_BATCH = JSON.stringify({
  id: 2,
  ts: '2026-10-28T13:38:06+00:00',
  session_id: null,
  agent: null,
  model: null,
  batch_id: 'review-af-money-r1',
  kind: 'batch',
  status: 'done',
  prev_status: null,
  detail: { total: 1, done: 1, failed: 0, no_changes: 0 },
})

const QUOTAS = JSON.stringify({
  fetched_at: '2026-10-28T13:35:00+00:00',
  providers: {
    codex: { status: 'ok', windows: [{ name: 'Session', pct_left: 97, reset_relative: '6d 7h' }] },
    claude: {
      status: 'ok',
      windows: [
        { name: 'Session', pct_left: 94, reset_relative: '4h' },
        { name: 'Weekly · Fable', pct_left: 65, reset_relative: '2d 3h' },
      ],
    },
    'claude-api': { status: 'ok', windows: [{ name: 'Credits', pct_left: 100, reset_relative: '6d' }] },
    // A failed probe with the last good numbers playmaker kept.
    kimi: {
      status: 'error',
      error: 'RuntimeError: HTTP 429',
      last_good: {
        at: '2026-10-28T13:20:00+00:00',
        windows: [{ name: 'Weekly', pct_left: 85, reset_relative: '5d 8h' }],
      },
    },
    agy: {
      status: 'ok',
      windows: [
        { name: 'Gemini weekly', pct_left: 96, reset_relative: '6d 3h' },
        { name: 'Gemini 4 Argon weekly', pct_left: 0, reset_relative: '6d 18h' },
      ],
    },
    muse: { status: 'ok', windows: [] },
    ollama: { status: 'unsupported', windows: [] },
  },
})

const POLICY = '# Policy\n\n## Tiers (owner order)\n\n| Senior | codex |\n\n## Quota economics\n\nrest of the file\n'

const PANE = {
  plugin: 'pm',
  component: 'Pane',
  requestId: 'pm',
  props: {
    title: 'Борд',
    isFocused: false,
    bodyColumns: 80,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const ok = (stdout: string) => ({
  value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})

// The world beneath the plugin: a clock, HOME, the policy file, the
// sessions' claim files and a playmaker that answers by its arguments.
const world = (on: On, phase: { batchDone: boolean }, files: Record<string, string> = {}) => {
  const clock = mock.clock(on, { now: Date.parse('2026-10-28T13:40:00Z') })
  mock.env(on, { HOME: '/Users/vlad', CLAUDE_CODE_HOST_SESSION_ID: SELF })
  on('fs.write', (_, e) => {
    files[e.path] = e.text

    return { value: undefined }
  })
  on('command.register', () => ({ value: { command: 'pm' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('fs.read', (_, e) => {
    if (e.path.includes('/mod-claims/')) {
      const text = files[e.path]
      if (text === undefined) throw new Error('ENOENT')

      return { value: text }
    }

    return { value: POLICY }
  })
  on('process.run', (_, e) => {
    const argv = e.argv.join(' ')
    if (argv.includes('command -v')) return ok('/usr/local/bin/playmaker\n')
    if (argv.includes(' list ')) return ok(ROWS)
    if (argv.includes(' events ')) {
      return ok(phase.batchDone ? `${EVENT_STATUS}\n${EVENT_BATCH}\n` : `${EVENT_STATUS}\n`)
    }
    if (argv.includes(' quotas ')) return ok(QUOTAS)
    if (argv.includes(' summary ')) return ok('All acceptance criteria met.')

    return ok('')
  })

  return clock
}

test('the pane lists the workers and a finished batch wakes the coach', async ($, on) => {
  const phase = { batchDone: false }
  // A session this one started has launched af-money.
  const files: Record<string, string> = {
    [claimsAt(CHILD)]: JSON.stringify({
      claims: { batches: ['af-money'], prefixes: [], sessions: [] },
      children: [],
    }),
  }
  const clock = world(on, phase, files)
  on('tool.call', (_, e) =>
    e.tool === 'mcp__ccd_session__start_session'
      ? { result: [{ type: 'text', text: `Started session "► Деньги в AppsFlyer" (session_id: ${CHILD}, name: af).` }] }
      : { result: {} },
  )
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)

    return { text: e.text }
  })

  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  await clock.settle()
  await $.tool.call({ tool: 'mcp__ccd_session__start_session', title: 'Деньги в AppsFlyer' })
  // This session starts the batch itself; the label comes through a variable.
  await $.tool.call({ tool: 'Bash', command: 'B=review-af-money-r1; playmaker dispatch codex --batch "$B" -p "x"' })
  await clock.advance(3000)

  // What it started is left for the session that started it.
  const left = JSON.parse(files[claimsAt(SELF)] ?? '{}')
  expect(left.claims.batches).toEqual(['review-af-money-r1'])
  expect(left.children).toEqual([{ id: CHILD, title: 'Деньги в AppsFlyer' }])

  for (const surface of ['terminal', 'desktop'] as const) {
    // The Workers tab: each worker with its lane, state and one-line summary.
    const workers = await $.ui.mount({ ...PANE, surface })
    expect(await workers.find({ type: 'Text', text: /Kimi Code/ })).toBeDefined()
    expect(await workers.find({ type: 'Text', text: /review-af-money-r1/ })).toBeDefined()
    // The state is the glyph before the name, nothing else.
    expect(await workers.find({ type: 'Text', text: /^•$/ })).toBeDefined()
    expect(await workers.find({ type: 'Text', text: /Running/ })).toBe(undefined)
    // The child's batch says whose it is; another session's is not here.
    expect(await workers.find({ type: 'Text', text: /↳ Деньги в AppsFlyer/ })).toBeDefined()
    expect(await workers.find({ type: 'Text', text: /unrelated work/ })).toBe(undefined)
    // A continued turn shows under the turn it went on from, as its own row.
    expect(await workers.find({ type: 'Text', text: /^↻$/ })).toBeDefined()
    expect(await workers.find({ type: 'Text', text: /Fix the finding from the board/ })).toBeDefined()
    expect(await workers.find({ type: 'Text', text: /^9d8c7b6a$/ })).toBeDefined()
    expect(await workers.find({ type: 'Text', text: /no batch/ })).toBe(undefined)
    expect(await workers.find({ type: 'Text', text: /All acceptance criteria met/ })).toBeDefined()
    await workers.unmount()

    // The quotas block at the top reads as CodexBar does: lane name, window, reset.
    const quotas = await $.ui.mount({ ...PANE, surface })
    expect(await quotas.find({ type: 'Text', text: /Codex/ })).toBeDefined()
    expect(await quotas.find({ type: 'Text', text: /^Weekly · Fable$/ })).toBeDefined()
    // Console credits sit inside the Claude block, not as a lane of their own.
    expect(await quotas.find({ type: 'Text', text: /^Credits$/ })).toBeDefined()
    expect(await quotas.find({ type: 'Text', text: /Claude Platform/ })).toBe(undefined)
    // A lane whose probe failed shows its kept numbers and says how old they are.
    expect(await quotas.find({ type: 'Text', text: /^85%$/ })).toBeDefined()
    expect(await quotas.find({ type: 'Text', text: /^as of \d\d:\d\d$/ })).toBeDefined()
    expect(await quotas.find({ type: 'Text', text: /^error$/ })).toBe(undefined)
    expect(await quotas.find({ type: 'Text', text: /^65%$/ })).toBeDefined()
    expect(await quotas.find({ type: 'Text', text: /6d 7h/ })).toBeDefined()
    // Argon's own weekly reads as Argon, not as a cut-off Gemini weekly.
    expect(await quotas.find({ type: 'Text', text: /^Argon weekly$/ })).toBeDefined()
    await quotas.unmount()
  }

  // Nothing replayed from before the session started.
  expect(submitted.length).toBe(0)

  phase.batchDone = true
  await clock.advance(3000)
  expect(submitted.length).toBe(1)
  const brief = submitted[0] ?? ''
  expect(brief.includes('Batch review-af-money-r1 finished: 1/1')).toBe(true)
  expect(brief.includes('codex gpt-5.6-terra c977dbdb')).toBe(true)
  expect(brief.includes('codex 97')).toBe(true)
  // Facts only: the brief never tells the coach what to do next.
  expect(/следующий шаг|next step|should|run the review/i.test(brief)).toBe(false)

  // The same event is not raised twice.
  await clock.advance(3000)
  expect(submitted.length).toBe(1)
})

const wakes = async (
  $: Engine,
  on: On,
  command: string | null,
): Promise<string[]> => {
  const phase = { batchDone: false }
  const clock = world(on, phase)
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)

    return { text: e.text }
  })
  on('tool.call', () => ({ result: {} }))
  // Same directory as the workers: a directory alone must never be enough.
  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  await clock.settle()
  await clock.advance(3000)
  if (command !== null) await $.tool.call({ tool: 'Bash', command })
  phase.batchDone = true
  await clock.advance(3000)
  await clock.advance(3000)

  return submitted
}

test('a batch another session started wakes nobody here, same directory or not', async ($, on) => {
  expect((await wakes($, on, null)).length).toBe(0)
})

test('a batch of some other label this session started does not count', async ($, on) => {
  expect((await wakes($, on, 'playmaker dispatch codex --batch other-batch -p x')).length).toBe(0)
})

test('the review board this session ran claims its rounds', async ($, on) => {
  const submitted = await wakes(
    $,
    on,
    '~/.claude/skills/playmaker-coach/scripts/review-board.sh --auto af-money main --impl-id 0a2ffe97',
  )
  expect(submitted.length).toBe(1)
  expect((submitted[0] ?? '').startsWith('Batch review-af-money-r1 finished')).toBe(true)
})

test('the system prompt carries the policy tiers, verbatim, after the engine', async ($, on) => {
  const clock = world(on, { batchDone: false })
  on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'engine', scope: 'shared' }] }))

  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  await clock.settle()

  const composed = await $.prompt.compose({
    model: 'm',
    promptModel: 'm',
    surfaces: ['desktop'],
    tools: [],
    outputStyle: null,
    traits: [],
  })
  expect(composed.sections[0]?.id).toBe('intro')
  const section = composed.sections.find(s => s.id === 'pm:policy')
  expect(section?.scope).toBe('session')
  expect(section?.text.includes('| Senior | codex |')).toBe(true)
  expect(section?.text.includes('rest of the file')).toBe(false)
})

test('a dispatch runs as asked and the model reads the quotas beside it', async ($, on) => {
  const clock = world(on, { batchDone: false })
  on('tool.call', () => ({ result: {} }))

  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  await clock.settle()

  const ran = await $.tool.call({ tool: 'Bash', command: 'playmaker dispatch codex --batch b -p "x"' })
  expect(ran.deny).toBe(undefined)
  expect((ran.context ?? []).some(line => line.includes('codex 97'))).toBe(true)

  const plain = await $.tool.call({ tool: 'Bash', command: 'ls' })
  expect((plain.context ?? []).length).toBe(0)
})
