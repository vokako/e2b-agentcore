"""`Template` for the AgentCore provider: E2B's own template DSL, built into an AgentCore Harness.

This class SUBCLASSES the official `e2b.Template`, so every builder method of the official
SDK (from_dockerfile, from_ubuntu_image, apt_install, pip_install, copy, run_cmd, set_envs,
set_workdir, set_start_cmd, ...) is the real E2B code, not a re-implementation. Only the
methods that talk to E2B's cloud (build, build_in_background, get_build_status, exists,
alias_exists) are replaced with AgentCore versions.

    from e2b_agentcore import Template, wait_for_port, default_build_logger
    t = (Template()
         .from_ubuntu_image("24.04")
         .apt_install(["git", "python3"])
         .set_workdir("/work")
         .set_start_cmd("python3 -m http.server 8000", wait_for_port(8000)))
    Template.build(t, "my-sandbox", on_build_logs=default_build_logger())

What happens in Template.build(t, "my-sandbox")
------------------------------------------------
 1. ensure_infra()      first build only: create S3 bucket, ECR repo, CodeBuild role/project.
                        (Runs after steps 2-3, so an invalid template fails before any AWS call.)
 2. _render_dockerfile  turn the E2B instruction list into a Dockerfile (FROM / RUN / COPY /
                        ENV / WORKDIR / USER). The start command is NOT baked in as an
                        ENTRYPOINT: like E2B, we run it ourselves when a sandbox starts.
 3. _zip_context        zip that Dockerfile plus only the local files referenced by COPY
                        steps (paths relative to file_context_path, .dockerignore honoured).
 4. upload to S3        s3://<bucket>/contexts/<alias>/<uuid>.zip (expires after 7 days)
 5. CodeBuild           native arm64 `docker build`, then push to ECR as
                        <registry>/<prefix>/templates:<alias>-<YYYYmmddHHMMSS>
 6. deploy             V1 (default): CreateHarness (first build) or UpdateHarness (rebuild)
                        named e2b_<alias>, with the new image as its environment.
                        V2 (Config.platform_version="V2"): CreateAgentRuntime /
                        UpdateAgentRuntime named e2b_<alias> with platformVersion=V2 and the
                        image as the runtime container. We poll until it is READY and
                        serving the new image.
 7. template record     s3://<bucket>/templates/<alias>.json keeps the E2B-only settings
                        (start cmd, ready cmd, workdir) that Sandbox.create needs.

build() is just build_in_background() + a get_build_status() loop, so both paths share code.

Why a Harness by default, and not a plain AgentCore Runtime? A Runtime requires the image
to run an HTTP server on :8080 (/ping, /invocations). A Harness uses the image only as the
sandbox environment, so any E2B-style template (e.g. bare Ubuntu with no server) works
unchanged.

Why V2 is a plain Runtime: CreateHarness / UpdateHarness have no platformVersion field, and
the runtime a Harness creates behind the scenes is always V1. So for V2 we deploy our own
Runtime, and make the image satisfy the Runtime contract ourselves: the generated Dockerfile
gets an extra build stage that compiles SERVER_GO (a ~20-line static Go server answering
/ping and /invocations) and copies the binary in as the ENTRYPOINT. The user's image needs
no Python, Node or anything else for this. Commands and files still go through
InvokeAgentRuntimeCommand exactly as on V1, so Sandbox code is identical on both.
"""
from __future__ import annotations

import glob
import io
import os
import re
import time
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Callable, List, Optional, Union

import e2b
from e2b import BuildException, BuildInfo, LogEntry, TemplateException
from e2b.template.types import BuildStatusReason, TemplateBuildStatus, TemplateBuildStatusResponse
from e2b.template.utils import get_all_files_in_path, read_dockerignore

from ._config import Config, ensure_infra
from ._errors import aws_errors, not_supported

DOCKERFILE_NAME = "Dockerfile.e2b"   # name of the generated Dockerfile inside the context zip
SERVER_FILE = "e2b-agentcore-server.go"   # V2 only: source of the :8080 server, inside the zip
SERVER_BIN = "/opt/e2b-agentcore/server"  # V2 only: where the compiled server lives in the image
GO_IMAGE = "golang:1.24-alpine"           # V2 only: builder stage image (via dockerhub_mirror)

