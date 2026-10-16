/**
 * Build the customer Dockerfile into an AgentCore Harness and run a sandbox on it.
 *
 *   export E2B_AGENTCORE_EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/<harness-exec-role>
 *   npm run build && node dist/examples/build-and-run.js
 *
 * The only difference from E2B code is the import.
 */
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { Sandbox, Template, defaultBuildLogger } from '../src/index.js' // in your code: from 'e2b-agentcore'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..')

await Template.build(Template().fromDockerfile(path.join(root, 'Dockerfile.fixed')), 'ubuntu24-base', {
  onBuildLogs: defaultBuildLogger(),
})

const sbx = await Sandbox.create('ubuntu24-base', { envs: { GREETING: 'hi' } })
try {
  console.log((await sbx.commands.run('uname -m; git --version; echo $GREETING')).stdout)
  await sbx.files.write('/root/work/hello.txt', 'hello from TypeScript\n')
  console.log(await sbx.files.read('/root/work/hello.txt'))
} finally {
  await sbx.kill()
}
