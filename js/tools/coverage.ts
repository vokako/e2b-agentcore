/**
 * Every public method of the official e2b JS SDK and whether e2b-agentcore supports it.
 * tools/api-coverage.ts prints this as the README table; test/full.ts requires a test per row.
 *
 * The method list is read from the INSTALLED e2b package (an offline e2b Sandbox instance;
 * CommandHandle from e2b's index.d.ts because the class is not exported), so when E2B adds
 * a method it shows up here as "missing" until the provider handles it.
 */
import fs from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import * as e2b from 'e2b'
import * as ac from '../src/index.js'
import { UNSUPPORTED } from '../src/errors.js'

// Supported, but behaves differently from E2B in a way callers should know.
const PARTIAL: Record<string, string> = {
  'Sandbox.create': 'timeoutMs/network/allowInternetAccess/lifecycle/mcp/volumeMounts accepted, no effect (per Harness)',
  'Sandbox.setTimeout': 'no effect; idle timeout and max lifetime are per Harness',
  'static Sandbox.setTimeout': 'no effect; idle timeout and max lifetime are per Harness',
  'Sandbox.getMetrics': 'one live sample, not a time series',
  'static Sandbox.getMetrics': 'one live sample, not a time series',
  'static Sandbox.list': 'from the S3 registry; reaped sandboxes drop out on the next connect/isRunning',
  'Sandbox.isRunning': 'probes the microVM (~0.4 s), not a control-plane lookup',
  'Sandbox.git': 'official e2b Git helper over our commands (E2B marks it deprecated)',
  'Commands.run': 'user defaults to root; stdin unsupported; background output arrives in ~1 s chunks; ' +
    'V1 drops any single output line over ~64 KB; big outputs (~20k+ lines) can lose their ' +
    'tail (V1 and V2), and on V2 lines of a large output can arrive out of order; on V2 a ' +
    'foreground command with timeout over ~1 h (or 0) runs as background, output in ~1 s chunks',
  'Commands.list': 'background commands started through this SDK only (as E2B: envd-started only)',
  'Filesystem.write': 'base64 over commands, ~100 KB/s; gzip/useOctetStream ignored',
  'Filesystem.read': "read in verified 192 KB chunks (one call each); format 'stream' yields one chunk",
  'static Template.build': 'cpuCount/memoryMB/minFreeDiskMb ignored; tags unsupported',
  'static Template.buildInBackground': 'same as build',
}
// e2b internals that are not part of the public API.
const SKIP = new Set(['resolveApiOpts', 'resolveOpts', 'boundOpts', 'createSandbox', 'forkSandbox', 'connectSandbox',
  'fileUrl', 'start', 'createHandle', 'connectionConfig', 'envdPort', 'mcpPort', 'envdAccessToken', 'envdApiUrl',
  'envdDirectUrl', 'envdApi', 'mcpToken', 'trafficAccessToken', 'length', 'name', 'prototype'])

const names = (o: object) => Object.getOwnPropertyNames(o).filter((n) => n !== 'constructor' && !n.startsWith('_') && !SKIP.has(n))
function statics(c: Function): string[] {
  const out = new Set<string>()
  for (let k: any = c; k && k !== Function.prototype; k = Object.getPrototypeOf(k)) names(k).forEach((n) => out.add(n))
  return [...out]
}

const off = new e2b.Sandbox({ sandboxId: 'x', sandboxDomain: 'e2b.app', envdVersion: '0.4.0', apiKey: 'e2b_0' } as any)
const dts = fs.readFileSync(path.join(path.dirname(createRequire(import.meta.url).resolve('e2b')), 'index.d.ts'), 'utf-8')
const handleBlock = dts.slice(dts.indexOf('declare class CommandHandle'))
const handleMembers = [...handleBlock.slice(0, handleBlock.indexOf('\n}')).matchAll(/^ {2}(?:get |readonly )?([a-zA-Z]+)[(:]/gm)]
  .map((m) => m[1]).filter((n) => n !== 'constructor')

const ourSbx = new ac.Sandbox(new ac.Config({ region: 'us-east-2' }), {
  sandbox_id: 'x', template: 't', harness_arn: 'arn', metadata: {}, started_at: 0, end_at: 0,
})

/** "yes" | "no" (stub with reason) | "missing". */
function status(obj: any, name: string): [string, string] {
  let v: any
  try {
    v = obj[name]
  } catch (err) {
    return err instanceof ac.NotSupportedError ? ['no', 'see message'] : ['yes', '']
  }
  if (v === undefined && !(name in obj)) return ['missing', '']
  if (typeof v === 'function' && v[UNSUPPORTED]) return ['no', v[UNSUPPORTED]]
  return ['yes', '']
}

export type Row = [name: string, status: string, note: string]

function collect(): Row[] {
const rows: Row[] = []
const add = (label: string, list: string[], obj: any) =>
  [...new Set(list)].sort().forEach((n) => rows.push([`${label}.${n}`, ...status(obj, n)]))
add('static Sandbox', statics(e2b.Sandbox), ac.Sandbox)
add('Sandbox', [...names(e2b.Sandbox.prototype), ...Object.keys(off).filter((n) => !SKIP.has(n))], ourSbx)
add('Commands', names(Object.getPrototypeOf(off.commands)), ourSbx.commands)
add('CommandHandle', handleMembers.filter((n) => !/^(handle|events|on[A-Z]|checkHealth|stdoutDecoder|stderrDecoder|result|iterationError|disconnected)/.test(n)),
  ac.CommandHandle.prototype)
add('Filesystem', names(Object.getPrototypeOf(off.files)), ourSbx.files)
add('static Template', ['build', 'buildInBackground', 'getBuildStatus', 'exists', 'aliasExists', 'assignTags', 'removeTags', 'getTags'],
  ac.Template)
rows.push(['Template().fromImage / aptInstall / copy / runCmd / set* ...', 'yes', 'official e2b builder, inherited'])

// CommandHandle instance fields (pid, stdout, ...) are getters on our prototype or set in the
// constructor; `pid` is a constructor field, so check it by name.
for (const r of rows) if (r[0] === 'CommandHandle.pid' && r[1] === 'missing') r.splice(1, 2, 'yes', '')
return rows.map(([name, s, why]): Row => (s === 'yes' && PARTIAL[name] ? [name, 'partial', PARTIAL[name]] : [name, s, why]))
}

/** Table rows as [name, status, note]; status is yes / partial / no / missing. */
export const coverageRows: () => Row[] = collect

export function e2bVersion(): string {
  const dir = path.dirname(createRequire(import.meta.url).resolve('e2b'))
  return JSON.parse(fs.readFileSync(path.join(dir, '..', 'package.json'), 'utf-8')).version
}