# The HTTP contract an AgentCore Runtime container must meet: listen on 0.0.0.0:8080, answer
# GET /ping (health) and POST /invocations. The server is also PID 1 and reaps orphans. Nothing here runs sandbox commands: those go
# through InvokeAgentRuntimeCommand, which AgentCore executes in the container itself.
# Built with CGO_ENABLED=0, so the binary is static and runs on any arm64 Linux base image
# (glibc, musl or distroless).
SERVER_GO = """package main

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
// Without this they stay zombies, and `kill -0 <pid>` keeps reporting them alive.
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
		w.Write([]byte(`{"status":"Healthy"}`))
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
"""


def harness_name(alias: str) -> str:
    """E2B aliases allow '-' and '.', Harness names only [a-zA-Z][a-zA-Z0-9_]{0,39}."""
    n = "e2b_" + re.sub(r"[^a-zA-Z0-9_]", "_", alias)
    if len(n) > 40:
        raise TemplateException(f"template name too long for a Harness name (max 36 chars): {alias!r}")
    return n


def _template_key(alias: str) -> str:
    return f"templates/{alias}.json"


def _build_key(build_id: str) -> str:
    return f"builds/{build_id.split(':')[-1]}.json"


class Template(e2b.Template):
    """Official e2b.Template with build/exists/... retargeted to AgentCore.

    Every method takes an optional `config=Config(...)`; otherwise Config() reads the
    E2B_AGENTCORE_* env vars. E2B connection options (api_key, domain, ...) are accepted and
    ignored so code written for e2b runs unchanged.
    """

    # ------------------------------------------------------------------ build (blocking)

    @classmethod
    def build(cls, template, name: Optional[str] = None, *, alias: Optional[str] = None,
              tags: Optional[List[str]] = None, cpu_count: Optional[int] = None,
              memory_mb: Optional[int] = None, min_free_disk_mb: Optional[int] = None,
              skip_cache: bool = False, on_build_logs: Optional[Callable[[LogEntry], None]] = None,
              config: Optional[Config] = None, **_e2b_opts) -> BuildInfo:
        """Same signature as e2b.Template.build. Blocks until the Harness is READY.

        cpu_count / memory_mb / min_free_disk_mb are accepted for API parity but have no
        effect: AgentCore sizes the microVM itself.
        """
        cfg = config or Config()
        info = cls.build_in_background(template, name, alias=alias, tags=tags, skip_cache=skip_cache,
                                       on_build_logs=on_build_logs, config=cfg)
        offset = 0
        while True:
            st = cls.get_build_status(info, logs_offset=offset, config=cfg)
            for entry in st.log_entries:          # stream new CodeBuild log lines to the caller
                on_build_logs and on_build_logs(entry)
            offset += len(st.log_entries)
            if st.status == TemplateBuildStatus.READY:
                return info
            if st.status == TemplateBuildStatus.ERROR:
                raise BuildException(st.reason.message if st.reason else "build failed")
            time.sleep(5)

    # ------------------------------------------------------------------ build (non-blocking)

    @classmethod
    @aws_errors
    def build_in_background(cls, template, name: Optional[str] = None, *, alias: Optional[str] = None,
                            tags: Optional[List[str]] = None, skip_cache: bool = False,
                            on_build_logs: Optional[Callable[[LogEntry], None]] = None,
                            config: Optional[Config] = None, **_ignored) -> BuildInfo:
        """Steps 1-5 start here; returns as soon as CodeBuild has accepted the build."""
        alias = name or alias
        if not alias:
            raise TemplateException("template name is required")
        cfg = config or Config()
        if not cfg.exec_role_arn:
            raise TemplateException("set E2B_AGENTCORE_EXEC_ROLE_ARN (the Harness execution role)")
        hname = harness_name(alias)  # validate the name before doing any work
        v2 = cfg.platform_version == "V2"
        prev = load_template(cfg, alias)
        if prev and prev.get("platform_version", "V1") != cfg.platform_version:
            raise TemplateException(
                f"template {alias!r} was built for platform {prev.get('platform_version', 'V1')}; "
                f"Template.delete({alias!r}) first to rebuild it for {cfg.platform_version}")

        def log(msg, level="info"):
            on_build_logs and on_build_logs(LogEntry(datetime.now(timezone.utc), level, msg))

        # V2: the image also gets the :8080 server a plain Runtime needs (see SERVER_GO).
        build_id, image = start_image_build(cfg, template, alias, skip_cache, log, with_server=v2)

        # Remember what this build is for, so get_build_status() can finish steps 6-7
        # later, even from another process that only has the BuildInfo.
        tpl = template._template
        cfg.put_json(_build_key(build_id), {
            "alias": alias, "harness_name": hname, "image": image, "codebuild_id": build_id,
            "start_cmd": tpl._start_cmd, "ready_cmd": tpl._ready_cmd, "workdir": _last(tpl, "WORKDIR"),
            "platform_version": cfg.platform_version,
            "harness_requested": False, "started_at": time.time()})
        return BuildInfo(template_id=hname, build_id=build_id, name=alias, alias=alias, tags=tags or [])

    @classmethod
    @aws_errors
    def get_build_status(cls, build_info: BuildInfo, logs_offset: Optional[int] = None,
                         config: Optional[Config] = None, **_ignored) -> TemplateBuildStatusResponse:
        """Poll one build. Drives steps 6-7 forward when CodeBuild has finished.

        Status mapping:  CodeBuild running            -> BUILDING
                         CodeBuild failed / stopped   -> ERROR (reason = CodeBuild status)
                         image pushed, Harness not
                         yet READY on the new image   -> BUILDING
                         Harness READY on new image   -> READY
        log_entries are CodeBuild's CloudWatch log lines, starting at logs_offset.
        """
        cfg = config or Config()
        rec = cfg.get_json(_build_key(build_info.build_id))
        if rec is None:
            raise TemplateException(f"unknown build {build_info.build_id}")
        cb = cfg.client("codebuild").batch_get_builds(ids=[rec["codebuild_id"]])["builds"][0]
        entries = _codebuild_logs(cfg, cb, logs_offset or 0)

        def resp(status, reason=None):
            return TemplateBuildStatusResponse(
                build_id=build_info.build_id, template_id=build_info.template_id, status=status,
                log_entries=entries, logs=[e.message for e in entries],
                reason=BuildStatusReason(message=reason) if reason else None)

        if cb["buildStatus"] == "IN_PROGRESS":
            return resp(TemplateBuildStatus.BUILDING)
        if cb["buildStatus"] != "SUCCEEDED":
            return resp(TemplateBuildStatus.ERROR, f"CodeBuild {cb['id']} {cb['buildStatus']}")

        if rec.get("platform_version", "V1") == "V2":
            return _runtime_status(cfg, rec, build_info, entries, resp)

        # Step 6: point the Harness at the new image (once), then wait for READY.
        cp = cfg.client("bedrock-agentcore-control")
        if not rec["harness_requested"]:
            _upsert_harness(cfg, rec["harness_name"], rec["image"])
            rec["harness_requested"] = True
            cfg.put_json(_build_key(build_info.build_id), rec)
            entries.append(LogEntry(datetime.now(timezone.utc), "info",
                                    f"image pushed; deploying Harness {rec['harness_name']}"))
            return resp(TemplateBuildStatus.BUILDING)
        h = find_harness(cfg, rec["harness_name"])
        h = h and cp.get_harness(harnessId=h["harnessId"])["harness"]
        if not h:
            return resp(TemplateBuildStatus.ERROR, f"Harness {rec['harness_name']} disappeared")
        if h["status"].endswith("FAILED"):
            return resp(TemplateBuildStatus.ERROR, f"Harness {h['status']}: {h.get('failureReason')}")
        uri = h.get("environmentArtifact", {}).get("containerConfiguration", {}).get("containerUri")
        if h["status"] != "READY" or uri != rec["image"]:
            return resp(TemplateBuildStatus.BUILDING)

        # Step 7: publish the template record Sandbox.create reads.
        cfg.put_json(_template_key(rec["alias"]), {
            "alias": rec["alias"], "harness_name": h["harnessName"], "harness_id": h["harnessId"],
            "harness_arn": h["arn"], "image": rec["image"], "start_cmd": rec["start_cmd"],
            "ready_cmd": rec["ready_cmd"], "workdir": rec["workdir"], "build_id": build_info.build_id,
            "platform_version": "V1", "agent_runtime_arn": h["arn"], "built_at": time.time()})
        entries.append(LogEntry(datetime.now(timezone.utc), "info", f"Harness {h['harnessName']} READY ({h['arn']})"))
        return resp(TemplateBuildStatus.READY)

    # ------------------------------------------------------------------ lookup / delete

    @classmethod
    @aws_errors
    def exists(cls, name: str, config: Optional[Config] = None, **_ignored) -> bool:
        """True if the template is deployed, as a Harness (V1) or a V2 Runtime."""
        cfg, n = config or Config(), harness_name(name)
        return find_harness(cfg, n) is not None or _find_v2_runtime(cfg, name) is not None

    @classmethod
    def alias_exists(cls, alias: str, config: Optional[Config] = None, **_ignored) -> bool:
        return cls.exists(alias, config=config)

    @classmethod
    @aws_errors
    def delete(cls, name: str, config: Optional[Config] = None, **_ignored) -> bool:
        """Not in e2b.Template (E2B deletes via CLI); deletes the Harness and template record.
        ECR images are kept."""
        cfg = config or Config()
        cp = cfg.client("bedrock-agentcore-control")
        h, rt = find_harness(cfg, harness_name(name)), _find_v2_runtime(cfg, name)
        if not h and not rt:
            return False
        if h:
            cp.delete_harness(harnessId=h["harnessId"])
        if rt:
            cp.delete_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
        cfg.delete_json(_template_key(name))
        return True

    # Tags are E2B's template-version aliases; ECR tags exist but Harness has no tag pointer.
    assign_tags = classmethod(not_supported("Template.assign_tags", "a Harness points at one image; rebuild instead"))
    remove_tags = classmethod(not_supported("Template.remove_tags", "a Harness points at one image; rebuild instead"))
    get_tags = classmethod(not_supported("Template.get_tags", "a Harness points at one image; rebuild instead"))


