# E2B Sandbox API → AgentCore mapping

## `e2b_agentcore`: E2B-style SDK on AgentCore Harness

```python
from e2b_agentcore import Template, Sandbox   # was: from e2b import Template, Sandbox
Template.build(Template().from_dockerfile("Dockerfile"), "ubuntu24-base", on_build_logs=print)
sbx = Sandbox.create("ubuntu24-base", envs={"FOO": "1"})
sbx.commands.run("git --version"); sbx.files.write("/root/a.txt", "hi"); sbx.kill()
```

`Template.build`: zips the build context to S3, builds natively on CodeBuild ARM (`docker build --platform linux/arm64`),
pushes to ECR `e2b-agentcore/templates:<name>-<ts>`, then creates or updates Harness `e2b_<name>` with that image and waits for READY.
The first build creates the bucket, ECR repo, CodeBuild role and project (`_config.ensure_infra`).
`Sandbox` maps to one `runtimeSessionId`. `commands` and `files` go through `InvokeAgentRuntimeCommand`, and `kill` calls `StopRuntimeSession`.

Env: `E2B_AGENTCORE_EXEC_ROLE_ARN` (required; needs ECR pull + logs), `E2B_AGENTCORE_REGION` (default us-east-2),
`E2B_AGENTCORE_MODEL_ID`. Run: `PYTHONPATH=. python examples/build_and_run.py Dockerfile.fixed ubuntu24-base`.

Measured 2026-10-15 (us-east-2): build + harness READY 276 s (apt via aliyun mirror from the US is most of it),
`Sandbox.create` 4.1 s, each command about 0.39 s.

## API mapping

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
