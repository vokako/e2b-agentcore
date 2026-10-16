/**
 * Full test of the JS/TS provider: one or more checks for EVERY row of the support table.
 *
 *   export E2B_AGENTCORE_EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/<harness-exec-role>
 *   npm run build
 *   node dist/test/full.js offline   # no resources created (a few read-only AWS calls)
 *   node dist/test/full.js build     # builds full-js, full-js-broken, full-js-del (~5 min)
 *   node dist/test/full.js run       # sandboxes on full-js (~4 min)
 *   node dist/test/full.js all       # offline + build + run
 *
 * How completeness is enforced: tools/coverage.ts generates the README's support table from
 * the installed e2b package. Every check() names the table rows it covers, and `all` fails if
 * any row is not covered. The checks test what the table SAYS:
 *   yes      the method works like E2B's
 *   partial  it works, and the difference written in the Notes column is real
 *   no       calling it throws NotSupportedError
 *
 * Cross-language checks (JS <-> Python sharing templates and sandboxes) run when a Python
 * >= 3.10 with requirements.txt installed is found ($E2B_AGENTCORE_PYTHON, default python3).
 */
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { randomBytes } from 'node:crypto'
import { unzipSync } from 'fflate'
import { BatchGetBuildsCommand } from '@aws-sdk/client-codebuild'
import { GetObjectCommand } from '@aws-sdk/client-s3'
import { BedrockAgentCoreControlClient } from '@aws-sdk/client-bedrock-agentcore-control'
import { BedrockAgentCoreClient, StopRuntimeSessionCommand } from '@aws-sdk/client-bedrock-agentcore'
import * as e2b from 'e2b'
import {
  Sandbox, Template, Config, NotSupportedError, CommandHandle, Commands, Filesystem, FileType,
  CommandExitError, TimeoutError, FileNotFoundError, NotFoundError, SandboxNotFoundError, TemplateError,
  BuildError, AuthenticationError, LogEntry, waitForPort, renderDockerfile, harnessName,
} from '../src/index.js'
import { zipContext, mirrorImage } from '../src/template.js'
import { coverageRows } from '../tools/coverage.js'

// dist/test/full.js -> repo root (fixtures are shared with the Python tests)
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..')
const FIX = path.join(ROOT, 'tests', 'fixtures', 'full')
const FULL = 'full-js', BROKEN = 'full-js-broken'
// Unique per run: a deleted Harness stays DELETING for minutes and keeps its name taken.
const DEL = `full-js-del-${Math.floor(Date.now() / 1000) % 100000}`
const DSL_ROW = 'Template().fromImage / aptInstall / copy / runCmd / set* ...'
const ROWS = new Map(coverageRows().map(([n, s, why]) => [n, { s, why }]))