# ---------------------------------------------------------------------- helpers

def start_image_build(cfg: Config, template, alias: str, skip_cache: bool = False, log=lambda *_: None,
                      include_start_cmd: bool = False, with_server: bool = False):
    """Steps 1-5: Dockerfile -> context zip -> S3 -> CodeBuild arm64 -> ECR. Non-blocking.

    Returns (codebuild_build_id, image_uri). Also used by runtime/build.py, which only
    needs the image (for a plain AgentCore Runtime) and no Harness.
    """
    # Steps 2-3 first: E2B instruction list -> Dockerfile -> context zip. Done before any
    # AWS call so an invalid template fails without touching the account.
    dockerfile = _render_dockerfile(template, include_start_cmd, cfg.dockerhub_mirror, with_server)
    body = _zip_context(template, dockerfile, {SERVER_FILE: SERVER_GO} if with_server else None)

    # Step 1: one-time infra (no-op after the first build).
    ensure_infra(cfg, log=log)

    # Unique, sortable image tag per build, e.g. ubuntu24-base-20261015092700.
    tag = f"{re.sub(r'[^a-zA-Z0-9_.-]', '-', alias)}-{time.strftime('%Y%m%d%H%M%S')}"
    image = f"{cfg.registry}/{cfg.ecr_repo}:{tag}"

    # Step 4: upload the context. The bucket lifecycle deletes it after 7 days.
    key = f"contexts/{alias}/{uuid.uuid4().hex}.zip"
    cfg.client("s3").put_object(Bucket=cfg.bucket, Key=key, Body=body)
    log(f"uploaded build context ({len(body)} bytes) to s3://{cfg.bucket}/{key}")

    # Step 5: start CodeBuild on this zip. These env vars fill the $VARS in _config.BUILDSPEC.
    build = cfg.client("codebuild").start_build(
        projectName=cfg.codebuild_project, sourceTypeOverride="S3",
        sourceLocationOverride=f"{cfg.bucket}/{key}",
        environmentVariablesOverride=[
            {"name": "IMAGE", "value": image},                # where to push
            {"name": "REGISTRY", "value": cfg.registry},      # what to docker-login to
            {"name": "DOCKERFILE", "value": DOCKERFILE_NAME},  # generated Dockerfile in the zip
            {"name": "NO_CACHE", "value": "--no-cache" if skip_cache else ""},
        ])["build"]
    log(f"CodeBuild {build['id']} started (native arm64) -> {image}")
    return build["id"], image


