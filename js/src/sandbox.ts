/**
 * `Sandbox` for the AgentCore provider, with the same methods and return shapes as e2b's `Sandbox`.
 *
 *   import { Sandbox } from 'e2b-agentcore'
 *   const sbx = await Sandbox.create('my-sandbox', { envs: { FOO: '1' }, metadata: { user: 'alice' } })
 *   console.log((await sbx.commands.run('git --version')).stdout)
 *   await sbx.files.write('/tmp/a.txt', 'hi')
 *   const h = await sbx.commands.run('sleep 2; echo done', { background: true })
 *   console.log((await h.wait()).stdout)
 *   await sbx.kill()
 *
 * How the E2B model maps onto AgentCore
 * -------------------------------------
 * - One sandbox = one AgentCore Runtime session (one arm64 microVM) on the template's Harness.
 *   sandboxId is used directly as the runtimeSessionId.
 * - Every command and file operation is one InvokeAgentRuntimeCommand call: a shell command
 *   run as root in the microVM, with stdout/stderr streamed back and an exit code at the end.
 *   Files are moved as base64 through such commands (there is no separate file API).
 * - Inside the microVM we keep a little state under /tmp/.e2b/:
 *       id     the sandboxId, used to detect that a session was reaped (see connect)
 *       env    `export` lines for the sandbox-level envs passed to Sandbox.create
 *       p/     background processes: <tag>.sh / .cmd / .pid / .out / .err / .rc
 * - Outside, s3://<bucket>/sandboxes/<sandboxId>.json stores what AgentCore cannot: the
 *   template, E2B metadata and start time, so Sandbox.list / getInfo / connect work.
 *   Envs are NOT written to S3 (they may hold secrets); they live only in the microVM.
 * - Errors are the official e2b classes (CommandExitError, TimeoutError, FileNotFoundError,
 *   SandboxNotFoundError, ...), so instanceof checks keep working.
 * - In-VM layout and S3 records are the same as the Python package, so a sandbox created
 *   from Python can be connected to from TypeScript and the other way round.
 *
 * Methods with no AgentCore equivalent (pause, snapshots, getHost, ...) exist and throw
 * NotSupportedError with the reason, so callers fail loudly instead of silently.
 */
import { randomUUID } from 'node:crypto'
import {
  CommandExitError, FileNotFoundError, FileType, Git, InvalidArgumentError, NotFoundError,
  SandboxError, SandboxNotFoundError, TimeoutError,
  type CommandResult, type EntryInfo, type ProcessInfo, type SandboxInfo, type SandboxMetrics,
  type WriteInfo, type FilesystemEvent,
} from 'e2b'
import {
  BedrockAgentCoreClient, InvokeAgentRuntimeCommandCommand, StopRuntimeSessionCommand,
} from '@aws-sdk/client-bedrock-agentcore'
import { Config, awsCode, sleep } from './config.js'
import { awsErrors, notSupported, NotSupportedError } from './errors.js'
import { loadTemplate, type AgentCoreOpts } from './template.js'

// V2 runtimes reject an InvokeAgentRuntimeCommand timeout above 3600 s (V1 accepts any).
// _exec sends `limit + 30`, and `timeout -k 5` adds 5 s more, so one call can wait 3565 s.
export const V2_MAX_CALL_S = 3565
const END_MARK = '@E2B-AGENTCORE-END' // last line of SDK-internal replies: "<mark>:<line count>"
const B64_LINE = 3000 //                 base64 line width for reads (V1 drops lines over ~64 KB)
const READ_CHUNK = 192 * 1024 //         files.read fetches this many bytes per command
const READ_RETRIES = 5
const MAX_CMD = 65536 //        InvokeAgentRuntimeCommand limit on the command string
const STATE = '/tmp/.e2b' //    in-VM state dir (see module docstring)
const ENOENT = 44 //            exit code our scripts use for "path does not exist"
// find -printf fields -> EntryInfo: type size mode(octal) perms owner group mtime link path
const STAT_FMT = String.raw`%y\t%s\t%m\t%M\t%u\t%g\t%T@\t%l\t%p\n`

/** POSIX shell single-quoting, like Python's shlex.quote. */
export function q(s: string): string {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(s) ? s : `'${s.replace(/'/g, `'"'"'`)}'`
}
const b64 = (s: string | Uint8Array) => Buffer.from(s).toString('base64')
const exportLines = (envs?: Record<string, string>) =>
  Object.entries(envs ?? {}).map(([k, v]) => `export ${k}=${q(String(v))}\n`).join('')

function warn(msg: string) {
  process.emitWarning(msg, { code: 'E2B_AGENTCORE_IGNORED' })
}

function result(stdout: string, stderr: string, exitCode: number, error?: string): CommandResult {
  return { stdout, stderr, exitCode, ...(error === undefined ? {} : { error }) } as CommandResult
}

/** Shape of the sandbox record in S3 (snake_case, shared with the Python package). */
export interface SandboxRecord {
  sandbox_id: string
  template: string
  /** The ARN commands go to: Harness ARN (V1) or Runtime ARN (V2). Older records lack it. */
  agent_runtime_arn?: string
  /** Same value as agent_runtime_arn; the only field in records from before V2 support. */
  harness_arn: string
  platform_version?: 'V1' | 'V2'
  workdir?: string | null
  metadata: Record<string, string>
  started_at: number
  end_at: number
  cpu_count?: number
  memory_mb?: number
  start_pid?: number
}

export interface SandboxOpts extends AgentCoreOpts {
  template?: string
  metadata?: Record<string, string>
  envs?: Record<string, string>
  /** Accepted, no per-sandbox effect (idle timeout / max lifetime are per Harness). */
  timeoutMs?: number
  [e2bOpt: string]: unknown
}

