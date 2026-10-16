/**
 * End-to-end test of the AgentCore provider (JS/TS) against a real account.
 *
 *   export E2B_AGENTCORE_EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/<harness-exec-role>
 *   npm run build
 *   node dist/test/e2e.js offline   # Dockerfile rendering + context zip, no AWS calls
 *   node dist/test/e2e.js build     # build the two templates (about 3-5 min)
 *   node dist/test/e2e.js run       # exercise every supported method
 *
 * Template "dsl-demo-js" uses the official E2B builder DSL (aptInstall, copy, setEnvs,
 * setWorkdir, setStartCmd + waitForPort); a first build, so it goes through CreateHarness.
 * Template "ubuntu24-base" is the customer Dockerfile via fromDockerfile; it usually exists
 * already (built by the Python test), so it goes through UpdateHarness.
 */
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomBytes } from 'node:crypto'
import { unzipSync } from 'fflate'
import {
  Sandbox, Template, NotSupportedError, CommandExitError, TimeoutError, FileNotFoundError,
  SandboxNotFoundError, FileType, waitForPort, defaultBuildLogger, renderDockerfile,
} from '../src/index.js'
import { zipContext } from '../src/template.js'

// dist/test/e2e.js -> repo root (fixtures and Dockerfiles are shared with the Python tests)
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..')
const FIXTURES = path.join(ROOT, 'tests')

