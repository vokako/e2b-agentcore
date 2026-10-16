/**
 * Print the support table: every public method of the installed e2b package and whether
 * e2b-agentcore supports it (rows come from tools/coverage.ts).
 *
 *   npm run coverage            # markdown table on stdout
 */
import { coverageRows, e2bVersion } from './coverage.js'

const rows = coverageRows()
console.log(`e2b ${e2bVersion()}\n`)
console.log('| E2B method | AgentCore | Notes |\n|---|---|---|')
const counts: Record<string, number> = { yes: 0, partial: 0, no: 0, missing: 0 }
for (const [name, s, why] of rows) {
  counts[s]++
  console.log(`| \`${name}\` | ${s} | ${why} |`)
}
console.log(`\n${JSON.stringify(counts)}`)