export interface CommandStartOpts {
  background?: boolean
  cwd?: string
  user?: string
  envs?: Record<string, string>
  onStdout?: (data: string) => void | Promise<void>
  onStderr?: (data: string) => void | Promise<void>
  stdin?: boolean
  /** Default 60 000. 0 = no limit (as in E2B; capped by the Harness max lifetime). */
  timeoutMs?: number
  requestTimeoutMs?: number
}

// ============================================================================ commands

/**
 * Same surface as e2b's CommandHandle for a background command: pid, stdout/stderr,
 * exitCode, wait(), kill(), disconnect().
 *
 * Output is read back by polling /tmp/.e2b/p/<tag>.out/.err (about once a second) from the
 * moment the handle is created, so onStdout/onStderr callbacks arrive in chunks rather than
 * line by line.
 */
export class CommandHandle {
  private outOff = 0
  private errOff = 0
  private _stdout = ''
  private _stderr = ''
  private res?: CommandResult
  private failure?: unknown
  private disconnected = false
  private readonly done: Promise<void>

  constructor(
    private readonly sbx: Sandbox, readonly pid: number, private readonly tag: string,
    private readonly onStdout?: CommandStartOpts['onStdout'], private readonly onStderr?: CommandStartOpts['onStderr'],
  ) {
    this.done = this.follow()
    this.done.catch(() => {}) // surfaced by wait(); never an unhandled rejection
  }

  get stdout() { return this._stdout }
  get stderr() { return this._stderr }
  get exitCode() { return this.res?.exitCode }
  get error() { return this.res?.error }

  private async pollOnce(): Promise<[string, string, string]> {
    // Sections are separated by marker lines; base64 is wrapped because V1 drops output lines
    // longer than ~64 KB. Sent through _execOrdered: the reply is verified, and a lost or
    // reordered reply just means "poll again" (offsets only move on success).
    const d = `${STATE}/p/${this.tag}`
    const text = await this.sbx._execOrdered(
      `echo @OUT; tail -c +${this.outOff + 1} ${d}.out 2>/dev/null | base64\n` +
      `echo @ERR; tail -c +${this.errOff + 1} ${d}.err 2>/dev/null | base64\n` +
      `echo @STATE; if [ -f ${d}.rc ]; then cat ${d}.rc; elif kill -0 ${this.pid} 2>/dev/null; then echo run; ` +
      `else echo gone; fi`, 60)
    const m = text?.match(/^@OUT\n([\s\S]*?)@ERR\n([\s\S]*?)@STATE\n([\s\S]*)$/)
    if (!m) return ['', '', 'run'] // reply lost or malformed: poll again
    const out = Buffer.from(m[1], 'base64'), err = Buffer.from(m[2], 'base64')
    this.outOff += out.length
    this.errOff += err.length
    return [out.toString(), err.toString(), m[3].trim()]
  }

  private async follow(): Promise<void> {
    while (!this.disconnected) {
      const [out, err, state] = await this.pollOnce()
      if (out) { this._stdout += out; await this.onStdout?.(out) }
      if (err) { this._stderr += err; await this.onStderr?.(err) }
      if (state === 'run') { await sleep(1000); continue }
      const code = /^-?\d+$/.test(state) ? Number(state) : -1 // "gone" = killed
      this.res = result(this._stdout, this._stderr, code, code === 0 ? undefined : 'process exited')
      return
    }
  }

  /** Resolve when the process exits. Throws e2b's CommandExitError on a non-zero exit. */
  async wait(): Promise<CommandResult> {
    await this.done
    if (!this.res) throw new SandboxError('handle was disconnected before the command finished')
    if (this.res.exitCode !== 0) throw new CommandExitError(this.res)
    return this.res
  }

  /** Stop following output; the process keeps running (reattach with commands.connect). */
  async disconnect(): Promise<void> {
    this.disconnected = true
  }

  async kill(): Promise<boolean> {
    return this.sbx.commands.kill(this.pid)
  }

  declare sendStdin: (data: string | Uint8Array, opts?: unknown) => Promise<never>
  declare closeStdin: (opts?: unknown) => Promise<never>
}
// On the prototype (not per instance) so tools/api-coverage.ts can see them.
Object.assign(CommandHandle.prototype, {
  sendStdin: notSupported('CommandHandle.sendStdin', 'InvokeAgentRuntimeCommand has no stdin channel'),
  closeStdin: notSupported('CommandHandle.closeStdin', 'InvokeAgentRuntimeCommand has no stdin channel'),
})

/** sbx.commands: run / list / kill / connect, same signatures as e2b's Commands. */
export class Commands {
  constructor(private readonly sbx: Sandbox) {}

  get supportsStdinClose() { return false }

