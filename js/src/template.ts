/**
 * `Template` for the AgentCore provider: E2B's own template DSL, built into an AgentCore Harness.
 *
 * This class EXTENDS the official `TemplateBase` from the e2b package, so every builder method
 * of the official SDK (fromDockerfile, fromUbuntuImage, aptInstall, pipInstall, copy, runCmd,
 * setEnvs, setWorkdir, setStartCmd, ...) is the real E2B code, not a re-implementation. Only
 * the static methods that talk to E2B's cloud (build, buildInBackground, getBuildStatus,
 * exists, aliasExists, tags) are replaced with AgentCore versions.
 *
 *   import { Template, waitForPort, defaultBuildLogger } from 'e2b-agentcore'
 *   const t = Template()
 *     .fromUbuntuImage('24.04')
 *     .aptInstall(['git', 'python3'])
 *     .setWorkdir('/work')
 *     .setStartCmd('python3 -m http.server 8000', waitForPort(8000))
 *   await Template.build(t, 'my-sandbox', { onBuildLogs: defaultBuildLogger() })
 *
 * What happens in Template.build(t, 'my-sandbox')
 * ------------------------------------------------
 *  1. ensureInfra()      first build only: create S3 bucket, ECR repo, CodeBuild role/project.
 *                        (Runs after steps 2-3, so an invalid template fails before any AWS call.)
 *  2. renderDockerfile   turn the E2B instruction list into a Dockerfile (FROM / RUN / COPY /
 *                        ENV / WORKDIR / USER). The start command is NOT baked in as an
 *                        ENTRYPOINT: like E2B, we run it ourselves when a sandbox starts.
 *  3. zipContext         zip that Dockerfile plus only the local files referenced by COPY
 *                        steps (paths relative to fileContextPath, .dockerignore honoured).
 *  4. upload to S3       s3://<bucket>/contexts/<alias>/<uuid>.zip (expires after 7 days)
 *  5. CodeBuild          native arm64 `docker build`, then push to ECR as
 *                        <registry>/<prefix>/templates:<alias>-<YYYYmmddHHMMSS>
 *  6. deploy             V1 (default): CreateHarness (first build) or UpdateHarness (rebuild)
 *                        named e2b_<alias>, with the new image as its environment.
 *                        V2 (Config platformVersion 'V2'): CreateAgentRuntime /
 *                        UpdateAgentRuntime named e2b_<alias> with platformVersion=V2 and the
 *                        image as the runtime container. We poll until it is READY and serving
 *                        the new image.
 *  7. template record    s3://<bucket>/templates/<alias>.json keeps the E2B-only settings
 *                        (start cmd, ready cmd, workdir) that Sandbox.create needs.
 *
 * build() is buildInBackground() + a getBuildStatus() loop, so both paths share code. The
 * build record (builds/<id>.json) means another process holding only the BuildInfo can poll
 * getBuildStatus and finish steps 6-7.
 *
 * Why a Harness by default, and not a plain AgentCore Runtime? A Runtime requires the image
 * to run an HTTP server on :8080 (/ping, /invocations). A Harness uses the image only as the
 * sandbox environment, so any E2B-style template (e.g. bare Ubuntu with no server) works
 * unchanged.
 *
 * Why V2 is a plain Runtime: CreateHarness / UpdateHarness have no platformVersion field, and
 * the runtime a Harness creates behind the scenes is always V1. So for V2 we deploy our own
 * Runtime and make the image meet the Runtime contract ourselves: the generated Dockerfile
 * gets an extra build stage that compiles SERVER_GO (a ~20-line static Go server answering
 * /ping and /invocations) and copies the binary in as the ENTRYPOINT. The user's image needs
 * no Python, Node or anything else for this. Commands and files still go through
 * InvokeAgentRuntimeCommand exactly as on V1, so Sandbox code is identical on both.
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { globSync } from 'glob'
import { minimatch } from 'minimatch'
import { zipSync, type Zippable } from 'fflate'
import {
  TemplateBase, LogEntry, LogEntryStart, LogEntryEnd, BuildError, TemplateError,
  type BuildInfo, type TemplateBuildStatusResponse, type TemplateClass, type ConnectionOpts,
} from 'e2b'
import { BatchGetBuildsCommand, StartBuildCommand, type Build } from '@aws-sdk/client-codebuild'
import { GetLogEventsCommand } from '@aws-sdk/client-cloudwatch-logs'
import {
  CreateHarnessCommand, UpdateHarnessCommand, GetHarnessCommand, ListHarnessesCommand, DeleteHarnessCommand,
  CreateAgentRuntimeCommand, UpdateAgentRuntimeCommand, GetAgentRuntimeCommand, ListAgentRuntimesCommand,
  DeleteAgentRuntimeCommand,
} from '@aws-sdk/client-bedrock-agentcore-control'
import { PutObjectCommand } from '@aws-sdk/client-s3'
import { Config, ensureInfra, sleep } from './config.js'
import { awsErrors, notSupported, UNSUPPORTED } from './errors.js'

const DOCKERFILE_NAME = 'Dockerfile.e2b' // name of the generated Dockerfile inside the context zip
export const SERVER_FILE = 'e2b-agentcore-server.go' //   V2 only: source of the :8080 server, inside the zip
export const SERVER_BIN = '/opt/e2b-agentcore/server' //    V2 only: where the compiled server lives in the image
export const GO_IMAGE = 'golang:1.24-alpine' //             V2 only: builder stage image (via dockerhubMirror)

/**
 * The HTTP contract an AgentCore Runtime container must meet: listen on 0.0.0.0:8080, answer
 * GET /ping (health) and POST /invocations. The server is also PID 1 and reaps orphans. Nothing here runs sandbox commands: those go
 * through InvokeAgentRuntimeCommand, which AgentCore executes in the container itself.
 * Built with CGO_ENABLED=0, so the binary is static and runs on any arm64 Linux base image
 * (glibc, musl or distroless). Byte-identical to SERVER_GO in the Python package.
 */
