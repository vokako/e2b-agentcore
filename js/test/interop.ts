/**
 * Cross-language helper, driven by tests/test_full.py (Python) and test/full.ts (JS).
 * Prints one JSON line on stdout.
 *
 *   node dist/test/interop.js connect <sandboxId> <path>   read <path> and $GREETING, write /work/from-js.txt
 *   node dist/test/interop.js create <template>            create a sandbox (FROM=js), write /work/from-js.txt
 */
import { Sandbox } from '../src/index.js'

const [cmd, a, b] = process.argv.slice(2)
if (cmd === 'connect') {
  const sbx = await Sandbox.connect(a)
  const read = await sbx.files.read(b)
  const env = (await sbx.commands.run('echo -n $GREETING')).stdout
  await sbx.files.write('/work/from-js.txt', 'hello from js')
  console.log(JSON.stringify({ read, env }))
} else if (cmd === 'create') {
  const sbx = await Sandbox.create(a, { envs: { FROM: 'js' } })
  await sbx.files.write('/work/from-js.txt', 'made by js')
  console.log(JSON.stringify({ id: sbx.sandboxId }))
} else {
  console.error('usage: interop.js connect <id> <path> | create <template>')
  process.exit(2)
}