  /**
   * Run `cmd` with bash.
   *
   * Foreground: resolves to a CommandResult, throws CommandExitError on a non-zero exit and
   * TimeoutError after `timeoutMs` (0 = no limit, as in E2B).
   * background: true resolves to a CommandHandle as soon as the process is started.
   * envs/cwd apply to this command only; sandbox envs from Sandbox.create always apply.
   * cwd defaults to the template's WORKDIR. user defaults to root (E2B defaults to "user").
   */
  run(cmd: string, opts?: CommandStartOpts & { background?: false }): Promise<CommandResult>
  run(cmd: string, opts: CommandStartOpts & { background: true }): Promise<CommandHandle>
  run(cmd: string, opts?: CommandStartOpts): Promise<CommandHandle | CommandResult>
  async run(cmd: string, opts: CommandStartOpts = {}): Promise<CommandHandle | CommandResult> {
    if (opts.stdin) throw new NotSupportedError('stdin: true is not supported on AgentCore: no stdin channel')
    let body = exportLines(opts.envs)
    const wd = opts.cwd ?? this.sbx._workdir
    if (wd) body += `cd ${q(wd)} || exit 1\n`
    body += cmd + '\n'
    if (opts.user && opts.user !== 'root') body = `exec runuser -u ${q(opts.user)} -- bash -c ${q(body)}\n`

    const timeoutS = opts.timeoutMs === undefined ? 60 : opts.timeoutMs / 1000
    const limit = timeoutS || this.sbx._cfg.maxLifetimeS
    if (!opts.background && this.sbx._v2 && limit > V2_MAX_CALL_S) {
      // Too long for one V2 call: run it as a background command and follow its output, so
      // timeoutMs (and 0 = no limit) behave exactly as on V1.
      const wrapped = timeoutS ? `timeout -k 5 ${Math.trunc(timeoutS)} bash -c ${q(body)}\n` : body
      const start = Date.now()
      try {
        return await (await this.spawn(wrapped, cmd, opts)).wait()
      } catch (e) {
        if (e instanceof CommandExitError && [124, 137].includes(e.exitCode) && timeoutS
          && (Date.now() - start) / 1000 >= timeoutS - 1)
          throw new TimeoutError(`command timed out after ${timeoutS}s: ${cmd.slice(0, 200)}`)
        throw e
      }
    }

    if (opts.background) return this.spawn(body, cmd, opts)

    const start = Date.now()
    const res = await this.sbx._exec(body, limit, { onStdout: opts.onStdout, onStderr: opts.onStderr })
    if ([124, 137].includes(res.exitCode) && timeoutS && (Date.now() - start) / 1000 >= timeoutS - 1)
      throw new TimeoutError(`command timed out after ${timeoutS}s: ${cmd.slice(0, 200)}`)
    if (res.exitCode !== 0) throw new CommandExitError(res)
    return res
  }

  /** Start `body` detached in the microVM; output and exit code go to files we poll. */
  private async spawn(body: string, cmd: string, opts: CommandStartOpts): Promise<CommandHandle> {
    {
      const tag = randomUUID().replace(/-/g, '').slice(0, 12)
      const d = `${STATE}/p/${tag}`
      const spawn =
        `mkdir -p ${STATE}/p\n` +
        `echo ${b64(body)} | base64 -d > ${d}.sh\n` +
        `echo ${q(cmd)} > ${d}.cmd\n` +
        // setsid: own process group, so kill() can take children down too.
        `setsid nohup bash -c '[ -f ${STATE}/env ] && . ${STATE}/env; bash ${d}.sh; echo $? > ${d}.rc' ` +
        `> ${d}.out 2> ${d}.err < /dev/null &\n` +
        `echo $! | tee ${d}.pid\n`
      const r = await this.sbx._exec(spawn, 60, { raw: true })
      if (r.exitCode !== 0) throw new CommandExitError({ ...r, error: 'spawn failed' })
      const pid = Number(r.stdout.trim().split('\n').at(-1))
      return new CommandHandle(this.sbx, pid, tag, opts.onStdout, opts.onStderr)
    }
  }

  /** Running background commands started through this SDK (like E2B: only envd-started ones). */
  async list(_opts?: unknown): Promise<ProcessInfo[]> {
    const out = (await this.sbx._exec(
      `for f in ${STATE}/p/*.pid; do [ -e "$f" ] || continue; t=$(basename $f .pid); p=$(cat $f); ` +
      `[ -f ${STATE}/p/$t.rc ] && continue; kill -0 $p 2>/dev/null || continue; ` +
      `printf '%s\\t%s\\t%s\\t%s\\n' "$p" "$t" "$(readlink /proc/$p/cwd)" "$(cat ${STATE}/p/$t.cmd)"; done`,
      30, { raw: true })).stdout
    return out.split('\n').filter(Boolean).map((line) => {
      const [pid, tag, cwd, ...rest] = line.split('\t')
      return { pid: Number(pid), tag, cmd: '/bin/bash', args: ['-l', '-c', rest.join('\t')], envs: {}, cwd }
    })
  }

  /** SIGKILL the process group (falls back to the single pid). True if it existed. */
  async kill(pid: number, _opts?: unknown): Promise<boolean> {
    const p = Math.trunc(Number(pid))
    const r = await this.sbx._exec(`kill -0 ${p} 2>/dev/null || exit 1; kill -KILL -- -${p} 2>/dev/null || kill -KILL ${p}`,
      30, { raw: true })
    return r.exitCode === 0
  }

  /** Reattach to a background command started with run(cmd, { background: true }). */
  async connect(pid: number, opts: Pick<CommandStartOpts, 'onStdout' | 'onStderr' | 'timeoutMs'> = {}): Promise<CommandHandle> {
    const p = Math.trunc(Number(pid))
    const r = await this.sbx._exec(`grep -lx ${p} ${STATE}/p/*.pid 2>/dev/null | head -1`, 30, { raw: true })
    const file = r.stdout.trim()
    if (!file) throw new NotFoundError(`no background command with pid ${pid}`)
    return new CommandHandle(this.sbx, p, file.split('/').pop()!.slice(0, -4), opts.onStdout, opts.onStderr)
  }

  sendStdin = notSupported('commands.sendStdin', 'InvokeAgentRuntimeCommand has no stdin channel')
  closeStdin = notSupported('commands.closeStdin', 'InvokeAgentRuntimeCommand has no stdin channel')
}

// ============================================================================ files

type WriteData = string | ArrayBuffer | ArrayBufferView | Blob | ReadableStream
export type WriteEntry = { path: string; data: WriteData }
interface FileOpts { user?: string; requestTimeoutMs?: number; [k: string]: unknown }