def wait_image_build(cfg: Config, build_id: str, log=lambda *_: None) -> None:
    """Block until a CodeBuild build ends, forwarding its log lines; raise if it failed."""
    offset = 0
    while True:
        b = cfg.client("codebuild").batch_get_builds(ids=[build_id])["builds"][0]
        entries = _codebuild_logs(cfg, b, offset)
        for e in entries:
            log(e.message)
        offset += len(entries)
        if b["buildStatus"] != "IN_PROGRESS":
            break
        time.sleep(5)
    if b["buildStatus"] != "SUCCEEDED":
        raise BuildException(f"CodeBuild {build_id} {b['buildStatus']}")

def load_template(cfg: Config, alias: str) -> Optional[dict]:
    return cfg.get_json(_template_key(alias))


def _last(tpl, kind: str) -> Optional[str]:
    vals = [i["args"][0] for i in tpl._instructions if i["type"].value == kind]
    return vals[-1] if vals else None


def mirror_image(image: str, mirror: str) -> str:
    """Docker official image (`ubuntu:24.04`, `library/python:3.12`) -> same image on `mirror`.

    Anything with a registry host (`public.ecr.aws/...`, `localhost:5000/x`) or a Docker Hub
    namespace (`user/repo`) is returned unchanged: the mirror only carries official images.
    """
    if not mirror:
        return image
    if image.startswith("library/"):
        image = image[len("library/"):]
    if "/" in image:
        return image
    return f"{mirror.rstrip('/')}/{image}"


