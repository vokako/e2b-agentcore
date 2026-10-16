/**
 * AgentCore provider for the official E2B JS/TS SDK.
 *
 * E2B's SDK has no provider/plugin interface: its `Sandbox` always talks to E2B's cloud (or a
 * self-hosted E2B via E2B_DOMAIN). This package plugs in at the import instead:
 *
 *   import { Sandbox, Template } from 'e2b-agentcore'   // instead of: from 'e2b'
 *
 * Everything else (error classes, FileType, LogEntry, waitForPort, defaultBuildLogger, Git,
 * ...) IS the official e2b package, re-exported here, so return values and instanceof
 * checks match. Unlike the Python package there is no install() switch: ES module exports
 * are read-only, so `e2b`'s own `Sandbox` cannot be swapped at runtime.
 *
 * `npm run coverage` prints the method-by-method support table.
 */
export * from 'e2b'

export { Config, ensureInfra, BUILDSPEC, type ConfigOpts } from './config.js'
export { NotSupportedError } from './errors.js'
export {
  Template, AgentCoreTemplateBase, startImageBuild, renderDockerfile, harnessName,
  type AgentCoreOpts, type BuildOpts, type TemplateRecord,
} from './template.js'
export {
  Sandbox, Commands, CommandHandle, Filesystem, SandboxPaginator,
  type SandboxOpts, type SandboxListOpts, type CommandStartOpts, type WriteEntry,
} from './sandbox.js'

import { Sandbox } from './sandbox.js'
export default Sandbox