export const SERVER_GO = `package main

import (
	"encoding/json"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"syscall"
)

// As PID 1 the server must reap orphans: background commands are detached (setsid nohup),
// so when they exit or are killed their parent is gone and they are re-parented to PID 1.
// Without this they stay zombies, and \`kill -0 <pid>\` keeps reporting them alive.
func reap() {
	ch := make(chan os.Signal, 16)
	signal.Notify(ch, syscall.SIGCHLD)
	for range ch {
		for {
			pid, err := syscall.Wait4(-1, nil, syscall.WNOHANG, nil)
			if pid <= 0 || err != nil {
				break
			}
		}
	}
}

func main() {
	if os.Getpid() == 1 {
		go reap()
	}
	http.HandleFunc("/ping", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(\`{"status":"Healthy"}\`))
	})
	http.HandleFunc("/invocations", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{
			"provider": "e2b-agentcore", "arch": runtime.GOARCH,
			"message":  "sandbox runtime: use InvokeAgentRuntimeCommand for commands and files",
		})
	})
	if err := http.ListenAndServe("0.0.0.0:8080", nil); err != nil {
		panic(err)
	}
}
`

/** Options every AgentCore method accepts on top of E2B's (E2B connection opts are ignored). */
export interface AgentCoreOpts {
  /** Region, prefix, role, timeouts. Defaults to `new Config()` (E2B_AGENTCORE_* env vars). */
  config?: Config
}

export interface BuildOpts extends AgentCoreOpts {
  alias?: string
  tags?: string[]
  /** Accepted for API parity; AgentCore sizes the microVM itself. */
  cpuCount?: number
  /** Accepted for API parity; AgentCore sizes the microVM itself. */
  memoryMB?: number
  /** Accepted for API parity. */
  minFreeDiskMb?: number
  /** docker build --no-cache */
  skipCache?: boolean
  onBuildLogs?: (entry: LogEntry) => void
  [e2bOpt: string]: unknown
}

/** The template record Sandbox.create reads (snake_case, shared with the Python package). */
export interface TemplateRecord {
  alias: string
  /** 'V1' (Harness) or 'V2' (plain Runtime). Records written before V2 support have none. */
  platform_version?: 'V1' | 'V2'
  /** The ARN sandbox commands are sent to: the Harness ARN (V1) or the Runtime ARN (V2). */
  agent_runtime_arn?: string
  agent_runtime_id?: string
  agent_runtime_name?: string
  harness_name?: string
  harness_id?: string
  harness_arn?: string
  image: string
  start_cmd?: string | null
  ready_cmd?: string | null
  workdir?: string | null
  build_id: string
  built_at: number
}

interface BuildRecord {
  alias: string
  harness_name: string
  image: string
  codebuild_id: string
  start_cmd?: string | null
  ready_cmd?: string | null
  workdir?: string | null
  platform_version?: 'V1' | 'V2'
  harness_requested: boolean
  started_at: number
}

/** E2B aliases allow '-' and '.', Harness names only [a-zA-Z][a-zA-Z0-9_]{0,39}. */
export function harnessName(alias: string): string {
  const n = 'e2b_' + alias.replace(/[^a-zA-Z0-9_]/g, '_')
  if (n.length > 40) throw new TemplateError(`template name too long for a Harness name (max 36 chars): '${alias}'`)
  return n
}

const templateKey = (alias: string) => `templates/${alias}.json`
const buildKey = (buildId: string) => `builds/${buildId.split(':').pop()}.json`
const now = () => Date.now() / 1000