def _render_dockerfile(template, include_start_cmd: bool = False, mirror: str = "",
                       with_server: bool = False) -> str:
    """E2B instructions -> Dockerfile text.

    Like e2b.Template.to_dockerfile, but keeps COPY's owner/mode (--chown/--chmod, needs
    BuildKit). The start command is left out by default because Sandbox.create runs it
    (E2B semantics); include_start_cmd=True bakes it in as ENTRYPOINT instead, which a
    plain AgentCore Runtime image needs (its server must start with the container).

    with_server=True (platform V2) puts a builder stage in front that compiles SERVER_GO,
    then copies the binary into the final image and makes it the ENTRYPOINT. The template's
    own start command is still run by Sandbox.create, not baked in.
    """
    tpl = template._template
    if tpl._base_template is not None:
        raise TemplateException("from_template() bases live in E2B's cloud; use a Docker image or Dockerfile")
    if tpl._base_image is None:
        raise TemplateException("no base image: call from_image / from_ubuntu_image / from_dockerfile first")
    lines = []
    if with_server:
        lines += [f"FROM {mirror_image(GO_IMAGE, mirror)} AS e2b_agentcore_server",
                  f"COPY {SERVER_FILE} /src/main.go",
                  "RUN cd /src && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /server main.go"]
    lines.append(f"FROM {mirror_image(tpl._base_image, mirror)}")
    for ins in tpl._instructions:
        kind, args = ins["type"].value, ins["args"]
        if kind == "RUN":
            # args[1], when present, is the user the step should run as (run_cmd(user=...)).
            if len(args) > 1 and args[1]:
                lines += [f"USER {args[1]}", f"RUN {args[0]}", "USER root"]
            else:
                lines.append(f"RUN {args[0]}")
        elif kind == "COPY":
            src, dest = args[0], args[1]
            owner = args[2] if len(args) > 2 and args[2] else None
            mode = args[3] if len(args) > 3 and args[3] else None
            flags = (f"--chown={owner} " if owner else "") + (f"--chmod={mode} " if mode else "")
            lines.append(f"COPY {flags}{src} {dest}")
        elif kind == "ENV":
            lines.append("ENV " + " ".join(f"{args[i]}={_env_quote(args[i + 1])}" for i in range(0, len(args), 2)))
        else:  # WORKDIR, USER
            lines.append(f"{kind} {' '.join(args)}")
    # The Harness runs commands as root regardless; end as root so in-image tooling matches.
    # (E2B's Dockerfile parser appends `USER user` / `WORKDIR /home/user`, E2B's default
    # sandbox user, which a stock image doesn't have.)
    lines.append("USER root")
    if with_server:
        lines += [f"COPY --from=e2b_agentcore_server /server {SERVER_BIN}",
                  f'ENTRYPOINT ["{SERVER_BIN}"]', "CMD []"]
    elif include_start_cmd and tpl._start_cmd:
        lines.append(f"ENTRYPOINT {tpl._start_cmd}")
    return "\n".join(lines) + "\n"