async function toBytes(data: WriteData): Promise<Uint8Array> {
  if (typeof data === 'string') return new TextEncoder().encode(data)
  if (data instanceof ArrayBuffer) return new Uint8Array(data)
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength)
  if (typeof Blob !== 'undefined' && data instanceof Blob) return new Uint8Array(await data.arrayBuffer())
  const chunks: Uint8Array[] = []
  const reader = (data as ReadableStream<Uint8Array>).getReader()
  for (let r = await reader.read(); !r.done; r = await reader.read()) chunks.push(r.value)
  return Buffer.concat(chunks)
}

/** sbx.files: same methods and return shapes as e2b's Filesystem. Needs GNU find + base64 in the image. */
export class Filesystem {
  constructor(private readonly sbx: Sandbox) {}

  private async run(script: string, path: string, timeoutS = 120): Promise<CommandResult> {
    const r = await this.sbx._exec(script, timeoutS, { raw: true })
    if (r.exitCode === ENOENT) throw new FileNotFoundError(`path '${path}' does not exist`)
    if (r.exitCode !== 0) throw new CommandExitError(r)
    return r
  }

  private mustExist(p: string) {
    return `[ -e ${p} ] || [ -L ${p} ] || exit ${ENOENT}\n`
  }

  private asUser(script: string, user?: string) {
    return user && user !== 'root' ? `runuser -u ${q(user)} -- bash -c ${q(script)}` : script
  }

  /** format 'text' (default) -> string, 'bytes' -> Uint8Array, 'blob' -> Blob, 'stream' -> ReadableStream. */
  read(path: string, opts?: FileOpts & { format?: 'text' }): Promise<string>
  read(path: string, opts: FileOpts & { format: 'bytes' }): Promise<Uint8Array>
  read(path: string, opts: FileOpts & { format: 'blob' }): Promise<Blob>
  read(path: string, opts: FileOpts & { format: 'stream' }): Promise<ReadableStream<Uint8Array>>
  async read(path: string, opts: FileOpts & { format?: 'text' | 'bytes' | 'blob' | 'stream' } = {}): Promise<unknown> {
    const p = q(path)
    const size = Number((await this.run(this.mustExist(p) + this.asUser(`stat -L -c %s ${p}`, opts.user), path))
      .stdout.trim().split(/\s+/).at(-1))
    const parts: Buffer[] = []
    for (let off = 0; off < size; off += READ_CHUNK)
      parts.push(await this.readChunk(p, off, Math.min(READ_CHUNK, size - off), opts.user, path))
    const raw = new Uint8Array(Buffer.concat(parts))
    switch (opts.format) {
      case 'bytes': return raw
      case 'blob': return new Blob([raw])
      case 'stream': return new ReadableStream<Uint8Array>({ start(c) { c.enqueue(raw); c.close() } })
      default: return new TextDecoder().decode(raw)
    }
  }

  /** `n` bytes at `off`, verified (see Sandbox._execOrdered); refetched if incomplete. */
  private async readChunk(p: string, off: number, n: number, user: string | undefined, path: string): Promise<Buffer> {
    const script = this.asUser(`tail -c +${off + 1} ${p} | head -c ${n} | base64 -w ${B64_LINE}`, user)
    for (let i = 0; i < READ_RETRIES; i++) {
      const text = await this.sbx._execOrdered(script, 120)
      if (text === undefined) continue
      const data = Buffer.from(text, 'base64')
      if (data.length === n) return data
    }
    throw new SandboxError(`files.read('${path}'): output kept arriving incomplete at offset ${off}`)
  }

  /**
   * Create or overwrite a file (parent dirs are created). Also accepts an array of
   * { path, data } like e2b. Large files are sent in ~34 KB chunks (45 000 base64 chars),
   * one command each, so expect roughly 100 KB/s.
   */
  write(path: string, data: WriteData, opts?: FileOpts): Promise<WriteInfo>
  write(files: WriteEntry[], opts?: FileOpts): Promise<WriteInfo[]>
  async write(pathOrFiles: string | WriteEntry[], dataOrOpts?: WriteData | FileOpts, maybeOpts?: FileOpts): Promise<WriteInfo | WriteInfo[]> {
    if (Array.isArray(pathOrFiles)) return this.writeFiles(pathOrFiles, dataOrOpts as FileOpts)
    const path = pathOrFiles
    const opts = maybeOpts ?? {}
    const raw = await toBytes(dataOrOpts as WriteData)
    const p = q(path), tmp = q(`${path}.e2b-part`)
    await this.run(`mkdir -p "$(dirname ${p})" && : > ${tmp}`, path)
    const enc = b64(raw)
    // _exec base64-encodes the whole script again (x4/3), so a chunk must stay under ~3/4
    // of the 64 KB command limit: 45 000 chars -> ~60 KB on the wire.
    const step = 45000
    for (let i = 0; i < enc.length; i += step) await this.run(`echo ${enc.slice(i, i + step)} | base64 -d >> ${tmp}`, path)
    const owner = opts.user && opts.user !== 'root' ? ` && chown ${q(opts.user)} ${p}` : ''
    await this.run(`mv -f ${tmp} ${p}${owner}`, path) // atomic replace: readers never see half a file
    return { name: path.split('/').pop()!, type: FileType.FILE, path }
  }

  async writeFiles(files: WriteEntry[], opts?: FileOpts): Promise<WriteInfo[]> {
    const out: WriteInfo[] = []
    for (const f of files) out.push(await this.write(f.path, f.data, opts))
    return out
  }

  private entries(out: string): EntryInfo[] {
    return out.split('\n').filter(Boolean).map((line) => {
      const [t, size, mode, permissions, owner, group, mtime, link, ...rest] = line.split('\t')
      const p = rest.join('\t')
      const type = t === 'd' ? FileType.DIR : t === 'l' ? FileType.SYMLINK : FileType.FILE
      return {
        name: p.replace(/\/+$/, '').split('/').pop() || '/', type, path: p, size: Number(size),
        mode: parseInt(mode, 8), permissions, owner, group, modifiedTime: new Date(parseFloat(mtime) * 1000),
        ...(link ? { symlinkTarget: link } : {}),
      }
    })
  }