/** Read the TS-private fields of an e2b TemplateBase instance (plain properties at runtime). */
function internals(t: TemplateClass) {
  const x = t as any
  return {
    baseImage: x.baseImage as string | undefined,
    baseTemplate: x.baseTemplate as string | undefined,
    instructions: (x.instructions ?? []) as { type: string; args: string[] }[],
    startCmd: x.startCmd as string | undefined,
    readyCmd: x.readyCmd as string | undefined,
    fileContextPath: String(x.fileContextPath ?? '.'),
    fileIgnorePatterns: (x.fileIgnorePatterns ?? []) as string[],
  }
}

function normalizeBuildArgs(nameOrOpts?: string | BuildOpts, opts?: BuildOpts): { alias: string; opts: BuildOpts } {
  const o = typeof nameOrOpts === 'string' ? { ...(opts ?? {}) } : { ...(nameOrOpts ?? {}) }
  const alias = typeof nameOrOpts === 'string' ? nameOrOpts : o.alias
  if (!alias) throw new TemplateError('template name is required')
  return { alias, opts: o }
}

/**
 * Official e2b TemplateBase with build/exists/... retargeted to AgentCore.
 *
 * Every static method takes an optional `{ config: new Config(...) }`; otherwise Config()
 * reads the E2B_AGENTCORE_* env vars. E2B connection options (apiKey, domain, ...) are
 * accepted and ignored so code written for e2b runs unchanged.
 */
export class AgentCoreTemplateBase extends TemplateBase {
  // ------------------------------------------------------------------ build (blocking)

  /** Same signature as e2b's Template.build. Resolves when the Harness is READY. */
  static override async build(template: TemplateClass, nameOrOpts?: any, options?: any): Promise<BuildInfo> {
    const { alias, opts } = normalizeBuildArgs(nameOrOpts, options)
    const cfg = opts.config ?? new Config()
    const logs = opts.onBuildLogs
    logs?.(new LogEntryStart(new Date(), 'Build started'))
    try {
      const info = await this.buildInBackground(template, alias, { ...opts, config: cfg })
      let offset = 0
      for (;;) {
        const st = await this.getBuildStatus(info, { logsOffset: offset, config: cfg })
        for (const e of st.logEntries) logs?.(e) // stream new CodeBuild log lines to the caller
        offset += st.logEntries.length
        if (st.status === 'ready') return info
        if (st.status === 'error') throw new BuildError(st.reason?.message ?? 'build failed')
        await sleep(5000)
      }
    } finally {
      logs?.(new LogEntryEnd(new Date(), 'Build finished'))
    }
  }

  // ------------------------------------------------------------------ build (non-blocking)

  /** Steps 1-5 start here; resolves as soon as CodeBuild has accepted the build. */
  static override async buildInBackground(template: TemplateClass, nameOrOpts?: any, options?: any): Promise<BuildInfo> {
    const { alias, opts } = normalizeBuildArgs(nameOrOpts, options)
    const cfg = opts.config ?? new Config()
    if (!cfg.execRoleArn) throw new TemplateError('set E2B_AGENTCORE_EXEC_ROLE_ARN (the Harness execution role)')
    const hname = harnessName(alias) // validate the name before doing any work
    const log = (msg: string) => opts.onBuildLogs?.(new LogEntry(new Date(), 'info', msg))
    const v2 = cfg.platformVersion === 'V2'

    return awsErrors(async () => {
      const prev = await loadTemplate(cfg, alias)
      const prevPv = prev?.platform_version ?? 'V1'
      if (prev && prevPv !== cfg.platformVersion)
        throw new TemplateError(`template '${alias}' was built for platform ${prevPv}; ` +
          `Template.delete('${alias}') first to rebuild it for ${cfg.platformVersion}`)
      // V2: the image also gets the :8080 server a plain Runtime needs (see SERVER_GO).
      const { buildId, image } = await startImageBuild(cfg, template, alias,
        { skipCache: !!opts.skipCache, log, withServer: v2 })
      // Remember what this build is for, so getBuildStatus() can finish steps 6-7 later,
      // even from another process that only has the BuildInfo.
      const t = internals(template)
      const rec: BuildRecord = {
        alias, harness_name: hname, image, codebuild_id: buildId, start_cmd: t.startCmd ?? null,
        ready_cmd: t.readyCmd ?? null, workdir: lastArg(template, 'WORKDIR') ?? null,
        platform_version: cfg.platformVersion, harness_requested: false, started_at: now(),
      }
      await cfg.putJson(buildKey(buildId), rec)
      return { templateId: hname, buildId, name: alias, alias, tags: opts.tags ?? [] }
    })
  }