def _env_quote(v: str) -> str:
    """Literal ENV value. Docker expands $VAR even inside double quotes, so `$` is escaped too:
    E2B's set_envs values are plain strings, not shell expressions."""
    if re.fullmatch(r"[A-Za-z0-9_./:@,+-]*", v):
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def _zip_context(template, dockerfile: str, extra: Optional[dict] = None) -> bytes:
    """Zip the generated Dockerfile plus only the files that COPY steps reference.

    Paths resolve against the template's file_context_path (E2B default: the directory of
    the script that created Template()), and .dockerignore / file_ignore_patterns apply.
    """
    tpl = template._template
    ctx = tpl._file_context_path
    # e2b's own matcher: .dockerignore semantics (patterns anchored at the context root, a
    # matched directory excludes everything under it, `!` re-includes, last match wins).
    ignore = list(tpl._file_ignore_patterns or []) + read_dockerignore(ctx)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        added = set()
        for ins in tpl._instructions:
            if ins["type"].value != "COPY":
                continue
            if not glob.glob(os.path.join(ctx, ins["args"][0])):
                raise TemplateException(f"COPY source not found under {ctx}: {ins['args'][0]}")
            for full in get_all_files_in_path(ins["args"][0], ctx, ignore, include_directories=False):
                rel = os.path.relpath(full, ctx).replace(os.sep, "/")
                if rel not in added and not os.path.isdir(full):
                    z.write(full, rel)
                    added.add(rel)
        z.writestr(DOCKERFILE_NAME, dockerfile)
        for fname, content in (extra or {}).items():   # generated files (V2 server source)
            z.writestr(fname, content)
    return buf.getvalue()


def _codebuild_logs(cfg: Config, build: dict, offset: int) -> List[LogEntry]:
    lg = build.get("logs", {})
    if not (lg.get("groupName") and lg.get("streamName")):
        return []
    logs, events, token = cfg.client("logs"), [], None
    while True:  # read the whole stream; builds are short enough for this
        kw = {"nextToken": token} if token else {"startFromHead": True}
        try:
            r = logs.get_log_events(logGroupName=lg["groupName"], logStreamName=lg["streamName"], **kw)
        except Exception:
            return []
        events += r["events"]
        if r["nextForwardToken"] == token or not r["events"]:
            break
        token = r["nextForwardToken"]
    return [LogEntry(datetime.fromtimestamp(e["timestamp"] / 1000, timezone.utc), "info", e["message"].rstrip())
            for e in events[offset:]]


def find_harness(cfg: Config, name: str):
    """The Harness called `name`, or None. A Harness being deleted counts as gone: AgentCore
    keeps it in DELETING for several minutes, and it can neither run sandboxes nor be
    deleted again."""
    cp, token = cfg.client("bedrock-agentcore-control"), None
    while True:
        page = cp.list_harnesses(**({"nextToken": token} if token else {}))
        for h in page.get("harnesses", []):
            if h.get("harnessName") == name and h.get("status") != "DELETING":
                return h
        token = page.get("nextToken")
        if not token:
            return None


def _upsert_harness(cfg: Config, name: str, image: str) -> None:
    """Create or update Harness `name` so it runs sandboxes from `image`.

    model: required by the API, never invoked by this provider.
    lifecycle: idle timeout and max lifetime for every session of this Harness (Config).
    """
    cp = cfg.client("bedrock-agentcore-control")
    spec = dict(executionRoleArn=cfg.exec_role_arn,
                environmentArtifact={"containerConfiguration": {"containerUri": image}},
                model={"bedrockModelConfig": {"modelId": cfg.model_id}},
                environment={"agentCoreRuntimeEnvironment": {"lifecycleConfiguration": {
                    "idleRuntimeSessionTimeout": cfg.idle_timeout_s, "maxLifetime": cfg.max_lifetime_s}}})
    existing = find_harness(cfg, name)
    if existing:
        # UpdateHarness wraps the artifact in `optionalValue` (CreateHarness does not).
        spec["environmentArtifact"] = {"optionalValue": spec["environmentArtifact"]}
        cp.update_harness(harnessId=existing["harnessId"], **spec)
    else:
        cp.create_harness(harnessName=name, **spec)


# ---------------------------------------------------------------------- V2 (plain Runtime)