  async list(path: string, opts: FileOpts & { depth?: number } = {}): Promise<EntryInfo[]> {
    const p = q(path)
    const out = (await this.run(this.mustExist(p) +
      `find ${p} -mindepth 1 -maxdepth ${Math.trunc(opts.depth ?? 1) || 1} -printf '${STAT_FMT}'`, path)).stdout
    return this.entries(out)
  }

  async getInfo(path: string, _opts?: FileOpts): Promise<EntryInfo> {
    const p = q(path)
    return this.entries((await this.run(this.mustExist(p) + `find ${p} -maxdepth 0 -printf '${STAT_FMT}'`, path)).stdout)[0]
  }

  async exists(path: string, _opts?: FileOpts): Promise<boolean> {
    const p = q(path)
    return (await this.sbx._exec(`[ -e ${p} ] || [ -L ${p} ]`, 30, { raw: true })).exitCode === 0
  }

  /** true if created, false if it already existed (same as E2B). */
  async makeDir(path: string, _opts?: FileOpts): Promise<boolean> {
    const p = q(path)
    const r = await this.sbx._exec(`[ -d ${p} ] && exit 3; mkdir -p ${p}`, 30, { raw: true })
    if (r.exitCode !== 0 && r.exitCode !== 3) throw new CommandExitError(r)
    return r.exitCode === 0
  }

  async remove(path: string, _opts?: FileOpts): Promise<void> {
    const p = q(path)
    await this.run(this.mustExist(p) + `rm -rf ${p}`, path)
  }

  async rename(oldPath: string, newPath: string, _opts?: FileOpts): Promise<EntryInfo> {
    const o = q(oldPath), n = q(newPath)
    await this.run(this.mustExist(o) + `mv ${o} ${n}`, oldPath)
    return this.getInfo(newPath)
  }

  watchDir = notSupported('files.watchDir', 'no file-event stream from the microVM; poll files.list instead') as
    (path: string, onEvent: (e: FilesystemEvent) => void, opts?: unknown) => Promise<never>
}

// ============================================================================ sandbox

/** Drop-in for e2b's Sandbox. See the module docstring for the mapping. */
export class Sandbox {
  // Same constants as e2b's Sandbox. Timeouts are informational only: the real limits are
  // Config.idleTimeoutS / maxLifetimeS on the Harness.
  static readonly defaultTemplate = 'base'
  static readonly defaultMcpTemplate = 'mcp-gateway'
  static readonly defaultSandboxTimeoutMs = 300_000

  readonly commands: Commands
  readonly files: Filesystem
  /** e2b's own Git helper, running over this provider's commands. */
  readonly git: Git
  readonly sandboxDomain = undefined

  /** @internal */ readonly _cfg: Config
  /** @internal */ readonly _workdir?: string
  /** @internal V2 runtime: one command call can wait at most V2_MAX_CALL_S */ readonly _v2: boolean
  private readonly rec: SandboxRecord
  private readonly arn: string
  private readonly data: BedrockAgentCoreClient

  /** Use Sandbox.create / Sandbox.connect; the constructor does no I/O. */
  constructor(cfg: Config, rec: SandboxRecord) {
    this._cfg = cfg
    this.rec = rec
    this.arn = rec.agent_runtime_arn ?? rec.harness_arn
    this._workdir = rec.workdir ?? undefined
    this._v2 = rec.platform_version === 'V2'
    // requestTimeout must outlast the longest command: a command that prints nothing for a
    // long time must not be cut off by the HTTP client.
    this.data = new BedrockAgentCoreClient({
      region: cfg.region, maxAttempts: 3,
      requestHandler: { requestTimeout: (cfg.maxLifetimeS + 120) * 1000, connectionTimeout: 30_000 },
    })
    this.commands = new Commands(this)
    this.files = new Filesystem(this)
    this.git = new Git(this.commands as any)
  }

  get sandboxId(): string { return this.rec.sandbox_id }

  get pty(): never {
    throw new NotSupportedError(
      'sbx.pty is not supported by this provider yet. AgentCore has an interactive shell over WebSocket ' +
      '(bedrock-agentcore runtime open_shell(runtimeArn, sessionId) in the Python SDK)')
  }

  // --------------------------------------------------------------- lifecycle