  /**
   * Poll one build. Drives steps 6-7 forward when CodeBuild has finished.
   *
   * Status mapping:  CodeBuild running                         -> 'building'
   *                  CodeBuild failed / stopped                -> 'error' (reason = CodeBuild status)
   *                  image pushed, Harness not yet READY on it -> 'building'
   *                  Harness READY on the new image            -> 'ready'
   * logEntries are CodeBuild's CloudWatch log lines, starting at logsOffset.
   */
  static override async getBuildStatus(
    data: Pick<BuildInfo, 'templateId' | 'buildId'>, options?: { logsOffset?: number } & AgentCoreOpts & Record<string, unknown>,
  ): Promise<TemplateBuildStatusResponse> {
    const cfg = options?.config ?? new Config()
    return awsErrors(async () => {
      const rec = await cfg.getJson<BuildRecord>(buildKey(data.buildId))
      if (!rec) throw new TemplateError(`unknown build ${data.buildId}`)
      const cb = (await cfg.codebuild.send(new BatchGetBuildsCommand({ ids: [rec.codebuild_id] }))).builds![0]
      const entries = await codebuildLogs(cfg, cb, options?.logsOffset ?? 0)
      const resp = (status: TemplateBuildStatusResponse['status'], reason?: string): TemplateBuildStatusResponse => ({
        buildID: data.buildId, templateID: data.templateId, status, logEntries: entries,
        logs: entries.map((e) => e.message), ...(reason ? { reason: { message: reason, logEntries: [] } } : {}),
      })

      if (cb.buildStatus === 'IN_PROGRESS') return resp('building')
      if (cb.buildStatus !== 'SUCCEEDED') return resp('error', `CodeBuild ${cb.id} ${cb.buildStatus}`)

      if (rec.platform_version === 'V2') return runtimeStatus(cfg, rec, data, entries, resp)

      // Step 6: point the Harness at the new image (once), then wait for READY.
      if (!rec.harness_requested) {
        await upsertHarness(cfg, rec.harness_name, rec.image)
        rec.harness_requested = true
        await cfg.putJson(buildKey(data.buildId), rec)
        entries.push(new LogEntry(new Date(), 'info', `image pushed; deploying Harness ${rec.harness_name}`))
        return resp('building')
      }
      const summary = await findHarness(cfg, rec.harness_name)
      const h = summary && (await cfg.control.send(new GetHarnessCommand({ harnessId: summary.harnessId }))).harness
      if (!h) return resp('error', `Harness ${rec.harness_name} disappeared`)
      if (String(h.status).endsWith('FAILED')) return resp('error', `Harness ${h.status}: ${h.failureReason ?? ''}`)
      const uri = (h.environmentArtifact as any)?.containerConfiguration?.containerUri
      if (h.status !== 'READY' || uri !== rec.image) return resp('building')

      // Step 7: publish the template record Sandbox.create reads.
      const tpl: TemplateRecord = {
        alias: rec.alias, harness_name: h.harnessName!, harness_id: h.harnessId!, harness_arn: h.arn!,
        image: rec.image, start_cmd: rec.start_cmd, ready_cmd: rec.ready_cmd, workdir: rec.workdir,
        build_id: data.buildId, built_at: now(), platform_version: 'V1', agent_runtime_arn: h.arn!,
      }
      await cfg.putJson(templateKey(rec.alias), tpl)
      entries.push(new LogEntry(new Date(), 'info', `Harness ${h.harnessName} READY (${h.arn})`))
      return resp('ready')
    })
  }

  // ------------------------------------------------------------------ lookup / delete

  static override async exists(name: string, opts?: AgentCoreOpts & ConnectionOpts): Promise<boolean> {
    const cfg = opts?.config ?? new Config()
    // Deployed as a Harness (V1) or as a V2 Runtime.
    return awsErrors(async () => (await findHarness(cfg, harnessName(name))) !== undefined
      || (await findV2Runtime(cfg, name)) !== undefined)
  }

  static override async aliasExists(alias: string, opts?: AgentCoreOpts & ConnectionOpts): Promise<boolean> {
    return this.exists(alias, opts)
  }

  /**
   * Not in e2b's Template (E2B deletes via its CLI): deletes the Harness and the template
   * record. ECR images are kept.
   */
  static async delete(name: string, opts?: AgentCoreOpts): Promise<boolean> {
    const cfg = opts?.config ?? new Config()
    return awsErrors(async () => {
      const h = await findHarness(cfg, harnessName(name))
      const rt = await findV2Runtime(cfg, name)
      if (!h && !rt) return false
      if (h) await cfg.control.send(new DeleteHarnessCommand({ harnessId: h.harnessId }))
      if (rt) await cfg.control.send(new DeleteAgentRuntimeCommand({ agentRuntimeId: rt.agentRuntimeId }))
      await cfg.deleteJson(templateKey(name))
      return true
    })
  }

  // Tags are E2B's template-version aliases; ECR tags exist but a Harness has no tag pointer.
  static override assignTags = notSupported('Template.assignTags', 'a Harness points at one image; rebuild instead') as any
  static override removeTags = notSupported('Template.removeTags', 'a Harness points at one image; rebuild instead') as any
  static override getTags = notSupported('Template.getTags', 'a Harness points at one image; rebuild instead') as any
}

