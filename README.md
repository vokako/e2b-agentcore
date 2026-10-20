# e2b_agentcore: an AgentCore provider for the official E2B SDK

Write E2B code and run it on Amazon Bedrock AgentCore. Templates are built from a Dockerfile
(or E2B's template DSL) into an arm64 image and deployed as an AgentCore **Harness**; each
sandbox is one AgentCore Runtime session (microVM).

Two SDKs, same AWS resources and records, so templates and sandboxes are shared between them:
- **Python**: this directory (`e2b_agentcore/`), on top of the `e2b` Python package.
- **JS/TS**: [`js/`](js/README.md), on top of the `e2b` npm package.

## How it plugs into E2B

The official `e2b` SDK has no provider/plugin interface: `e2b.Sandbox` always talks to E2B's
cloud, or to a self-hosted E2B via `E2B_DOMAIN`. This package plugs in at the Python level:

```python
# Option 1: change the import (recommended)
from e2b_agentcore import Sandbox, Template          # was: from e2b import Sandbox, Template

# Option 2: keep existing code untouched
import e2b_agentcore; e2b_agentcore.install()         # once, before `from e2b import ...`
from e2b import Sandbox, Template                     # now the AgentCore versions
```

It reuses the official package wherever it can, so code behaves the same:
- `Template` subclasses `e2b.Template`. Every builder method (`from_dockerfile`, `from_ubuntu_image`,
  `apt_install`, `pip_install`, `copy`, `run_cmd`, `set_envs`, `set_workdir`, `set_start_cmd`, ...)
  is E2B's own code. Only `build` / `build_in_background` / `get_build_status` / `exists` are replaced.
- Return values and exceptions are the official classes: `e2b.CommandResult`, `e2b.EntryInfo`,
  `e2b.SandboxInfo`, `e2b.CommandExitException`, `e2b.FileNotFoundException`, `e2b.TimeoutException`, ...
- `sbx.git` is E2B's own `Git` helper running over this provider's `commands`.
- E2B features AgentCore can't do raise `e2b_agentcore.NotSupportedException` with the reason.

## Quick start

Requires Python >= 3.10, `pip install -r requirements.txt`, AWS credentials, and a Harness execution role
(ECR pull + CloudWatch Logs).

```python
from e2b_agentcore import Sandbox, Template, wait_for_port, default_build_logger

# Either the customer Dockerfile ...
Template.build(Template().from_dockerfile("Dockerfile"), "ubuntu24-base", on_build_logs=default_build_logger())
# ... or E2B's template DSL
t = (Template().from_ubuntu_image("24.04").apt_install(["python3", "iproute2"])
     .set_workdir("/work").set_start_cmd("python3 -m http.server 8000", wait_for_port(8000)))
Template.build(t, "dsl-demo")

with Sandbox.create("dsl-demo", envs={"FOO": "1"}, metadata={"user": "alice"}) as sbx:
    print(sbx.commands.run("uname -m; echo $FOO").stdout)
    h = sbx.commands.run("sleep 2; echo done", background=True)
    print(h.wait().stdout)
    sbx.files.write("/work/a.txt", "hi")
    print(sbx.files.list("/work"))
```

```bash
export E2B_AGENTCORE_EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/<harness-exec-role>
python examples/build_and_run.py Dockerfile.fixed ubuntu24-base
python tests/e2e.py build && python tests/e2e.py run     # quick end-to-end smoke test
python tests/test_full.py all                            # full test: every support-table row (~10 min)
python tools/api_coverage.py                             # support table below, from the installed e2b
```

`tests/test_full.py` is driven by the support table: `tools/api_coverage.py` generates the table
from the installed `e2b`, every check names the rows it covers, and the run fails if any row has no
check. Each check tests what its row says: `yes` works as in E2B, `partial` shows the difference
written in the Notes column, `no` raises `NotSupportedException`. Stages: `offline` (no resources
created), `build` (templates `full-py`, a deliberately broken one, and one that is deleted again),
`run` (sandboxes). With `js/dist` built it also checks that Python and JS share templates and
sandboxes. Results for both platforms are under "Platform V1 or V2".

Env vars: `E2B_AGENTCORE_REGION` (default us-east-2), `E2B_AGENTCORE_EXEC_ROLE_ARN` (required for builds),
`E2B_AGENTCORE_MODEL_ID` (the Harness API requires one; never called), `E2B_AGENTCORE_PREFIX`
(default `e2b-agentcore`), `E2B_AGENTCORE_IDLE_TIMEOUT` / `E2B_AGENTCORE_MAX_LIFETIME` (900 / 28800 s, per Harness),
`E2B_AGENTCORE_PLATFORM_VERSION` (`V1` default, or `V2`; see below).

## Platform V1 or V2

AgentCore Runtime has two platform versions. V2 restores every session from a MicroVM snapshot
taken at deploy time. Pick one per build:

```python
Template.build(t, "my-sandbox", config=Config(platform_version="V2"))
# or: export E2B_AGENTCORE_PLATFORM_VERSION=V2
```

`Sandbox` code doesn't change: `Sandbox.create("my-sandbox")` reads the platform from the template.

| | V1 (default) | V2 |
|---|---|---|
| Deployed as | Harness `e2b_<alias>` (its backing runtime is V1) | Runtime `e2b_<alias>` with `platformVersion=V2` |
| Image changes | none | the build adds a static Go server (`/opt/e2b-agentcore/server`, ENTRYPOINT) answering `/ping` and `/invocations` on :8080, and reaping orphan processes as PID 1 |
| Rebuild to READY (`full-py`, measured 2026-10-20) | 88-101 s | 331-333 s (V2 takes the snapshot at deploy) |
| First command in a new session (cold start) | 7.6-8.7 s | 1.7-2.4 s |
| `Sandbox.create` incl. start command + ready check | 11.2 s | 4.7 s (21-23 s for the first sandbox right after a deploy) |
| Later commands | ~0.33 s | ~0.27 s |
| One command call | no timeout cap | timeout capped at 3600 s; longer foreground commands run as background commands transparently |
| Command output | lines over ~64 KB dropped | no line limit; lines of a large output can arrive out of order (`seq 1 1000`: 1 of 8 runs, `seq 1 20000`: 5 of 8) |

Why V2 is not a Harness: `CreateHarness` / `UpdateHarness` have no platform field, and a Harness's
backing runtime is always V1. A plain Runtime needs an HTTP server in the image, hence the added
server stage. A template keeps the platform it was first built with; to switch, `Template.delete`
it and build again (the SDK refuses a cross-platform rebuild before starting CodeBuild).

On both platforms AgentCore can also lose the last lines of a very large output (seen with
`seq 1 20000`). The SDK's own reads (`files.read`, background-command output) are immune: each
line is numbered inside the microVM, reordered and counted on arrival, and refetched if incomplete.
Output of your own `commands.run` can't be verified that way; for large output, write it to a
file and `files.read` it.

Both suites run on V2 with `E2B_AGENTCORE_PLATFORM_VERSION=V2` (templates get a `-v2` suffix).
Last runs (2026-10-20, us-east-2), all with 0 failures and every table row covered:

| Suite | V1 | V2 |
|---|---|---|
| Python `tests/test_full.py all` | 141 passed | 141 passed |
| JS `npm run test:full -- all` | 151 passed | 151 passed |

## What `Template.build` does

1. First build only: create S3 bucket `<prefix>-src-<account>-<region>`, ECR repo `<prefix>/templates`,
   CodeBuild role `<prefix>-codebuild` and ARM project `<prefix>-builder`.
2. Turn the E2B instructions into a Dockerfile. The start command is left out: `Sandbox.create`
   starts it and polls the ready command, as E2B does.
3. Zip the Dockerfile plus only the files that `COPY` steps reference (`.dockerignore` honoured).
4. Upload to S3 (`contexts/`, expires after 7 days).
5. CodeBuild: native arm64 `docker build`, push `<registry>/<prefix>/templates:<alias>-<timestamp>`.
6. V1: create or update Harness `e2b_<alias>` with that image. V2: create or update Runtime
   `e2b_<alias>` with `platformVersion=V2`. Wait until it is READY on the new image.
7. Write `templates/<alias>.json` (start/ready command, workdir) for `Sandbox.create`.

Each step is commented in `e2b_agentcore/template.py`; the buildspec and the one-time infra are in
`e2b_agentcore/_config.py`. `runtime/build.py` reuses steps 1-5 to build an image for a plain
AgentCore Runtime (which needs its own HTTP server on :8080, see `runtime/`).

## How sandboxes map

- `sandbox_id` is the `runtimeSessionId`. Every command and file operation is one
  `InvokeAgentRuntimeCommand` call (root shell, streamed stdout/stderr, exit code).
- AgentCore has no session list or metadata, so `s3://<bucket>/sandboxes/<id>.json` holds template,
  metadata and start time. Envs are kept only inside the microVM (`/tmp/.e2b/env`), never in S3.
- Invoking a reaped session id on AgentCore silently boots a new, empty microVM. `connect()` and
  `is_running()` detect this with an id marker file and raise `SandboxNotFoundException`, as E2B would.

## Test results (2026-10-15, us-east-2)

`tests/e2e.py`: 36 passed, 0 failed. Also checked: reaped-session detection; `runtime/build.py` image
redeployed to Runtime `sbx_ubuntu24` and answering.

| Step | Time |
|---|---|
| Build `ubuntu24-base` (customer Dockerfile) to Harness READY | 102 s |
| `Sandbox.create` including start command + ready check | 6.7 s |
| One command | about 0.3-0.4 s |
| `files.write` 200 KB | 3.1 s |

Found while testing: on V1, AgentCore's command output stream silently drops any single line longer
than about 64 KB (wrapped output arrives intact). File reads use wrapped base64 for this reason; user
commands that print one huge line will lose it. V2 has no such limit, but see "Platform V1 or V2"
for its ordering issue.

Not provided: `AsyncSandbox` / `AsyncTemplate`, `pty`, and the code-interpreter SDK's `run_code`
(AgentCore Code Interpreter is the native match for that; see the mapping below).

## Method support

Generated by `python tools/api_coverage.py` against the installed `e2b`. "partial" means it works with
the noted difference; "no" raises `NotSupportedException`.

Checked against e2b 2.56.0

| E2B method | AgentCore | Notes |
|---|---|---|
| `Sandbox.beta_pause` | no | no memory snapshot |
| `Sandbox.commands` | yes |  |
| `Sandbox.connect` | yes |  |
| `Sandbox.create` | partial | timeout/network/allow_internet_access/lifecycle/mcp/volume_mounts accepted, no effect (per Harness) |
| `Sandbox.create_snapshot` | no | no VM snapshot API |
| `Sandbox.default_mcp_template` | yes |  |
| `Sandbox.default_sandbox_timeout` | yes |  |
| `Sandbox.default_template` | yes |  |
| `Sandbox.delete_snapshot` | no | no VM snapshot API |
| `Sandbox.download_url` | no | no direct HTTP endpoint into the session; use files.read |
| `Sandbox.files` | yes |  |
| `Sandbox.fork` | no | no VM snapshot API |
| `Sandbox.get_host` | no | AgentCore exposes no inbound port of a session |
| `Sandbox.get_info` | yes |  |
| `Sandbox.get_mcp_token` | no | use AgentCore Gateway for MCP |
| `Sandbox.get_mcp_url` | no | use AgentCore Gateway for MCP |
| `Sandbox.get_metrics` | partial | one live sample, not a time series |
| `Sandbox.git` | partial | official e2b Git helper over our commands (E2B marks it deprecated) |
| `Sandbox.is_running` | partial | probes the microVM (~0.4 s), not a control-plane lookup |
| `Sandbox.kill` | yes |  |
| `Sandbox.list` | partial | from the S3 registry; reaped sandboxes drop out on the next connect/is_running |
| `Sandbox.list_snapshots` | no | no VM snapshot API |
| `Sandbox.mcp_port` | yes |  |
| `Sandbox.pause` | no | no memory snapshot; session storage (Preview) keeps only the filesystem |
| `Sandbox.pty` | no | see message |
| `Sandbox.sandbox_domain` | yes |  |
| `Sandbox.sandbox_id` | yes |  |
| `Sandbox.set_timeout` | partial | no effect; idle timeout and max lifetime are per Harness |
| `Sandbox.update_network` | no | network mode is set per Harness |
| `Sandbox.upload_url` | no | no direct HTTP endpoint into the session; use files.write |
| `Commands.close_stdin` | no | InvokeAgentRuntimeCommand has no stdin channel |
| `Commands.connect` | yes |  |
| `Commands.kill` | yes |  |
| `Commands.list` | partial | background commands started through this SDK only (as E2B: envd-started only) |
| `Commands.run` | partial | user defaults to root; stdin=True unsupported; background output arrives in ~1 s chunks; V1 drops any single output line over ~64 KB; big outputs (~20k+ lines) can lose their tail (V1 and V2), and on V2 lines of a large output can arrive out of order; on V2 a foreground command with timeout over ~1 h (or 0) runs as background, output in ~1 s chunks |
| `Commands.send_stdin` | no | InvokeAgentRuntimeCommand has no stdin channel |
| `CommandHandle.close_stdin` | no | InvokeAgentRuntimeCommand has no stdin channel |
| `CommandHandle.disconnect` | yes |  |
| `CommandHandle.kill` | yes |  |
| `CommandHandle.pid` | yes |  |
| `CommandHandle.send_stdin` | no | InvokeAgentRuntimeCommand has no stdin channel |
| `CommandHandle.wait` | yes |  |
| `Filesystem.exists` | yes |  |
| `Filesystem.get_info` | yes |  |
| `Filesystem.list` | yes |  |
| `Filesystem.make_dir` | yes |  |
| `Filesystem.read` | partial | read in verified 192 KB chunks (one call each); format='stream' yields one chunk |
| `Filesystem.remove` | yes |  |
| `Filesystem.rename` | yes |  |
| `Filesystem.watch_dir` | no | no file-event stream from the microVM; poll files.list instead |
| `Filesystem.write` | partial | base64 over commands, ~100 KB/s; gzip/use_octet_stream ignored |
| `Filesystem.write_files` | yes |  |
| `Template.build` | partial | cpu_count/memory_mb/min_free_disk_mb ignored; tags unsupported |
| `Template.build_in_background` | partial | same as build |
| `Template.get_build_status` | yes |  |
| `Template.exists` | yes |  |
| `Template.alias_exists` | yes |  |
| `Template.assign_tags` | no | a Harness points at one image; rebuild instead |
| `Template.remove_tags` | no | a Harness points at one image; rebuild instead |
| `Template.get_tags` | no | a Harness points at one image; rebuild instead |
| `Template.from_* / apt_install / copy / run_cmd / set_* ...` | yes | official e2b builder, inherited |


## Low-level mapping: E2B API vs AgentCore SDK

Versions checked by introspecting installed SDKs (2026-10-14): `e2b` 2.56.0, `e2b-code-interpreter` 2.11.0,
`bedrock-agentcore` 1.24.1, boto3 1.43.113 service models. Not tested end-to-end against live sessions.

Two AgentCore targets:
- **CI** = Code Interpreter (`bedrock_agentcore.tools.CodeInterpreter`): managed image, no custom image.
- **RT** = Runtime (`InvokeAgentRuntimeCommand`, `AgentCoreRuntimeClient.open_shell`): custom ECR image (arm64), one microVM per `runtimeSessionId`.

| E2B | AgentCore CI | AgentCore RT | Gap |
|---|---|---|---|
| `Sandbox.create(template=...)` | `CodeInterpreter.start(identifier, session_timeout_seconds)` | Session created implicitly by first invoke with a new `runtimeSessionId` | |
| template (`e2b template build`) | none (only network mode / role / certs / filesystem via `create_code_interpreter`) | Build arm64 image → ECR → `CreateAgentRuntime(agentRuntimeArtifact.containerConfiguration.containerUri)` | CI has no custom image |
| `create(envs=...)` | none | `environmentVariables` on the runtime (not per session) | No per-session env |
| `create(timeout=)` / `set_timeout()` | `sessionTimeoutSeconds` 1–28800, set at start only | `lifecycleConfiguration.idleRuntimeSessionTimeout` / `maxLifetime` 60–1209600 s, per runtime | No per-session extend |
| `create(allow_internet_access / network)` | interpreter `networkConfiguration` PUBLIC / SANDBOX / VPC | runtime `networkConfiguration` PUBLIC / VPC | Runtime-level, not per session |
| `Sandbox.connect(id)` | reuse `session_id` + `get_session()` | reuse `runtimeSessionId` | |
| `kill()` | `stop()` → StopCodeInterpreterSession | `StopRuntimeSession` | |
| `list()` / `get_info()` / `is_running()` | `list_sessions()` / `get_session()` | no session list/get API | RT gap |
| `pause()` / resume via `connect()` | none | Session storage (Preview) keeps filesystem across stop/resume; no memory snapshot | No memory pause |
| `create_snapshot()` / `fork()` | none | none | Gap |
| `commands.run(cmd, cwd, envs, timeout)` | `execute_command(command)` | `InvokeAgentRuntimeCommand(body={command, timeout})`, streamed stdout/stderr + exit code, runs as root | cwd/envs/user must be inlined (`cd x && FOO=1 cmd`) |
| `commands.run(background=True)`, `commands.list/kill` | `invoke("startCommandExecution")`, `getTask`, `stopTask` (no SDK wrapper, use `invoke()`) | none; use `nohup ... &` and `kill` via command | |
| `commands.send_stdin`, `pty` | none | `open_shell(runtime_arn, session_id, shell_id)` → `ShellSession.send/resize/close` (WebSocket PTY, reconnectable) | |
| `run_code(code, language)` (code-interpreter SDK) | `execute_code(code, language)`; language python / javascript / typescript | none (run interpreter via command) | |
| `create_code_context` / `restart_code_context` | `clear_context()` only, one context per session | n/a | No multiple contexts |
| `files.read` | `download_file(s)` (readFiles) | via command (`cat`, base64) or mounted EFS/S3 Files | |
| `files.write` / `write_files` | `upload_file(s)` (writeFiles) | via command or mounted filesystem | |
| `files.list` / `remove` | `invoke("listFiles")` / `invoke("removeFiles")` | via command | |
| `files.exists/get_info/make_dir/rename` | via `execute_command` | via command | |
| `files.watch_dir` | none | none (`inotifywait` in image) | Gap |
| `get_host(port)` (public URL to a sandbox port) | none | none (only the agent invocation endpoint is exposed) | Gap: no inbound port preview |
| `get_metrics()` | none (CloudWatch) | none (CloudWatch / Observability) | |
| `mcp=` | AgentCore Gateway (separate service) | same | |
| `volume_mounts` | `filesystemConfigurations` | `filesystemConfigurations` (EFS / S3 Files, need VPC) | |
| `E2B_API_KEY` | IAM SigV4 | IAM SigV4 or OAuth JWT inbound auth | |

Summary: an E2B-style adapter is feasible on top of RT (custom image + command + shell) or CI (files + code).
Hard gaps: memory pause/snapshot/fork, port forwarding (`get_host`), per-session env/timeout, `watch_dir`.

## License

MIT. See [LICENSE](LICENSE).