  /**
   * Start a microVM from a template built with Template.build.
   *
   * Like E2B: the default template is "base"; envs apply to every command; the template's
   * start command (setStartCmd) is started and its ready command polled before resolving.
   * Not per-sandbox on AgentCore (accepted, warned, no effect): timeoutMs, network,
   * allowInternetAccess, lifecycle, mcp, volumeMounts. Set those on the Harness via Config.
   */
  static create<S extends typeof Sandbox>(this: S, opts?: SandboxOpts): Promise<InstanceType<S>>
  static create<S extends typeof Sandbox>(this: S, template: string, opts?: SandboxOpts): Promise<InstanceType<S>>
  static async create(templateOrOpts?: string | SandboxOpts, maybeOpts?: SandboxOpts): Promise<Sandbox> {
    const opts = (typeof templateOrOpts === 'string' ? maybeOpts : templateOrOpts) ?? {}
    const alias = (typeof templateOrOpts === 'string' ? templateOrOpts : opts.template) ?? this.defaultTemplate
    const cfg = opts.config ?? new Config()
    return awsErrors(async () => {
      const tpl = await loadTemplate(cfg, alias)
      if (!tpl) throw new NotFoundError(`template '${alias}' not found; build it with Template.build from e2b-agentcore`)
      for (const k of ['timeoutMs', 'network', 'lifecycle', 'allowInternetAccess', 'mcp', 'volumeMounts'])
        if (opts[k] !== undefined) warn(`Sandbox.create({ ${k} }) has no per-sandbox effect on AgentCore`)

      const started = Date.now() / 1000
      const rec: SandboxRecord = {
        sandbox_id: 'sbx' + randomUUID().replace(/-/g, ''), // 35 chars; runtimeSessionId needs >= 33
        // The ARN commands go to: Harness ARN (V1) or Runtime ARN (V2). harness_arn is kept
        // under its old name too, for SDK versions that only read that field.
        template: alias, agent_runtime_arn: tpl.agent_runtime_arn ?? tpl.harness_arn!,
        harness_arn: tpl.agent_runtime_arn ?? tpl.harness_arn!, platform_version: tpl.platform_version ?? 'V1',
        workdir: tpl.workdir ?? null,
        metadata: opts.metadata ?? {}, started_at: started, end_at: started + cfg.maxLifetimeS,
      }
      const sbx = new this(cfg, rec)

      // First command provisions the microVM (cold start ~4 s). It also writes the in-VM
      // state (id marker, sandbox envs) and reports the VM size for getInfo().
      const r = await sbx._exec(
        `mkdir -p ${STATE}/p && chmod 700 ${STATE}\n` +
        `echo ${rec.sandbox_id} > ${STATE}/id\n` +
        `echo ${b64(exportLines(opts.envs)) || '""'} | base64 -d > ${STATE}/env\n` +
        `chmod 600 ${STATE}/env\n` +
        `nproc; awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo`, 120, { raw: true })
      const nums = r.stdout.split(/\s+/).filter((x) => /^\d+$/.test(x)).map(Number)
      rec.cpu_count = nums[0] ?? 0
      rec.memory_mb = nums[1] ?? 0

      // E2B runs setStartCmd when the sandbox starts and waits for its ready command.
      if (tpl.start_cmd) {
        const h = await sbx.commands.run(tpl.start_cmd, { background: true })
        await h.disconnect() // the start command runs for the sandbox's lifetime; don't follow it
        rec.start_pid = h.pid
        const deadline = Date.now() + 120_000
        while (tpl.ready_cmd && (await sbx._exec(tpl.ready_cmd, 30)).exitCode !== 0) {
          if (Date.now() > deadline) {
            await sbx.kill()
            throw new TimeoutError(`ready command did not succeed within 120 s: ${tpl.ready_cmd}`)
          }
          await sleep(1000)
        }
      }
      await cfg.putJson(`sandboxes/${rec.sandbox_id}.json`, rec)
      return sbx
    })
  }

  private static async fromRegistry(sandboxId: string, cfg = new Config()): Promise<Sandbox> {
    const rec = await cfg.getJson<SandboxRecord>(`sandboxes/${sandboxId}.json`)
    if (!rec) throw new SandboxNotFoundError(`sandbox ${sandboxId} not found`)
    return new this(cfg, rec)
  }

  /** Sandbox.connect(sandboxId): reattach to a running sandbox (works across processes). */
  static async connect<S extends typeof Sandbox>(this: S, sandboxId: string, opts: AgentCoreOpts & Record<string, unknown> = {}): Promise<InstanceType<S>> {
    const sbx = await (this as typeof Sandbox).fromRegistry(sandboxId, opts.config)
    return (await sbx.connect()) as InstanceType<S>
  }

  /**
   * Invoking a reaped session id silently boots a NEW empty microVM on AgentCore. E2B would
   * say "not found" instead, so check the id marker and undo the accidental boot.
   */
  async connect(_opts?: unknown): Promise<this> {
    return awsErrors(async () => {
      const r = await this._exec(`cat ${STATE}/id 2>/dev/null`, 60, { raw: true })
      if (r.stdout.trim() !== this.sandboxId) {
        await this.stop()
        await this._cfg.deleteJson(`sandboxes/${this.sandboxId}.json`)
        throw new SandboxNotFoundError(`sandbox ${this.sandboxId} is no longer running ` +
          `(idle timeout ${this._cfg.idleTimeoutS}s or max lifetime reached)`)
      }
      return this
    })
  }

  private async stop(): Promise<boolean> {
    try {
      await this.data.send(new StopRuntimeSessionCommand({
        agentRuntimeArn: this.arn, runtimeSessionId: this.sandboxId, qualifier: 'DEFAULT',
      }))
      return true
    } catch (err) {
      if (awsCode(err) === 'ResourceNotFoundException') return false
      throw err
    }
  }

  /** sbx.kill() or Sandbox.kill(sandboxId). */
  static async kill(sandboxId: string, opts: AgentCoreOpts & Record<string, unknown> = {}): Promise<boolean> {
    const cfg = opts.config ?? new Config()
    const rec = await cfg.getJson<SandboxRecord>(`sandboxes/${sandboxId}.json`)
    if (!rec) return false
    return new this(cfg, rec).kill()
  }

  async kill(_opts?: unknown): Promise<boolean> {
    return awsErrors(async () => {
      const stopped = await this.stop()
      await this._cfg.deleteJson(`sandboxes/${this.sandboxId}.json`)
      return stopped
    })
  }

  /** `await using sbx = await Sandbox.create(...)` kills the sandbox at the end of the scope. */
  async [Symbol.asyncDispose](): Promise<void> {
    await this.kill()
  }

  static async setTimeout(_sandboxId: string, timeoutMs: number, opts: AgentCoreOpts = {}): Promise<void> {
    const cfg = opts.config ?? new Config()
    warn(`setTimeout(${timeoutMs}) has no effect on AgentCore: idle timeout / max lifetime are per Harness ` +
      `(now ${cfg.idleTimeoutS}s / ${cfg.maxLifetimeS}s)`)
  }

  async setTimeout(timeoutMs: number, _opts?: unknown): Promise<void> {
    return Sandbox.setTimeout(this.sandboxId, timeoutMs, { config: this._cfg })
  }