// ---------------------------------------------------------------------- callable Template

const OWN_DIR = path.dirname(fileURLToPath(import.meta.url))

/**
 * Directory of the first stack frame outside this package and outside e2b. e2b defaults
 * fileContextPath to "the caller's directory", but with this wrapper in between e2b would
 * see our file as the caller, so we work it out here and pass it in explicitly.
 */
function callerDirectory(): string | undefined {
  for (const line of (new Error().stack ?? '').split('\n').slice(1)) {
    const m = line.match(/\(?((?:file:\/\/)?\/[^():]+):\d+:\d+\)?\s*$/)
    if (!m) continue
    const file = m[1].startsWith('file:') ? fileURLToPath(m[1]) : m[1]
    if (file.startsWith(OWN_DIR + path.sep) || file.includes(`${path.sep}node_modules${path.sep}e2b${path.sep}`)) continue
    return path.dirname(file)
  }
  return undefined
}

type TemplateOptions = { fileContextPath?: fs.PathLike; fileIgnorePatterns?: string[] }

function callableTemplate<T extends typeof AgentCoreTemplateBase>(cls: T) {
  // Like e2b's `Template`: callable as a factory (`Template()`) and a class with statics
  // (`Template.build(...)`). Statics are bound so `const { build } = Template` still works.
  const bound = new Map<PropertyKey, Function>()
  return new Proxy(cls, {
    apply(target, _this, args: [TemplateOptions?]) {
      const opts = { ...(args[0] ?? {}) }
      if (opts.fileContextPath === undefined) {
        const dir = callerDirectory()
        if (dir) opts.fileContextPath = dir
      }
      return new target(opts)
    },
    get(target, prop, receiver) {
      const value = Reflect.get(target, prop, receiver)
      if (prop === 'prototype' || typeof value !== 'function') return value
      let fn = bound.get(prop)
      if (!fn) {
        fn = value.bind(target)
        if ((value as any)[UNSUPPORTED]) (fn as any)[UNSUPPORTED] = (value as any)[UNSUPPORTED]
        bound.set(prop, fn!)
      }
      return fn
    },
  }) as T & ((options?: TemplateOptions) => ReturnType<TemplateBase['skipCache']> & AgentCoreTemplateBase)
}

/** Drop-in for e2b's `Template` (call it to start a builder; use its statics to build). */
export const Template = callableTemplate(AgentCoreTemplateBase)

// ---------------------------------------------------------------------- helpers

/**
 * Steps 1-5: Dockerfile -> context zip -> S3 -> CodeBuild arm64 -> ECR. Non-blocking.
 *
 * Returns the CodeBuild build id and the image URI. Exported for callers that only need an
 * image (e.g. for a plain AgentCore Runtime) and no Harness; includeStartCmd bakes the start
 * command in as ENTRYPOINT, which such an image needs.
 */
export async function startImageBuild(
  cfg: Config, template: TemplateClass, alias: string,
  opts: { skipCache?: boolean; log?: (msg: string) => void; includeStartCmd?: boolean; withServer?: boolean } = {},
): Promise<{ buildId: string; image: string }> {
  const log = opts.log ?? (() => {})
  // Steps 2-3 first: E2B instruction list -> Dockerfile -> context zip. Done before any AWS
  // call so an invalid template fails without touching the account.
  const dockerfile = renderDockerfile(template, opts.includeStartCmd, cfg.dockerhubMirror, opts.withServer)
  const body = zipContext(template, dockerfile, opts.withServer ? { [SERVER_FILE]: SERVER_GO } : undefined)

  // Step 1: one-time infra (no-op after the first build).
  await ensureInfra(cfg, log)

  // Unique, sortable image tag per build, e.g. ubuntu24-base-20261015092700.
  const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14)
  const image = `${await cfg.registry()}/${cfg.ecrRepo}:${alias.replace(/[^a-zA-Z0-9_.-]/g, '-')}-${ts}`

  // Step 4: upload the context. The bucket lifecycle deletes it after 7 days.
  const bucket = await cfg.bucket()
  const key = `contexts/${alias}/${randomUUID().replace(/-/g, '')}.zip`
  await cfg.s3.send(new PutObjectCommand({ Bucket: bucket, Key: key, Body: body }))
  log(`uploaded build context (${body.length} bytes) to s3://${bucket}/${key}`)

  // Step 5: start CodeBuild on this zip. These env vars fill the $VARS in BUILDSPEC.
  const build = (await cfg.codebuild.send(new StartBuildCommand({
    projectName: cfg.codebuildProject, sourceTypeOverride: 'S3', sourceLocationOverride: `${bucket}/${key}`,
    environmentVariablesOverride: [
      { name: 'IMAGE', value: image }, //                       where to push
      { name: 'REGISTRY', value: await cfg.registry() }, //     what to docker-login to
      { name: 'DOCKERFILE', value: DOCKERFILE_NAME }, //        generated Dockerfile in the zip
      { name: 'NO_CACHE', value: opts.skipCache ? '--no-cache' : '' },
    ],
  }))).build!
  log(`CodeBuild ${build.id} started (native arm64) -> ${image}`)
  return { buildId: build.id!, image }
}