let ok = 0, fail = 0
function check(name: string, cond: boolean, detail = '') {
  cond ? ok++ : fail++
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}  ${detail}`.trimEnd())
}
async function raises(fn: () => unknown): Promise<unknown> {
  try { await fn() } catch (err) { return err }
  return new Error('did not raise')
}

function dslTemplate() {
  return Template({ fileContextPath: FIXTURES })
    .fromUbuntuImage('24.04')
    .aptInstall(['python3', 'git', 'iproute2', 'curl'])
    .copy('fixture.txt', '/opt/fixture.txt', { mode: 0o644 })
    .setEnvs({ APP_MODE: 'demo mode' })
    .setWorkdir('/work')
    .setStartCmd('python3 -m http.server 8000', waitForPort(8000))
}

function offline() {
  const df = renderDockerfile(dslTemplate())
  check('render: FROM/ENV/WORKDIR/COPY --chmod', df.startsWith('FROM ubuntu:24.04\n') && df.includes('ENV APP_MODE="demo mode"')
    && df.includes('COPY --chmod=0644 fixture.txt /opt/fixture.txt') && df.includes('WORKDIR /work'), JSON.stringify(df.slice(0, 60)))
  check('render: no ENTRYPOINT, ends USER root', !df.includes('ENTRYPOINT') && df.trimEnd().endsWith('USER root'))
  const zip = unzipSync(zipContext(dslTemplate(), df))
  check('zip: Dockerfile.e2b + COPY source only', Object.keys(zip).sort().join(',') === 'Dockerfile.e2b,fixture.txt')
  const cust = Template().fromDockerfile(path.join(ROOT, 'Dockerfile.fixed'))
  const cdf = renderDockerfile(cust)
  check('fromDockerfile: customer RUN steps kept', cdf.includes('mirrors.aliyun.com') && cdf.includes('ports.ubuntu.com'))
  check('fromDockerfile: TZ env kept', /ENV .*TZ=Asia\/Shanghai/.test(cdf))
}

async function build() {
  const t0 = Date.now()
  const info = await Template.buildInBackground(dslTemplate(), 'dsl-demo-js') // non-blocking path
  console.log('background build started:', info)
  const customer = Template().fromDockerfile(path.join(ROOT, 'Dockerfile.fixed'))
  await Template.build(customer, 'ubuntu24-base', { onBuildLogs: defaultBuildLogger() }) // blocking path
  console.log(`ubuntu24-base built in ${Math.round((Date.now() - t0) / 1000)}s`)
  for (;;) {
    const st = await Template.getBuildStatus(info)
    if (st.status === 'ready' || st.status === 'error') { console.log('dsl-demo-js:', st.status, st.reason ?? ''); break }
    await new Promise((r) => setTimeout(r, 10000))
  }
  check('Template.exists', (await Template.exists('dsl-demo-js')) && (await Template.aliasExists('ubuntu24-base')))
}

async function run() {
  let t = Date.now()
  const sbx = await Sandbox.create('dsl-demo-js', { envs: { GREETING: 'hi there' }, metadata: { suite: 'e2e-js' } })
  check('create + startCmd ready', true, `${((Date.now() - t) / 1000).toFixed(1)}s id=${sbx.sandboxId}`)
  try {
    const c = sbx.commands
    const r = await c.run('uname -m; pwd; echo $GREETING; echo $APP_MODE')
    check('run: arch/cwd/envs', r.stdout.split('\n').slice(0, 4).join('|') === 'aarch64|/work|hi there|demo mode', JSON.stringify(r.stdout))
    check('run: per-command envs+cwd', (await c.run('echo $X; pwd', { envs: { X: '1' }, cwd: '/tmp' })).stdout === '1\n/tmp\n')
    const streamed: string[] = []
    await c.run('echo a; echo b >&2', { onStdout: (d) => { streamed.push(d) }, onStderr: (d) => { streamed.push(d) } })
    check('run: onStdout/onStderr', ['ab', 'ba'].includes(streamed.join('').replace(/\n/g, '')))
    let e: any = await raises(() => c.run('echo oops >&2; exit 3'))
    check('run: non-zero -> CommandExitError', e instanceof CommandExitError && e.exitCode === 3 && e.stderr.includes('oops'))
    e = await raises(() => c.run('sleep 10', { timeoutMs: 2000 }))
    check('run: timeout -> TimeoutError', e instanceof TimeoutError, String(e?.message).slice(0, 80))
    check('startCmd serving :8000', (await c.run("curl -s -o /dev/null -w '%{http_code}' localhost:8000/")).stdout === '200')

    const h = await c.run('for i in 1 2 3; do echo tick $i; sleep 1; done; exit 0', { background: true })
    check('background: pid', h.pid > 0, `pid=${h.pid}`)
    check('commands.list sees it', (await c.list()).some((p) => p.pid === h.pid))
    const got: string[] = []
    const res = await (await c.connect(h.pid, { onStdout: (d) => { got.push(d) } })).wait()
    check('connect(pid).wait streams', res.exitCode === 0 && res.stdout.includes('tick 3') && got.join('') === res.stdout)
    check('original handle also completes', (await h.wait()).stdout === res.stdout)
    const h2 = await c.run('sleep 300', { background: true })
    check('commands.kill', (await c.kill(h2.pid)) && !(await c.list()).some((p) => p.pid === h2.pid))
    e = await raises(() => h2.wait())
    check('killed handle.wait -> CommandExitError', e instanceof CommandExitError)
    check('kill unknown pid -> false', (await c.kill(999999)) === false)

    const f = sbx.files
    const w = await f.write('/work/sub/a.txt', 'hello\n')
    check('files.write -> WriteInfo', w.type === FileType.FILE && w.name === 'a.txt')
    check('files.read text', (await f.read('/work/sub/a.txt')) === 'hello\n')
    const blob = randomBytes(200_000)
    t = Date.now()
    await f.write('/work/blob.bin', blob)
    const tw = (Date.now() - t) / 1000
    check('files.write/read 200 KB bytes', Buffer.from(await f.read('/work/blob.bin', { format: 'bytes' })).equals(blob), `write ${tw.toFixed(1)}s`)
    check('files.read blob', (await (await f.read('/work/sub/a.txt', { format: 'blob' })).text()) === 'hello\n')
    await f.write([{ path: '/work/m/1.txt', data: '1' }, { path: '/work/m/2.txt', data: new TextEncoder().encode('2') }])
    const ls = await f.list('/work/m')
    check('write([...]) + list -> EntryInfo', ls.map((x) => x.name).sort().join() === '1.txt,2.txt' && ls.every((x) => x.size === 1))
    const gi = await f.getInfo('/opt/fixture.txt')
    check('getInfo: COPY mode/owner', gi.permissions === '-rw-r--r--' && gi.owner === 'root', `${gi.permissions} ${gi.mode.toString(8)}`)
    check('list depth 2 includes nested', (await f.list('/work', { depth: 2 })).some((x) => x.path === '/work/sub/a.txt'))
    check('makeDir new/existing', (await f.makeDir('/work/d')) === true && (await f.makeDir('/work/d')) === false)
    check('exists', (await f.exists('/work/d')) && !(await f.exists('/work/nope')))
    check('rename -> EntryInfo', (await f.rename('/work/sub/a.txt', '/work/b.txt')).name === 'b.txt')
    await f.remove('/work/b.txt')
    check('read missing -> FileNotFoundError', (await raises(() => f.read('/work/b.txt'))) instanceof FileNotFoundError)

    await sbx.git.init('/work/repo')
    check('sbx.git (official helper)', (await c.run('git -C /work/repo rev-parse --is-inside-work-tree')).stdout.trim() === 'true')

    const info = await sbx.getInfo()
    check('getInfo -> SandboxInfo', info.metadata.suite === 'e2e-js' && info.cpuCount > 0 && info.state === 'running',
      `cpu=${info.cpuCount} mem=${info.memoryMB}MB`)
    const m = (await sbx.getMetrics())[0]
    check('getMetrics', m.memTotal > 0 && m.diskTotal > 0, `cpu%=${m.cpuUsedPct} mem=${m.memUsed >> 20}MB`)
    const p = Sandbox.list({ query: { metadata: { suite: 'e2e-js' } } })
    const ids = (await p.nextItems()).map((i) => i.sandboxId)
    check('Sandbox.list(query) paginator', ids.includes(sbx.sandboxId) && p.hasNext === false)
    check('isRunning', await sbx.isRunning())

    const again = await Sandbox.connect(sbx.sandboxId)
    check('Sandbox.connect keeps state + envs', (await again.files.read('/work/m/1.txt')) === '1'
      && (await again.commands.run('echo $GREETING')).stdout === 'hi there\n')
    const warnings: string[] = []
    const onWarn = (w: Error) => warnings.push(w.message)
    process.on('warning', onWarn)
    await sbx.setTimeout(600_000)
    await new Promise((r) => setImmediate(r))
    process.off('warning', onWarn)
    check('setTimeout warns', warnings.length === 1)
    for (const [name, fn] of [['pause', () => sbx.pause()], ['getHost', () => sbx.getHost(8000)],
      ['watchDir', () => f.watchDir('/work', () => {})], ['pty', () => sbx.pty]] as [string, () => unknown][]) {
      check(`${name} -> NotSupportedError`, (await raises(fn)) instanceof NotSupportedError)
    }
  } finally {
    check('Sandbox.kill(sandboxId)', (await Sandbox.kill(sbx.sandboxId)) === true)
  }
  check('connect after kill -> SandboxNotFoundError', (await raises(() => Sandbox.connect(sbx.sandboxId))) instanceof SandboxNotFoundError)

  // Customer Dockerfile template (rebuilt through UpdateHarness), and `await using` cleanup.
  let id = ''
  {
    await using s2 = await Sandbox.create('ubuntu24-base')
    id = s2.sandboxId
    const out = (await s2.commands.run('git --version; grep -h ^URIs /etc/apt/sources.list.d/ubuntu.sources; echo $TZ')).stdout
    check('customer Dockerfile sandbox', out.includes('git version') && out.includes('aliyun') && out.includes('Asia/Shanghai'), JSON.stringify(out))
  }
  check('await using -> killed', !(await Sandbox.list({ query: {} }).nextItems()).some((i) => i.sandboxId === id))
}

const mode = process.argv[2]
const steps: Record<string, () => unknown> = { offline, build, run }
if (!steps[mode]) { console.error('usage: node dist/test/e2e.js offline|build|run'); process.exit(2) }
await steps[mode]()
console.log(`\n${ok} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