const covered = new Set<string>()
let passed = 0, failed = 0
function check(name: string, cond: boolean, detail = '', covers: string[] = []) {
  for (const c of covers) {
    if (!ROWS.has(c)) throw new Error(`check '${name}' claims to cover unknown table row '${c}'`)
    covered.add(c)
  }
  cond ? passed++ : failed++
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}  ${detail}`.trimEnd())
}
/** The thrown error, or an Error saying it did not throw. */
async function raises(fn: () => unknown): Promise<any> {
  try { await fn() } catch (err) { return err }
  return new Error('did not throw')
}
async function warningsOf(fn: () => Promise<unknown>): Promise<string[]> {
  const got: string[] = []
  const on = (w: Error) => got.push(w.message)
  process.on('warning', on)
  try { await fn() } finally {
    await new Promise((r) => setImmediate(r)) // warnings are emitted on the next tick
    process.off('warning', on)
  }
  return got
}
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** Uses every kind of E2B builder step, so the DSL row is tested end to end. */
function fullTemplate() {
  return Template({ fileContextPath: FIX, fileIgnorePatterns: ['app/*.tmp'] })
    .fromUbuntuImage('24.04')
    .setEnvs({ APP_MODE: 'demo mode', QUOTED: 'say "hi" $x', PIP_BREAK_SYSTEM_PACKAGES: '1' })
    .aptInstall(['python3', 'python3-pip', 'git', 'curl', 'iproute2']) // iproute2: ss, for waitForPort
    .pipInstall(['six'])
    .copy('app', '/opt/app') //                                 dir; .dockerignore drops *.env
    .copy('run.sh', '/usr/local/bin/run-ok', { mode: 0o755 })
    .copy('app/keep.txt', '/opt/owned.txt', { user: 'nobody' })
    .makeDir('/opt/nb', { mode: 0o777 })
    .runCmd('whoami > /opt/nb/whoami', { user: 'nobody' })
    .makeSymlink('/opt/app/keep.txt', '/opt/keep-link')
    .runCmd('echo moved > /opt/before')
    .rename('/opt/before', '/opt/after')
    .runCmd('touch /opt/gone')
    .remove('/opt/gone')
    .setWorkdir('/work')
    .setStartCmd('python3 -m http.server 8000', waitForPort(8000))
}

// =============================================================================== offline

async function offline() {
  console.log('\n== offline')
  const missing = [...ROWS].filter(([, r]) => r.s === 'missing').map(([n]) => n)
  check('coverage table: no e2b method missing', missing.length === 0, missing.join(', '))

  const df = renderDockerfile(fullTemplate())
  check('render: ENV values quoted, $ escaped', df.includes('QUOTED="say \\"hi\\" \\$x"'), '', [DSL_ROW])
  check('render: COPY --chmod / --chown', df.includes('COPY --chmod=0755 run.sh /usr/local/bin/run-ok')
    && df.includes('COPY --chown=nobody app/keep.txt /opt/owned.txt'))
  check('render: runCmd({ user }) -> USER around RUN', df.includes('USER nobody\nRUN whoami > /opt/nb/whoami\nUSER root'))
  check('render: start cmd not baked in', !df.includes('ENTRYPOINT') && df.trimEnd().endsWith('USER root'))
  check('render: includeStartCmd -> ENTRYPOINT',
    renderDockerfile(fullTemplate(), true).trimEnd().endsWith('ENTRYPOINT python3 -m http.server 8000'))
  const names = Object.keys(unzipSync(zipContext(fullTemplate(), df))).sort()
  check('zip: COPY sources only; .dockerignore and fileIgnorePatterns applied',
    names.join() === 'Dockerfile.e2b,app/keep.txt,app/sub/nested.txt,run.sh', names.join())
  check('Template(): default fileContextPath = directory of the calling file',
    String((Template() as any).fileContextPath) === path.dirname(fileURLToPath(import.meta.url)),
    String((Template() as any).fileContextPath))
  check('harness name mapping', harnessName('a-b.c') === 'e2b_a_b_c')
  const mir = 'public.ecr.aws/docker/library'
  check('mirror: official images -> ECR Public; others unchanged',
    ['ubuntu:24.04', 'library/python:3.12', 'user/repo:1', 'ghcr.io/a/b', 'localhost:5000/x'].map((i) => mirrorImage(i, mir)).join()
      === [`${mir}/ubuntu:24.04`, `${mir}/python:3.12`, 'user/repo:1', 'ghcr.io/a/b', 'localhost:5000/x'].join()
      && mirrorImage('ubuntu:24.04', '') === 'ubuntu:24.04')
  check('render: FROM uses the configured mirror', renderDockerfile(fullTemplate(), false, mir).startsWith(`FROM ${mir}/ubuntu:24.04\n`)
    && new Config().dockerhubMirror === mir)
  check('name too long -> TemplateError', (await raises(() => harnessName('x'.repeat(37)))) instanceof TemplateError)
  check('fromTemplate() base -> TemplateError',
    (await raises(() => Template.build(Template().fromTemplate('base'), 'x'))) instanceof TemplateError)
  check('build without exec role -> TemplateError', (await raises(() =>
    Template.build(fullTemplate(), 'x', { config: new Config({ execRoleArn: '' }) }))) instanceof TemplateError)

  // Every "no" row: calling it throws NotSupportedError.
  const sbx = new Sandbox(new Config(), { sandbox_id: 'sbx-offline', template: 't', harness_arn: 'arn', metadata: {},
    started_at: 0, end_at: 0 })
  const objs: Record<string, any> = {
    'static Sandbox': Sandbox, Sandbox: sbx, Commands: sbx.commands, Filesystem: sbx.files,
    CommandHandle: Object.create(CommandHandle.prototype), 'static Template': Template,
  }
  for (const [name, r] of ROWS) {
    if (r.s !== 'no') continue
    const i = name.lastIndexOf('.')
    const e = await raises(() => objs[name.slice(0, i)][name.slice(i + 1)]('x'))
    check(`${name} -> NotSupportedError`, e instanceof NotSupportedError, '', [name])
  }
  check('commands.run({ stdin: true }) -> NotSupportedError',
    (await raises(() => sbx.commands.run('cat', { stdin: true }))) instanceof NotSupportedError, '', ['Commands.run'])
  check('supportsStdinClose = false', sbx.commands.supportsStdinClose === false, '', ['Commands.supportsStdinClose'])
  check('class constants match e2b', Sandbox.defaultTemplate === (e2b.Sandbox as any).defaultTemplate
    && Sandbox.defaultMcpTemplate === (e2b.Sandbox as any).defaultMcpTemplate, '',
  ['static Sandbox.defaultTemplate', 'static Sandbox.defaultMcpTemplate'])
  check('sandboxDomain undefined', sbx.sandboxDomain === undefined, '', ['Sandbox.sandboxDomain'])
  check('error classes are e2b\'s own', CommandExitError === e2b.CommandExitError && FileNotFoundError === e2b.FileNotFoundError)

  // AWS errors become e2b errors.
  const bad = new Config()
  ;(bad as any)._clients.set('ctl', new BedrockAgentCoreControlClient({ region: bad.region,
    credentials: { accessKeyId: 'AKIAIOSFODNN7EXAMPLE', secretAccessKey: 'x'.repeat(40) } }))
  const ae = await raises(() => Template.exists('anything', { config: bad }))
  check('bad credentials -> AuthenticationError', ae instanceof AuthenticationError, String(ae?.message).slice(0, 60))
  check('create(unknown template) -> NotFoundError', (await raises(() => Sandbox.create('no-such-template-xyz'))) instanceof NotFoundError)
  check('connect(unknown id) -> SandboxNotFoundError',
    (await raises(() => Sandbox.connect('sbx' + '0'.repeat(32)))) instanceof SandboxNotFoundError)
  check('Sandbox.kill(unknown id) -> false', (await Sandbox.kill('sbx' + '0'.repeat(32))) === false)
  check('getBuildStatus(unknown build) -> TemplateError',
    (await raises(() => Template.getBuildStatus({ templateId: 'x', buildId: 'nope:123' }))) instanceof TemplateError)
  check('exists(unknown) -> false', (await Template.exists('no-such-template-xyz')) === false)
}

// =============================================================================== build

async function waitStatus(info: e2b.BuildInfo, want: string[], limitS = 900) {
  let offset = 0
  const logs: LogEntry[] = []
  const t0 = Date.now()
  while (Date.now() - t0 < limitS * 1000) {
    const st = await Template.getBuildStatus(info, { logsOffset: offset })
    logs.push(...st.logEntries)
    offset += st.logEntries.length
    if (want.includes(st.status)) return { st, logs }
    await sleep(10_000)
  }
  throw new Error(`build ${info.buildId} not ${want} after ${limitS}s`)
}

async function build() {
  console.log('\n== build')
  const cfg = new Config()
  const broken = Template().fromUbuntuImage('24.04').runCmd('echo about-to-fail; exit 7')
  const bgBroken = await Template.buildInBackground(broken, BROKEN)
  const tiny = Template().fromUbuntuImage('24.04')
  const bgDel = await Template.buildInBackground(tiny, DEL)
  check('buildInBackground -> BuildInfo', bgBroken.templateId === 'e2b_full_js_broken' && bgBroken.alias === BROKEN, '',
    ['static Template.buildInBackground'])

  const how = (await Template.exists(FULL)) ? 'update' : 'create'
  const logs: LogEntry[] = []
  const t0 = Date.now()
  const info = await Template.build(fullTemplate(), FULL, {
    skipCache: true, cpuCount: 8, memoryMB: 16384, minFreeDiskMb: 1, tags: ['v1'], onBuildLogs: (e: LogEntry) => { logs.push(e) },
  })
  check(`build (${how}Harness) -> BuildInfo`, info.alias === FULL && info.tags.join() === 'v1',
    `${Math.round((Date.now() - t0) / 1000)}s`, ['static Template.build'])
  check('build streams LogEntry lines (start/end markers included)', logs.length > 20
    && logs[0] instanceof e2b.LogEntryStart && logs.at(-1) instanceof e2b.LogEntryEnd
    && logs.some((x) => x.message.includes('READY')), `${logs.length} lines`)
  const b = (await cfg.codebuild.send(new BatchGetBuildsCommand({ ids: [info.buildId] }))).builds![0]
  const env = Object.fromEntries((b.environment?.environmentVariables ?? []).map((v) => [v.name, v.value]))
  check('skipCache -> docker build --no-cache', env.NO_CACHE === '--no-cache')
  check('exists / aliasExists', (await Template.exists(FULL)) && (await Template.aliasExists(FULL)), '',
    ['static Template.exists', 'static Template.aliasExists'])

  const { st, logs: blogs } = await waitStatus(bgBroken, ['error'])
  check('failed build -> getBuildStatus error + reason + logs', (st.reason?.message ?? '').includes('FAILED')
    && blogs.some((x) => x.message.includes('about-to-fail')), st.reason?.message ?? '', ['static Template.getBuildStatus'])
  const be = await raises(() => Template.build(broken, BROKEN))
  check('failed build -> Template.build throws BuildError', be instanceof BuildError, String(be?.message).slice(0, 60),
    ['static Template.build'])
  check('failed build leaves no template', !(await Template.exists(BROKEN)))

  await waitStatus(bgDel, ['ready'])
  const t2 = Date.now()
  await Template.build(tiny.runCmd('echo v2 > /v2'), DEL)
  check('rebuild an existing template (UpdateHarness)', await Template.exists(DEL), `${Math.round((Date.now() - t2) / 1000)}s`,
    ['static Template.build'])
  check('Template.delete -> true', (await Template.delete(DEL)) === true)
  check('exists -> false right after delete (Harness still DELETING)', !(await Template.exists(DEL)))
  check('Template.delete(again, still DELETING) -> false', (await Template.delete(DEL)) === false)
}

// =============================================================================== run

async function run() {
  console.log('\n== run')
  const cfg = new Config()
  const ignored = { timeoutMs: 60_000, network: {}, lifecycle: {}, allowInternetAccess: false, mcp: {}, volumeMounts: {} }
  let sbx!: Sandbox
  const t0 = Date.now()
  const w = await warningsOf(async () => {
    sbx = await Sandbox.create(FULL, { envs: { GREETING: 'hi there', SECRET_TOKEN: 's3cr3t' }, metadata: { suite: 'full-js' },
      ...ignored })
  })
  check('create + start cmd ready', sbx.sandboxId.startsWith('sbx'), `${((Date.now() - t0) / 1000).toFixed(1)}s`,
    ['static Sandbox.create', 'Sandbox.sandboxId'])
  check('create: 6 per-sandbox options warn, no effect', w.length === 6 && w.every((x) => x.includes('no per-sandbox effect')),
    `${w.length} warnings`, ['static Sandbox.create'])
  const raw = await (await cfg.s3.send(new GetObjectCommand({ Bucket: await cfg.bucket(),
    Key: `sandboxes/${sbx.sandboxId}.json` }))).Body!.transformToString()
  check('envs are not written to S3', !raw.includes('s3cr3t') && !raw.includes('SECRET_TOKEN'))
  let sbx2: Sandbox | undefined
  try {
    const c = sbx.commands, f = sbx.files
    check('commands / files objects', c instanceof Commands && f instanceof Filesystem, '', ['Sandbox.commands', 'Sandbox.files'])

    // ---- template DSL steps, as seen inside the sandbox
    const out = (await c.run("python3 -c 'import six; print(six.__name__)'; echo \"$QUOTED\"; echo $APP_MODE; run-ok; " +
      'cat /opt/nb/whoami; stat -c %U /opt/owned.txt; readlink /opt/keep-link; cat /opt/after; ' +
      'test -e /opt/gone || echo gone-ok; ls /opt/app /opt/app/sub; pwd')).stdout.split('\n')
    const expect = ['six', 'say "hi" $x', 'demo mode', 'run-ok', 'nobody', 'nobody', '/opt/app/keep.txt', 'moved', 'gone-ok']
    check('DSL: pip/env/copy mode+chown/runCmd user/symlink/rename/remove', out.slice(0, 9).join('|') === expect.join('|'),
      JSON.stringify(out.slice(0, 9)), [DSL_ROW])
    check('DSL: copy dir honours .dockerignore + ignore patterns, workdir', out.includes('keep.txt')
      && !out.includes('secret.env') && !out.includes('skip.tmp') && out.includes('nested.txt') && out.at(-2) === '/work',
    JSON.stringify(out.slice(9)))
    check('start cmd serving :8000', (await c.run("curl -s -o /dev/null -w '%{http_code}' localhost:8000/")).stdout === '200')

    // ---- commands.run
    const r = await c.run('uname -m; whoami; echo $GREETING')
    check('run: arm64, root by default, sandbox envs', r.stdout === 'aarch64\nroot\nhi there\n' && r.exitCode === 0,
      JSON.stringify(r.stdout), ['Commands.run'])
    check('run: per-command envs + cwd', (await c.run('echo $X; pwd', { envs: { X: '1' }, cwd: '/tmp' })).stdout === '1\n/tmp\n')
    check('run: user', (await c.run('whoami', { user: 'nobody' })).stdout === 'nobody\n')
    const got: string[] = []
    await c.run('echo a; echo b >&2', { onStdout: (d) => { got.push(d) }, onStderr: (d) => { got.push(d) } })
    check('run: onStdout / onStderr', ['ab', 'ba'].includes(got.join('').replace(/\n/g, '')))
    let e = await raises(() => c.run('echo oops >&2; exit 3'))
    check('run: non-zero -> CommandExitError', e instanceof CommandExitError && e.exitCode === 3 && e.stderr.includes('oops'))
    check('run: timeout -> TimeoutError', (await raises(() => c.run('sleep 10', { timeoutMs: 2000 }))) instanceof TimeoutError)
    check('run: timeoutMs 0 means no limit', (await c.run('sleep 2; echo ok', { timeoutMs: 0 })).stdout === 'ok\n')
    const big = (await c.run("python3 -c \"print('a' * 70000)\"")).stdout
    check('run: one output line > 64 KB is dropped (documented AgentCore limit)', !big.includes('a'.repeat(70000)),
      `got ${big.length} chars`, ['Commands.run'])
    check('run: same output wrapped arrives whole',
      (await c.run("python3 -c \"print('a' * 70000)\" | fold -w 1000")).stdout.replace(/\n/g, '') === 'a'.repeat(70000))

    // ---- background commands
    const chunks: string[] = []
    const h = await c.run('for i in 1 2 3; do echo t$i; sleep 1.5; done', { background: true, onStdout: (d) => { chunks.push(d) } })
    check('background -> CommandHandle with pid', h instanceof CommandHandle && h.pid > 0, `pid=${h.pid}`, ['CommandHandle.pid'])
    check('commands.list sees it', (await c.list()).some((p) => p.pid === h.pid && p.args.at(-1)!.includes('t$i')), '',
      ['Commands.list'])
    await sleep(2500)
    check('onStdout fires without wait()', chunks.length >= 1 && h.exitCode === undefined, `${chunks.length} chunk(s) so far`)
    const res = await h.wait()
    check('background output arrives in chunks; stdout/exitCode/error', chunks.length >= 2 && chunks.join('') === 't1\nt2\nt3\n'
      && h.stdout === res.stdout && h.stderr === '' && h.exitCode === 0 && h.error === undefined, `${chunks.length} chunks`,
    ['Commands.run', 'CommandHandle.wait', 'CommandHandle.stdout', 'CommandHandle.stderr', 'CommandHandle.exitCode',
      'CommandHandle.error'])
    const hd = await c.run('sleep 3; echo late', { background: true })
    await hd.disconnect()
    check('wait() after disconnect -> SandboxError', (await raises(() => hd.wait())) instanceof e2b.SandboxError, '',
      ['CommandHandle.disconnect'])
    const got2: string[] = []
    const res2 = await (await c.connect(hd.pid, { onStdout: (d) => { got2.push(d) } })).wait()
    check('commands.connect(pid) after disconnect', res2.stdout === 'late\n' && got2.join('') === 'late\n', '',
      ['Commands.connect', 'CommandHandle.disconnect'])
    const h4 = await c.run('exit 4', { background: true })
    e = await raises(() => h4.wait())
    check('handle.wait on non-zero -> CommandExitError', e instanceof CommandExitError && e.exitCode === 4
      && h4.exitCode === 4 && h4.error === 'process exited')
    const hk = await c.run('sleep 300', { background: true })
    check('handle.kill', (await hk.kill()) === true && !(await c.list()).some((p) => p.pid === hk.pid), '', ['CommandHandle.kill'])
    const hk2 = await c.run('sleep 300', { background: true })
    check('commands.kill', (await c.kill(hk2.pid)) === true && !(await c.list()).some((p) => p.pid === hk2.pid), '',
      ['Commands.kill'])
    check('commands.kill(unknown) -> false', (await c.kill(999999)) === false)
    await c.run('nohup sleep 120 > /dev/null 2>&1 &')
    check('commands.list: only SDK-started commands', !(await c.list()).some((p) => p.args.join(' ').includes('sleep 120')),
      '', ['Commands.list'])
    check('commands.connect(unknown) -> NotFoundError', (await raises(() => c.connect(999999))) instanceof NotFoundError)

    // ---- files
    const wi = await f.write('/work/sub/a.txt', 'hello\n')
    check('write string -> WriteInfo', wi.type === FileType.FILE && wi.path === '/work/sub/a.txt' && wi.name === 'a.txt', '',
      ['Filesystem.write'])
    check('read text', (await f.read('/work/sub/a.txt')) === 'hello\n', '', ['Filesystem.read'])
    const blob = randomBytes(200_000)
    const tw = Date.now()
    await f.write('/work/blob.bin', blob)
    const kbs = 200 / ((Date.now() - tw) / 1000)
    check('write 200 KB at >= 30 KB/s (doc: ~100 KB/s)', kbs >= 30, `${kbs.toFixed(0)} KB/s`, ['Filesystem.write'])
    check('read bytes -> Uint8Array', Buffer.from(await f.read('/work/blob.bin', { format: 'bytes' })).equals(blob))
    check('read blob', Buffer.from(await (await f.read('/work/blob.bin', { format: 'blob' })).arrayBuffer()).equals(blob))
    const reader = (await f.read('/work/blob.bin', { format: 'stream' })).getReader()
    const parts: Uint8Array[] = []
    for (let x = await reader.read(); !x.done; x = await reader.read()) parts.push(x.value)
    check('read stream -> one chunk', parts.length === 1 && Buffer.from(parts[0]).equals(blob), '', ['Filesystem.read'])
    await f.write('/work/ab.txt', new TextEncoder().encode('from bytes').buffer as ArrayBuffer)
    await f.write('/work/blob.txt', new Blob(['from blob']))
    await f.write('/work/rs.txt', new ReadableStream({ start(ctl) { ctl.enqueue(new TextEncoder().encode('from stream')); ctl.close() } }),
      { gzip: true, useOctetStream: true })
    check('write ArrayBuffer / Blob / ReadableStream; gzip/useOctetStream ignored', (await f.read('/work/ab.txt')) === 'from bytes'
      && (await f.read('/work/blob.txt')) === 'from blob' && (await f.read('/work/rs.txt')) === 'from stream', '', ['Filesystem.write'])
    const ws = await f.write([{ path: '/work/m/1.txt', data: '1' }, { path: '/work/m/2.txt', data: new TextEncoder().encode('2') }])
    const ws2 = await f.writeFiles([{ path: '/work/m/3.txt', data: '3' }])
    check('write([...]) and writeFiles', ws.map((x) => x.name).join() === '1.txt,2.txt' && ws2[0].name === '3.txt', '',
      ['Filesystem.writeFiles', 'Filesystem.write'])
    const ls = Object.fromEntries((await f.list('/opt')).map((x) => [x.name, x]))
    check('list -> EntryInfo (type/size/mode/perms/owner/group/mtime/symlink)', ls.app.type === FileType.DIR
      && ls['keep-link'].type === FileType.SYMLINK && ls['keep-link'].symlinkTarget === '/opt/app/keep.txt'
      && ls['owned.txt'].owner === 'nobody' && ls.after.size === 6 && ls.after.permissions === '-rw-r--r--'
      && ls.after.mode === 0o644 && ls.after.group === 'root' && ls.after.modifiedTime instanceof Date, '', ['Filesystem.list'])
    check('list depth 1 vs 2', !(await f.list('/opt/app')).some((x) => x.path === '/opt/app/sub/nested.txt')
      && (await f.list('/opt/app', { depth: 2 })).some((x) => x.path === '/opt/app/sub/nested.txt'))
    const gi = await f.getInfo('/usr/local/bin/run-ok')
    check('getInfo: COPY mode 0755', gi.permissions === '-rwxr-xr-x', gi.permissions, ['Filesystem.getInfo'])
    check('exists (file / symlink / missing)', (await f.exists('/work/m/1.txt')) && (await f.exists('/opt/keep-link'))
      && !(await f.exists('/work/nope')), '', ['Filesystem.exists'])
    check('makeDir new -> true, existing -> false', (await f.makeDir('/work/d/e')) === true && (await f.makeDir('/work/d/e')) === false,
      '', ['Filesystem.makeDir'])
    const mv = await f.rename('/work/sub/a.txt', '/work/b.txt')
    check('rename -> EntryInfo of new path', mv.path === '/work/b.txt' && !(await f.exists('/work/sub/a.txt')), '', ['Filesystem.rename'])
    await f.remove('/work/b.txt')
    await f.remove('/work/d')
    check('remove file and dir', !(await f.exists('/work/b.txt')) && !(await f.exists('/work/d')), '', ['Filesystem.remove'])
    for (const [name, fn] of [['read', () => f.read('/work/b.txt')], ['remove', () => f.remove('/work/b.txt')],
      ['list', () => f.list('/work/nope')], ['getInfo', () => f.getInfo('/work/nope')]] as [string, () => unknown][])
      check(`${name}(missing) -> FileNotFoundError`, (await raises(fn)) instanceof FileNotFoundError)
    await f.write('/work/nb.txt', 'mine', { user: 'nobody' })
    check('write({ user }) sets owner', (await f.getInfo('/work/nb.txt')).owner === 'nobody')
    await f.write('/root/private.txt', 'root only')
    await c.run('chmod 600 /root/private.txt')
    check('read({ user }) uses that user\'s permissions',
      (await raises(() => f.read('/root/private.txt', { user: 'nobody' }))) instanceof CommandExitError)

    // ---- git (official e2b helper over our commands)
    const g = sbx.git
    await g.configureUser('Test', 'test@example.com')
    await g.init('/work/repo', { initialBranch: 'main' })
    await f.write('/work/repo/x.txt', 'x')
    await g.add('/work/repo')
    await g.commit('/work/repo', 'first')
    const gs = await g.status('/work/repo')
    check('git: configureUser/init/add/commit/status', gs.isClean && gs.currentBranch === 'main', '', ['Sandbox.git'])

    // ---- info, metrics, timeout
    const info = await sbx.getInfo()
    check('getInfo -> SandboxInfo; cpuCount 8 at build ignored', info.metadata.suite === 'full-js' && info.templateId === FULL
      && info.state === 'running' && info.cpuCount === 2 && info.startedAt instanceof Date,
    `cpu=${info.cpuCount} mem=${info.memoryMB}`, ['Sandbox.getInfo', 'static Template.build'])
    check('Sandbox.getInfo(id) / getFullInfo(id)', (await Sandbox.getInfo(sbx.sandboxId)).sandboxId === sbx.sandboxId
      && (await Sandbox.getFullInfo(sbx.sandboxId)).sandboxId === sbx.sandboxId, '',
    ['static Sandbox.getInfo', 'static Sandbox.getFullInfo'])
    const m = await sbx.getMetrics()
    check('getMetrics -> one live sample', m.length === 1 && m[0].memTotal > m[0].memUsed && m[0].memUsed > 0
      && m[0].diskTotal > 0 && m[0].timestamp instanceof Date, '', ['Sandbox.getMetrics'])
    check('Sandbox.getMetrics(id)', (await Sandbox.getMetrics(sbx.sandboxId)).length === 1, '', ['static Sandbox.getMetrics'])
    const endBefore = info.endAt.getTime()
    const w1 = await warningsOf(() => sbx.setTimeout(600_000))
    const w2 = await warningsOf(() => Sandbox.setTimeout(sbx.sandboxId, 600_000))
    check('setTimeout warns and has no effect', w1.length === 1 && w2.length === 1
      && (await Sandbox.getInfo(sbx.sandboxId)).endAt.getTime() === endBefore, '',
    ['Sandbox.setTimeout', 'static Sandbox.setTimeout'])

    // ---- list, isRunning, connect
    const tMid = new Date()
    sbx2 = await Sandbox.create({ template: FULL, metadata: { suite: 'full-js' } })
    const query = { metadata: { suite: 'full-js' } }
    const p = Sandbox.list({ query, limit: 1 })
    const pages = [...await p.nextItems(), ...await p.nextItems()].map((i) => i.sandboxId)
    check('list({ limit: 1 }) pages, newest first', pages.join() === [sbx2.sandboxId, sbx.sandboxId].join() && p.hasNext === false
      && p.nextToken === undefined, '', ['static Sandbox.list'])
    const ids = async (q: object, order?: 'asc') => (await Sandbox.list({ query: q, order }).nextItems()).map((i) => i.sandboxId)
    check('list({ order: asc })', (await ids(query, 'asc')).join() === [sbx.sandboxId, sbx2.sandboxId].join())
    check('list(query.startedAfter)', (await ids({ ...query, startedAfter: tMid })).join() === sbx2.sandboxId)
    check('list(query.template)', (await ids({ template: FULL })).includes(sbx.sandboxId) && !(await ids({ template: 'nope' })).includes(sbx.sandboxId))
    check('list(query.state paused) -> none', !(await ids({ state: ['paused'] })).includes(sbx.sandboxId))
    check('isRunning', (await sbx.isRunning()) === true, '', ['Sandbox.isRunning'])
    const again = await Sandbox.connect(sbx.sandboxId)
    check('Sandbox.connect(id) keeps files + envs', (await again.files.read('/work/m/1.txt')) === '1'
      && (await again.commands.run('echo $GREETING')).stdout === 'hi there\n', '', ['static Sandbox.connect'])
    check('sbx.connect()', (await sbx.connect()) === sbx, '', ['Sandbox.connect'])

    // A session reaped by AgentCore (simulated: stopped behind the SDK's back).
    const rec = (await cfg.getJson<{ harness_arn: string }>(`sandboxes/${sbx2.sandboxId}.json`))!
    await new BedrockAgentCoreClient({ region: cfg.region }).send(new StopRuntimeSessionCommand({
      agentRuntimeArn: rec.harness_arn, runtimeSessionId: sbx2.sandboxId, qualifier: 'DEFAULT' }))
    check('reaped sandbox still listed until noticed', (await ids(query)).includes(sbx2.sandboxId), '', ['static Sandbox.list'])
    check('isRunning probes the microVM -> false', (await sbx2.isRunning()) === false, '', ['Sandbox.isRunning'])
    check('...and then drops out of list', !(await ids(query)).includes(sbx2.sandboxId))
    check('connect(reaped) -> SandboxNotFoundError', (await raises(() => Sandbox.connect(sbx2!.sandboxId))) instanceof SandboxNotFoundError)
    sbx2 = undefined

    await interop(sbx)
  } finally {
    check('Sandbox.kill(id) -> true', (await Sandbox.kill(sbx.sandboxId)) === true, '', ['static Sandbox.kill'])
    if (sbx2) await sbx2.kill()
  }
  check('Sandbox.kill(again) -> false', (await Sandbox.kill(sbx.sandboxId)) === false)
  check('connect after kill -> SandboxNotFoundError', (await raises(() => Sandbox.connect(sbx.sandboxId))) instanceof SandboxNotFoundError)
  const s3 = await Sandbox.create(FULL)
  check('sbx.kill() -> true', (await s3.kill()) === true && (await Sandbox.kill(s3.sandboxId)) === false, '', ['Sandbox.kill'])
  let id = ''
  {
    await using s4 = await Sandbox.create(FULL)
    id = s4.sandboxId
  }
  check('await using -> killed', (await Sandbox.kill(id)) === false)
}

/** JS and Python share templates and sandboxes (same S3 records and in-VM layout). */
async function interop(sbx: Sandbox) {
  const py = process.env.E2B_AGENTCORE_PYTHON ?? 'python3'
  try {
    execFileSync(py, ['-c', 'import sys, e2b, boto3; assert sys.version_info >= (3, 10)'], { stdio: 'ignore' })
  } catch {
    console.log(`SKIP  interop: ${py} lacks Python >= 3.10 with requirements.txt (set E2B_AGENTCORE_PYTHON)`)
    return
  }
  const pyRun = (...args: string[]) => JSON.parse(execFileSync(py, [path.join(ROOT, 'tests', 'interop.py'), ...args],
    { encoding: 'utf-8', timeout: 300_000 }).trim().split('\n').at(-1)!)
  await sbx.files.write('/work/interop.txt', 'from js')
  const out = pyRun('connect', sbx.sandboxId, '/work/interop.txt')
  check('Python connects to a JS sandbox (files + envs)', out.read === 'from js' && out.env === 'hi there', JSON.stringify(out))
  check('JS reads what Python wrote', (await sbx.files.read('/work/from-py.txt')) === 'hello from python')
  const made = pyRun('create', FULL)
  const other = await Sandbox.connect(made.id)
  check('JS connects to a Python sandbox on a JS-built template', (await other.files.read('/work/from-py.txt')) === 'made by python'
    && (await other.commands.run('echo $FROM')).stdout === 'py\n')
  await other.kill()
}

// =============================================================================== main

const stage = process.argv[2] ?? ''
const stages: Record<string, (() => Promise<void>)[]> = { offline: [offline], build: [build], run: [run], all: [offline, build, run] }
if (!stages[stage]) {
  console.error('usage: node dist/test/full.js offline|build|run|all')
  process.exit(2)
}
for (const fn of stages[stage]) await fn()
if (stage === 'all') for (const name of ROWS.keys()) if (!covered.has(name)) check(`table row has a test: ${name}`, false)
console.log(`\n${passed} passed, ${failed} failed; ${covered.size}/${ROWS.size} table rows covered by this stage`)
process.exit(failed ? 1 : 0)