export async function loadTemplate(cfg: Config, alias: string): Promise<TemplateRecord | undefined> {
  return cfg.getJson<TemplateRecord>(templateKey(alias))
}

function lastArg(template: TemplateClass, kind: string): string | undefined {
  const vals = internals(template).instructions.filter((i) => i.type === kind).map((i) => i.args[0])
  return vals.at(-1)
}

/**
 * E2B instructions -> Dockerfile text.
 *
 * Like e2b's toDockerfile, but keeps COPY's owner/mode (--chown/--chmod, needs BuildKit) and
 * quotes ENV values. The start command is left out by default because Sandbox.create runs it
 * (E2B semantics); includeStartCmd bakes it in as ENTRYPOINT instead.
 */
/**
 * Docker official image (`ubuntu:24.04`, `library/python:3.12`) -> same image on `mirror`.
 * Anything with a registry host or a Docker Hub namespace (`user/repo`) is unchanged: the
 * mirror only carries official images.
 */
export function mirrorImage(image: string, mirror: string): string {
  if (!mirror) return image
  const img = image.startsWith('library/') ? image.slice('library/'.length) : image
  return img.includes('/') ? img : `${mirror.replace(/\/+$/, '')}/${img}`
}

/**
 * withServer (platform V2) puts a builder stage in front that compiles SERVER_GO, then copies
 * the binary into the final image and makes it the ENTRYPOINT. The template's own start
 * command is still run by Sandbox.create, not baked in.
 */
export function renderDockerfile(template: TemplateClass, includeStartCmd = false, mirror = '', withServer = false): string {
  const t = internals(template)
  if (t.baseTemplate !== undefined)
    throw new TemplateError('fromTemplate() bases live in E2B\'s cloud; use a Docker image or Dockerfile')
  if (t.baseImage === undefined)
    throw new TemplateError('no base image: call fromImage / fromUbuntuImage / fromDockerfile first')
  const lines: string[] = []
  if (withServer)
    lines.push(`FROM ${mirrorImage(GO_IMAGE, mirror)} AS e2b_agentcore_server`, `COPY ${SERVER_FILE} /src/main.go`,
      "RUN cd /src && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /server main.go")
  lines.push(`FROM ${mirrorImage(t.baseImage, mirror)}`)
  for (const { type, args } of t.instructions) {
    if (type === 'RUN') {
      // args[1], when present, is the user the step should run as.
      if (args[1]) lines.push(`USER ${args[1]}`, `RUN ${args[0]}`, 'USER root')
      else lines.push(`RUN ${args[0]}`)
    } else if (type === 'COPY') {
      const [src, dest, owner, mode] = args
      lines.push(`COPY ${owner ? `--chown=${owner} ` : ''}${mode ? `--chmod=${mode} ` : ''}${src} ${dest}`)
    } else if (type === 'ENV') {
      const pairs: string[] = []
      for (let i = 0; i < args.length; i += 2) pairs.push(`${args[i]}=${envQuote(args[i + 1])}`)
      lines.push(`ENV ${pairs.join(' ')}`)
    } else { // WORKDIR, USER
      lines.push(`${type} ${args.join(' ')}`)
    }
  }
  // The Harness runs commands as root regardless; end as root so in-image tooling matches.
  // (e2b's Dockerfile parser appends `USER user` / `WORKDIR /home/user`, E2B's default
  // sandbox user, which a stock image doesn't have.)
  lines.push('USER root')
  if (withServer) lines.push(`COPY --from=e2b_agentcore_server /server ${SERVER_BIN}`, `ENTRYPOINT ["${SERVER_BIN}"]`, 'CMD []')
  else if (includeStartCmd && t.startCmd) lines.push(`ENTRYPOINT ${t.startCmd}`)
  return lines.join('\n') + '\n'
}

/**
 * Literal ENV value. Docker expands $VAR even inside double quotes, so `$` is escaped too:
 * E2B's setEnvs values are plain strings, not shell expressions.
 */
function envQuote(v: string): string {
  if (/^[A-Za-z0-9_./:@,+-]*$/.test(v)) return v
  return '"' + v.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\$/g, '\\$') + '"'
}

function readDockerignore(ctx: string): string[] {
  const p = path.join(ctx, '.dockerignore')
  if (!fs.existsSync(p)) return []
  return fs.readFileSync(p, 'utf-8').replace(/^\uFEFF/, '').split('\n')
    .filter((l) => !l.startsWith('#')).map((l) => l.trim()).filter(Boolean)
}

