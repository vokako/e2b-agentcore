# e2b-agentcore (JS/TS)

AgentCore provider for the official [E2B JS/TS SDK](https://www.npmjs.com/package/e2b): build E2B
templates into Amazon Bedrock AgentCore Harnesses and run sandboxes on them. It is the TypeScript
counterpart of the Python package in the repo root and shares its AWS resources, S3 records and
in-sandbox layout, so a template built from Python can be used from TypeScript and the other way round.

## How it plugs into E2B

The E2B SDK has no provider interface. Change the import and keep the rest of the code:

```ts
import { Sandbox, Template } from 'e2b-agentcore'   // was: from 'e2b'
```

- `Template` extends e2b's own `TemplateBase`. Every builder method (`fromDockerfile`,
  `fromUbuntuImage`, `aptInstall`, `copy`, `setStartCmd`, ...) is e2b's code; only `build`,
  `buildInBackground`, `getBuildStatus`, `exists` and `aliasExists` are replaced.
- Error classes, `FileType`, `LogEntry`, `waitForPort`, `defaultBuildLogger` and `Git` are re-exported
  from e2b, so `instanceof CommandExitError` and friends keep working.
- There is no `install()` switch like the Python package has: ES module exports are read-only, so
  e2b's own `Sandbox` export cannot be swapped at runtime.

## Quick start

Requires Node >= 22.19 (same as e2b 2.x), AWS credentials (the normal AWS SDK chain, e.g.
`AWS_PROFILE`), and a Harness execution role with ECR pull and CloudWatch Logs write.

```bash
cd js && npm install && npm run build
export E2B_AGENTCORE_EXEC_ROLE_ARN=arn:aws:iam::<account>:role/<harness-exec-role>
```

```ts
import { Sandbox, Template, waitForPort, defaultBuildLogger } from 'e2b-agentcore'

// Either the customer Dockerfile ...
await Template.build(Template().fromDockerfile('Dockerfile'), 'ubuntu24-base', { onBuildLogs: defaultBuildLogger() })
// ... or E2B's template DSL
const t = Template().fromUbuntuImage('24.04').aptInstall(['python3', 'iproute2'])
  .setWorkdir('/work').setStartCmd('python3 -m http.server 8000', waitForPort(8000))
await Template.build(t, 'dsl-demo')

await using sbx = await Sandbox.create('dsl-demo', { envs: { FOO: '1' }, metadata: { user: 'alice' } })
console.log((await sbx.commands.run('echo $FOO; pwd')).stdout)
await sbx.files.write('/work/a.txt', 'hi')
const h = await sbx.commands.run('sleep 2; echo done', { background: true })
console.log((await h.wait()).stdout)
```

Configuration is the same `E2B_AGENTCORE_*` environment variables as the Python package (region,
execution role, model id, resource prefix, idle timeout, max lifetime), or pass
`{ config: new Config({...}) }` to any call. See `src/config.ts` for the list and for every AWS
resource the first build creates.

## Method support

`npm run coverage` prints the full table, generated from the installed `e2b` 2.56.0:
36 supported, 13 supported with differences, 27 not supported, 0 missing.

- **Supported:** `Sandbox.create`, `connect`, `kill`, `getInfo`, `list`, `isRunning`, `getMetrics`;
  all of `commands` including background commands and `connect(pid)`; all of `files` except
  `watchDir`; `git`; `Template.build`, `buildInBackground`, `getBuildStatus`, `exists`; `await using`.
- **Not supported** (throw `NotSupportedError` with the reason): `pause`, snapshots, `fork`,
  `getHost`, upload/download URLs, `updateNetwork`, MCP, stdin, `pty`, `watchDir`, template tags.
- **Accepted but no effect** (warning): per-sandbox `timeoutMs`, network, lifecycle, and `setTimeout`.
  AgentCore sets these once per Harness.

Differences from the Python package: `CommandHandle` starts following a background command's output
as soon as it is created (as e2b's JS handle does), so `onStdout`/`onStderr` fire without calling
`wait()`; and `runCmd(cmd, { user })` steps are rendered as `USER <user>` around the `RUN`.

## Files

- `src/template.ts`: the 7 build steps, with comments at each step
- `src/sandbox.ts`: sandbox, commands, files and the InvokeAgentRuntimeCommand transport
- `src/config.ts`: configuration, the CodeBuild buildspec and one-time infrastructure
- `test/e2e.ts`: `offline`, `build` and `run` test stages
- `tools/api-coverage.ts`: the support table
- `examples/build-and-run.ts`