  /**
   * Registry check, then the same id-marker probe as connect(). A reaped sandbox is cleaned
   * up and reported as not running.
   */
  async isRunning(_opts?: unknown): Promise<boolean> {
    if (Date.now() / 1000 > this.rec.end_at) return false
    if (!(await this._cfg.getJson(`sandboxes/${this.sandboxId}.json`))) return false
    try {
      await this.connect()
      return true
    } catch (err) {
      if (err instanceof SandboxNotFoundError) return false
      throw err
    }
  }

  static async getInfo(sandboxId: string, opts: AgentCoreOpts & Record<string, unknown> = {}): Promise<SandboxInfo> {
    return (await (this as typeof Sandbox).fromRegistry(sandboxId, opts.config)).getInfo()
  }

  async getInfo(_opts?: unknown): Promise<SandboxInfo> {
    return infoFrom(this.rec)
  }

  static async getMetrics(sandboxId: string, opts: AgentCoreOpts & Record<string, unknown> = {}): Promise<SandboxMetrics[]> {
    return (await (this as typeof Sandbox).fromRegistry(sandboxId, opts.config)).getMetrics()
  }

  /** One live sample (E2B returns a time series; AgentCore keeps none per session). */
  async getMetrics(_opts?: unknown): Promise<SandboxMetrics[]> {
    const out = (await this._exec(
      'read -r _ a b c d e f g h _ < /proc/stat; t1=$((a+b+c+d+e+f+g+h)); i1=$((d+e)); sleep 0.5\n' +
      'read -r _ a b c d e f g h _ < /proc/stat; t2=$((a+b+c+d+e+f+g+h)); i2=$((d+e))\n' +
      'echo $(nproc) $(( (100*((t2-t1)-(i2-i1)))/((t2-t1)>0?(t2-t1):1) ))\n' +
      "awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}/^Cached/{c=$2}END{print t*1024,(t-a)*1024,c*1024}' /proc/meminfo\n" +
      'df -B1 --output=size,used / | tail -1', 30, { raw: true })).stdout.split(/\s+/).filter(Boolean).map(Number)
    const n = [...out, 0, 0, 0, 0, 0, 0, 0]
    return [{
      cpuCount: n[0], cpuUsedPct: n[1], memTotal: n[2], memUsed: n[3], memCache: n[4],
      diskTotal: n[5], diskUsed: n[6], timestamp: new Date(),
    }]
  }

  /**
   * Same as e2b's Sandbox.list: returns a paginator (hasNext / nextItems()). Source is the
   * S3 registry, filtered by query.metadata / query.template / query.startedAfter / query.state.
   * Sandboxes reaped by the idle timeout stay listed until connect/isRunning notices.
   */
  static list(opts: SandboxListOpts = {}): SandboxPaginator {
    return new SandboxPaginator(opts)
  }

  // --------------------------------------------------------------- E2B features with no AgentCore equivalent

  pause = notSupported('Sandbox.pause', 'no memory snapshot; session storage (Preview) keeps only the filesystem')
  betaPause = notSupported('Sandbox.betaPause', 'no memory snapshot')
  createSnapshot = notSupported('Sandbox.createSnapshot', 'no VM snapshot API')
  listSnapshots = notSupported('Sandbox.listSnapshots', 'no VM snapshot API')
  fork = notSupported('Sandbox.fork', 'no VM snapshot API')
  getHost = notSupported('Sandbox.getHost', 'AgentCore exposes no inbound port of a session')
  uploadUrl = notSupported('Sandbox.uploadUrl', 'no direct HTTP endpoint into the session; use files.write')
  downloadUrl = notSupported('Sandbox.downloadUrl', 'no direct HTTP endpoint into the session; use files.read')
  updateNetwork = notSupported('Sandbox.updateNetwork', 'network mode is set per Harness')
  getMcpUrl = notSupported('Sandbox.getMcpUrl', 'use AgentCore Gateway for MCP')
  getMcpToken = notSupported('Sandbox.getMcpToken', 'use AgentCore Gateway for MCP')
  static pause = notSupported('Sandbox.pause', 'no memory snapshot')
  static betaPause = notSupported('Sandbox.betaPause', 'no memory snapshot')
  static fork = notSupported('Sandbox.fork', 'no VM snapshot API')
  static createSnapshot = notSupported('Sandbox.createSnapshot', 'no VM snapshot API')
  static listSnapshots = notSupported('Sandbox.listSnapshots', 'no VM snapshot API')
  static deleteSnapshot = notSupported('Sandbox.deleteSnapshot', 'no VM snapshot API')
  static updateNetwork = notSupported('Sandbox.updateNetwork', 'network mode is set per Harness')
  static getFullInfo = Sandbox.getInfo

  // --------------------------------------------------------------- transport

  /**
   * Run an SDK-internal, read-only `script` and resolve to its stdout verified, or undefined.
   *
   * AgentCore streams stdout one line per event, and big outputs are not delivered reliably:
   * on V1 and V2 the last lines can be lost when the command exits right after printing, and
   * on V2 lines can also arrive out of order. So every line is numbered in the microVM (awk)
   * and followed by an end line carrying the count; here the lines are put back in order and
   * the reply is accepted only if every line 1..count is present. Callers retry on undefined.
   * User commands (commands.run) cannot be fixed this way, since their output is the user's own.
   * @internal
   */
  async _execOrdered(script: string, timeoutS: number): Promise<string | undefined> {
    const r = await this._exec(`{ ${script}\n} | awk '{print NR":"$0} END{print "${END_MARK}:"NR}'`, timeoutS, { raw: true })
    const lines = new Map<number, string>()
    let total: number | undefined
    for (const line of r.stdout.split('\n')) {
      const k = line.indexOf(':')
      if (k < 0) continue
      const head = line.slice(0, k), body = line.slice(k + 1)
      if (head === END_MARK && /^\d+$/.test(body)) total = Number(body)
      else if (/^\d+$/.test(head)) lines.set(Number(head), body)
    }
    if (total === undefined || lines.size !== total) return undefined
    let out = ''
    for (let i = 1; i <= total; i++) {
      const l = lines.get(i)
      if (l === undefined) return undefined
      out += l + '\n'
    }
    return out
  }