/**
 * .dockerignore semantics, same as e2b's matcher: patterns are anchored at the context root
 * (a leading `/` is dropped), a pattern matching a directory excludes everything under it,
 * `!` re-includes, and the last matching pattern wins.
 */
export function isIgnored(rel: string, patterns: string[]): boolean {
  const parts = rel.split('/')
  let ignored = false
  for (const raw of patterns) {
    const negated = raw.startsWith('!')
    const pat = (negated ? raw.slice(1) : raw).trim().replace(/^\/+/, '').replace(/\/+$/, '')
    if (!pat) continue
    if (parts.some((_, i) => minimatch(parts.slice(0, i + 1).join('/'), pat, { dot: true }))) ignored = !negated
  }
  return ignored
}

/**
 * Zip the generated Dockerfile plus only the files that COPY steps reference.
 *
 * Paths resolve against the template's fileContextPath (E2B default: the directory of the
 * file that called Template()), and .dockerignore / fileIgnorePatterns apply.
 */
export function zipContext(template: TemplateClass, dockerfile: string, extra?: Record<string, string>): Uint8Array {
  const t = internals(template)
  const ctx = t.fileContextPath
  const ignore = [...t.fileIgnorePatterns, ...readDockerignore(ctx)]
  const files: Zippable = {}
  for (const ins of t.instructions) {
    if (ins.type !== 'COPY') continue
    const matches = globSync(ins.args[0], { cwd: ctx, dot: true, absolute: true })
    if (!matches.length) throw new TemplateError(`COPY source not found under ${ctx}: ${ins.args[0]}`)
    for (const m of matches) {
      const list = fs.statSync(m).isFile()
        ? [m]
        : globSync('**/*', { cwd: m, dot: true, absolute: true, nodir: true })
      for (const full of list) {
        const rel = path.relative(ctx, full).split(path.sep).join('/')
        if (rel in files) continue
        if (isIgnored(rel, ignore)) continue
        files[rel] = fs.readFileSync(full)
      }
    }
  }
  files[DOCKERFILE_NAME] = new TextEncoder().encode(dockerfile)
  for (const [name, content] of Object.entries(extra ?? {})) files[name] = new TextEncoder().encode(content) // V2 server source
  return zipSync(files, { level: 6 })
}

async function codebuildLogs(cfg: Config, build: Build, offset: number): Promise<LogEntry[]> {
  const g = build.logs?.groupName, s = build.logs?.streamName
  if (!g || !s) return []
  const events: { timestamp?: number; message?: string }[] = []
  let token: string | undefined
  try {
    for (;;) { // read the whole stream; builds are short enough for this
      const r = await cfg.logs.send(new GetLogEventsCommand({
        logGroupName: g, logStreamName: s, ...(token ? { nextToken: token } : { startFromHead: true }),
      }))
      events.push(...(r.events ?? []))
      if (r.nextForwardToken === token || !r.events?.length) break
      token = r.nextForwardToken
    }
  } catch {
    return []
  }
  return events.slice(offset).map((e) => new LogEntry(new Date(e.timestamp ?? Date.now()), 'info', (e.message ?? '').trimEnd()))
}

/**
 * The Harness called `name`, or undefined. A Harness being deleted counts as gone: AgentCore
 * keeps it in DELETING for several minutes, and it can neither run sandboxes nor be deleted again.
 */
export async function findHarness(cfg: Config, name: string) {
  let token: string | undefined
  for (;;) {
    const page = await cfg.control.send(new ListHarnessesCommand(token ? { nextToken: token } : {}))
    const h = page.harnesses?.find((x) => x.harnessName === name && x.status !== 'DELETING')
    if (h) return h as { harnessId: string; harnessName: string; arn?: string }
    token = page.nextToken
    if (!token) return undefined
  }
}

/**
 * Create or update Harness `name` so it runs sandboxes from `image`.
 *
 * model: required by the API, never invoked by this provider.
 * lifecycle: idle timeout and max lifetime for every session of this Harness (Config).
 */
async function upsertHarness(cfg: Config, name: string, image: string): Promise<void> {
  const artifact = { containerConfiguration: { containerUri: image } }
  const common = {
    executionRoleArn: cfg.execRoleArn,
    model: { bedrockModelConfig: { modelId: cfg.modelId } },
    environment: {
      agentCoreRuntimeEnvironment: {
        lifecycleConfiguration: { idleRuntimeSessionTimeout: cfg.idleTimeoutS, maxLifetime: cfg.maxLifetimeS },
      },
    },
  }
  const existing = await findHarness(cfg, name)
  if (existing) {
    // UpdateHarness wraps the artifact in `optionalValue` (CreateHarness does not).
    await cfg.control.send(new UpdateHarnessCommand({
      harnessId: existing.harnessId, ...common, environmentArtifact: { optionalValue: artifact },
    } as any))
  } else {
    await cfg.control.send(new CreateHarnessCommand({ harnessName: name, ...common, environmentArtifact: artifact } as any))
  }
}