def _find_v2_runtime(cfg: Config, alias: str):
    """The V2 Runtime of template `alias`, or None. Only runtimes our template record points
    at count, so an unrelated runtime that happens to be called e2b_<alias> is never touched."""
    rec = load_template(cfg, alias)
    rid = rec and rec.get("platform_version") == "V2" and rec.get("agent_runtime_id")
    if not rid:
        # Template record not written yet (first build still deploying): fall back to the name.
        return _runtime_by_name(cfg, harness_name(alias)) if rec is None else None
    try:
        r = cfg.client("bedrock-agentcore-control").get_agent_runtime(agentRuntimeId=rid)
    except Exception:
        return None
    return None if r.get("status") == "DELETING" else r


def _runtime_by_name(cfg: Config, name: str):
    cp, token = cfg.client("bedrock-agentcore-control"), None
    while True:
        page = cp.list_agent_runtimes(maxResults=100, **({"nextToken": token} if token else {}))
        for r in page.get("agentRuntimes", []):
            if r.get("agentRuntimeName") == name and r.get("status") != "DELETING":
                return r
        token = page.get("nextToken")
        if not token:
            return None


def _upsert_v2_runtime(cfg: Config, name: str, image: str) -> None:
    """Create or update Runtime `name` (platformVersion=V2) so it runs `image`.

    roleArn / network / lifecycle are the same settings a V1 Harness gets from Config.
    V2 snapshots the microVM at deploy time (the :8080 server must be up for that), and
    every sandbox session is restored from that snapshot.
    """
    cp = cfg.client("bedrock-agentcore-control")
    spec = dict(agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
                roleArn=cfg.exec_role_arn, networkConfiguration={"networkMode": "PUBLIC"},
                protocolConfiguration={"serverProtocol": "HTTP"},
                lifecycleConfiguration={"idleRuntimeSessionTimeout": cfg.idle_timeout_s,
                                        "maxLifetime": cfg.max_lifetime_s},
                platformVersion="V2")
    existing = _runtime_by_name(cfg, name)
    if existing:
        cp.update_agent_runtime(agentRuntimeId=existing["agentRuntimeId"], **spec)
    else:
        cp.create_agent_runtime(agentRuntimeName=name, **spec)


def _runtime_status(cfg: Config, rec: dict, build_info, entries, resp):
    """get_build_status steps 6-7 for platform V2."""
    if not rec["harness_requested"]:
        _upsert_v2_runtime(cfg, rec["harness_name"], rec["image"])
        rec["harness_requested"] = True
        cfg.put_json(_build_key(build_info.build_id), rec)
        entries.append(LogEntry(datetime.now(timezone.utc), "info",
                                f"image pushed; deploying V2 Runtime {rec['harness_name']}"))
        return resp(TemplateBuildStatus.BUILDING)
    r = _runtime_by_name(cfg, rec["harness_name"])
    r = r and cfg.client("bedrock-agentcore-control").get_agent_runtime(agentRuntimeId=r["agentRuntimeId"])
    if not r:
        return resp(TemplateBuildStatus.ERROR, f"Runtime {rec['harness_name']} disappeared")
    if r["status"].endswith("FAILED"):
        return resp(TemplateBuildStatus.ERROR, f"Runtime {r['status']}: {r.get('failureReason')}")
    uri = r.get("agentRuntimeArtifact", {}).get("containerConfiguration", {}).get("containerUri")
    if r["status"] != "READY" or uri != rec["image"]:
        return resp(TemplateBuildStatus.BUILDING)
    if r.get("platformVersion") != "V2":
        return resp(TemplateBuildStatus.ERROR, f"Runtime {r['agentRuntimeName']} is {r.get('platformVersion')}, not V2")
    cfg.put_json(_template_key(rec["alias"]), {
        "alias": rec["alias"], "platform_version": "V2", "agent_runtime_name": r["agentRuntimeName"],
        "agent_runtime_id": r["agentRuntimeId"], "agent_runtime_arn": r["agentRuntimeArn"],
        "image": rec["image"], "start_cmd": rec["start_cmd"], "ready_cmd": rec["ready_cmd"],
        "workdir": rec["workdir"], "build_id": build_info.build_id, "built_at": time.time()})
    entries.append(LogEntry(datetime.now(timezone.utc), "info",
                            f"V2 Runtime {r['agentRuntimeName']} READY ({r['agentRuntimeArn']})"))
    return resp(TemplateBuildStatus.READY)