  /**
   * Run `script` with bash in the microVM: one InvokeAgentRuntimeCommand call.
   *
   * The script travels base64-encoded (no quoting issues), is written to a temp file and run
   * under coreutils `timeout`. Sandbox envs are sourced first unless raw (internal helpers
   * that don't need them).
   * @internal
   */
  async _exec(script: string, timeoutS: number,
    o: { onStdout?: CommandStartOpts['onStdout']; onStderr?: CommandStartOpts['onStderr']; raw?: boolean } = {},
  ): Promise<CommandResult> {
    const pre = o.raw ? '' : `[ -f ${STATE}/env ] && . ${STATE}/env\n`
    let t = Math.max(1, Math.trunc(timeoutS))
    if (this._v2) t = Math.min(t, V2_MAX_CALL_S) // Commands.run goes background past this
    const cmd = `/bin/sh -c 'f=$(mktemp); echo ${b64(pre + script)} | base64 -d > $f; ` +
      `timeout -k 5 ${t} bash $f; rc=$?; rm -f $f; exit $rc'`
    if (cmd.length > MAX_CMD)
      throw new InvalidArgumentError('command longer than the 64 KB AgentCore limit; write it to a file with files.write and run that')

    let resp
    for (let attempt = 0; ; attempt++) {
      try {
        resp = await this.data.send(new InvokeAgentRuntimeCommandCommand({
          agentRuntimeArn: this.arn, runtimeSessionId: this.sandboxId, qualifier: 'DEFAULT',
          contentType: 'application/json', accept: 'application/vnd.amazon.eventstream',
          body: { command: cmd, timeout: t + 30 },
        } as any))
        break
      } catch (err) {
        // RetryableConflict: the session is still starting or busy with a previous call.
        if (['RetryableConflictException', 'ThrottlingException'].includes(awsCode(err)) && attempt < 9) {
          await sleep(3000)
          continue
        }
        throw err
      }
    }
    let out = '', err = '', code: number | undefined
    for await (const ev of resp.stream ?? []) {
      if (!ev.chunk) {
        const [name, val] = Object.entries(ev)[0] ?? ['unknown', {}]
        throw new SandboxError(`${name}: ${(val as any)?.message ?? JSON.stringify(val)}`)
      }
      const d = ev.chunk.contentDelta
      if (d?.stdout) { out += d.stdout; await o.onStdout?.(d.stdout) }
      if (d?.stderr) { err += d.stderr; await o.onStderr?.(d.stderr) }
      if (ev.chunk.contentStop) code = ev.chunk.contentStop.exitCode
    }
    const c = code === undefined ? -1 : Number(code)
    return result(out, err, c, c === 0 ? undefined : `exit status ${c}`)
  }
}

function infoFrom(r: SandboxRecord): SandboxInfo {
  return {
    sandboxId: r.sandbox_id, templateId: r.template, name: r.template, metadata: r.metadata ?? {},
    startedAt: new Date(r.started_at * 1000), endAt: new Date(r.end_at * 1000), state: 'running',
    cpuCount: r.cpu_count ?? 0, memoryMB: r.memory_mb ?? 0, envdVersion: 'agentcore',
  }
}

export interface SandboxListOpts extends AgentCoreOpts {
  query?: { metadata?: Record<string, string>; state?: Array<'running' | 'paused'>; startedAfter?: Date; template?: string }
  order?: 'asc' | 'desc'
  limit?: number
  nextToken?: string
  [e2bOpt: string]: unknown
}

/** Same shape as e2b's SandboxPaginator: `while (p.hasNext) for (const i of await p.nextItems()) ...` */
export class SandboxPaginator {
  private offset: number
  private items?: SandboxInfo[]
  private _hasNext = true
  private _nextToken?: string
  private readonly cfg: Config

  constructor(private readonly opts: SandboxListOpts) {
    this.cfg = opts.config ?? new Config()
    this.offset = Number(opts.nextToken ?? 0)
    this._nextToken = opts.nextToken
  }

  get hasNext() { return this._hasNext }
  get nextToken() { return this._nextToken }

  private async all(): Promise<SandboxInfo[]> {
    if (!this.items) {
      const nowS = Date.now() / 1000, qy = this.opts.query
      const recs = await this.cfg.listJson<SandboxRecord>('sandboxes/')
      this.items = recs
        .filter((r) => r.end_at >= nowS)
        .filter((r) => !qy?.metadata || Object.entries(qy.metadata).every(([k, v]) => r.metadata?.[k] === v))
        .filter((r) => !qy?.template || r.template === qy.template)
        .filter((r) => !qy?.startedAfter || r.started_at * 1000 > qy.startedAfter.getTime())
        .filter((r) => !qy?.state || qy.state.includes('running'))
        .map(infoFrom)
        .sort((a, b) => (this.opts.order === 'asc' ? 1 : -1) * (a.startedAt.getTime() - b.startedAt.getTime()))
    }
    return this.items
  }

  async nextItems(_opts?: unknown): Promise<SandboxInfo[]> {
    const items = await awsErrors(() => this.all())
    const limit = this.opts.limit ?? 100
    const page = items.slice(this.offset, this.offset + limit)
    this.offset += page.length
    this._hasNext = this.offset < items.length
    this._nextToken = this._hasNext ? String(this.offset) : undefined
    return page
  }
}