// ---------------------------------------------------------------------- V2 (plain Runtime)

/**
 * The V2 Runtime of template `alias`, or undefined. Only the runtime our template record
 * points at counts, so an unrelated runtime that happens to be called e2b_<alias> is never
 * touched. Before the record exists (first build still deploying) it falls back to the name.
 */
async function findV2Runtime(cfg: Config, alias: string): Promise<{ agentRuntimeId: string } | undefined> {
  const rec = await loadTemplate(cfg, alias)
  if (!rec) return runtimeByName(cfg, harnessName(alias))
  if (rec.platform_version !== 'V2' || !rec.agent_runtime_id) return undefined
  try {
    const r = await cfg.control.send(new GetAgentRuntimeCommand({ agentRuntimeId: rec.agent_runtime_id }))
    return r.status === 'DELETING' ? undefined : { agentRuntimeId: r.agentRuntimeId! }
  } catch {
    return undefined
  }
}

async function runtimeByName(cfg: Config, name: string) {
  let token: string | undefined
  for (;;) {
    const page = await cfg.control.send(new ListAgentRuntimesCommand({ maxResults: 100, ...(token ? { nextToken: token } : {}) }))
    const r = page.agentRuntimes?.find((x) => x.agentRuntimeName === name && x.status !== 'DELETING')
    if (r) return r as { agentRuntimeId: string; agentRuntimeName: string }
    token = page.nextToken
    if (!token) return undefined
  }
}

/**
 * Create or update Runtime `name` (platformVersion=V2) so it runs `image`. roleArn /
 * network / lifecycle are the same settings a V1 Harness gets from Config. V2 snapshots the
 * microVM at deploy time (the :8080 server must be up for that), and every sandbox session
 * is restored from that snapshot.
 */
async function upsertV2Runtime(cfg: Config, name: string, image: string): Promise<void> {
  const spec = {
    agentRuntimeArtifact: { containerConfiguration: { containerUri: image } },
    roleArn: cfg.execRoleArn, networkConfiguration: { networkMode: 'PUBLIC' },
    protocolConfiguration: { serverProtocol: 'HTTP' },
    lifecycleConfiguration: { idleRuntimeSessionTimeout: cfg.idleTimeoutS, maxLifetime: cfg.maxLifetimeS },
    platformVersion: 'V2',
  }
  const existing = await runtimeByName(cfg, name)
  if (existing) await cfg.control.send(new UpdateAgentRuntimeCommand({ agentRuntimeId: existing.agentRuntimeId, ...spec } as any))
  else await cfg.control.send(new CreateAgentRuntimeCommand({ agentRuntimeName: name, ...spec } as any))
}

/** getBuildStatus steps 6-7 for platform V2. */
async function runtimeStatus(
  cfg: Config, rec: BuildRecord, data: Pick<BuildInfo, 'templateId' | 'buildId'>, entries: LogEntry[],
  resp: (status: TemplateBuildStatusResponse['status'], reason?: string) => TemplateBuildStatusResponse,
): Promise<TemplateBuildStatusResponse> {
  if (!rec.harness_requested) {
    await upsertV2Runtime(cfg, rec.harness_name, rec.image)
    rec.harness_requested = true
    await cfg.putJson(buildKey(data.buildId), rec)
    entries.push(new LogEntry(new Date(), 'info', `image pushed; deploying V2 Runtime ${rec.harness_name}`))
    return resp('building')
  }
  const summary = await runtimeByName(cfg, rec.harness_name)
  const r = summary && (await cfg.control.send(new GetAgentRuntimeCommand({ agentRuntimeId: summary.agentRuntimeId })))
  if (!r) return resp('error', `Runtime ${rec.harness_name} disappeared`)
  if (String(r.status).endsWith('FAILED')) return resp('error', `Runtime ${r.status}: ${r.failureReason ?? ''}`)
  const uri = (r.agentRuntimeArtifact as any)?.containerConfiguration?.containerUri
  if (r.status !== 'READY' || uri !== rec.image) return resp('building')
  if ((r as any).platformVersion !== 'V2')
    return resp('error', `Runtime ${r.agentRuntimeName} is ${(r as any).platformVersion}, not V2`)
  const tpl: TemplateRecord = {
    alias: rec.alias, platform_version: 'V2', agent_runtime_name: r.agentRuntimeName!,
    agent_runtime_id: r.agentRuntimeId!, agent_runtime_arn: r.agentRuntimeArn!, image: rec.image,
    start_cmd: rec.start_cmd, ready_cmd: rec.ready_cmd, workdir: rec.workdir, build_id: data.buildId, built_at: now(),
  }
  await cfg.putJson(templateKey(rec.alias), tpl)
  entries.push(new LogEntry(new Date(), 'info', `V2 Runtime ${r.agentRuntimeName} READY (${r.agentRuntimeArn})`))
  return resp('ready')
}
