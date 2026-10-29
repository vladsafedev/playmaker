import { expect, mock, test } from 'claude-code/testing'
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
])

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

// The world beneath the plugin: a clock, HOME, the policy file and a
// playmaker that answers by its arguments.
const world = (on: On, phase: { batchDone: boolean }) => {
  const clock = mock.clock(on, { now: Date.parse('2026-10-28T13:40:00Z') })
  mock.env(on, { HOME: '/Users/vlad' })
  on('command.register', () => ({ value: { command: 'pm' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('fs.read', () => ({ value: POLICY }))
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
  const clock = world(on, phase)
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)

    return { text: e.text }
  })

  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  await clock.settle()
  await clock.advance(3000)

  for (const surface of ['terminal', 'desktop'] as const) {
    // The Workers tab: each worker with its lane, state and one-line summary.
    const workers = await $.ui.mount({ ...PANE, surface })
    expect(await workers.find({ type: 'Text', text: /Kimi Code/ })).toBeDefined()
    expect(await workers.find({ type: 'Text', text: /review-af-money-r1/ })).toBeDefined()
    // The state is the glyph before the name, nothing else.
    expect(await workers.find({ type: 'Text', text: /^●$/ })).toBeDefined()
    expect(await workers.find({ type: 'Text', text: /Running/ })).toBe(undefined)
    expect(await workers.find({ type: 'Text', text: /All acceptance criteria met/ })).toBeDefined()
    await workers.unmount()

    // The quotas block at the top reads as CodexBar does: lane name, window, reset.
    const quotas = await $.ui.mount({ ...PANE, surface })
    expect(await quotas.find({ type: 'Text', text: /Codex/ })).toBeDefined()
    expect(await quotas.find({ type: 'Text', text: /^Weekly · Fable$/ })).toBeDefined()
    // Console credits sit inside the Claude block, not as a lane of their own.
    expect(await quotas.find({ type: 'Text', text: /^Credits$/ })).toBeDefined()
    expect(await quotas.find({ type: 'Text', text: /Claude Platform/ })).toBe(undefined)
    expect(await quotas.find({ type: 'Text', text: /^65%$/ })).toBeDefined()
    expect(await quotas.find({ type: 'Text', text: /6d 7h/ })).toBeDefined()
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

test('a batch of another project shows as a toast and wakes nobody', async ($, on) => {
  const phase = { batchDone: true }
  const clock = world(on, phase)
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)

    return { text: e.text }
  })

  // The rows live in /repo; this session runs elsewhere.
  await $.session.start({ cwd: '/elsewhere', surface: 'desktop', isInteractive: true })
  await clock.settle()
  phase.batchDone = true
  await clock.advance(3000)
  await clock.advance(3000)
  expect(submitted.length).toBe(0)
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
