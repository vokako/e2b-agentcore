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
 2. _render_dockerfile  turn the E2B instruction list into a Dockerfile (FROM / RUN / COPY /
                        ENV / WORKDIR / USER). The start command is NOT baked in as an
                        ENTRYPOINT: like E2B, we run it ourselves when a sandbox starts.
 3. _zip_context        zip that Dockerfile plus only the local files referenced by COPY
                        steps (paths relative to file_context_path, .dockerignore honoured).
 4. upload to S3        s3://<bucket>/contexts/<alias>/<uuid>.zip (expires after 7 days)
 5. CodeBuild           native arm64 `docker build`, then push to ECR as
                        <registry>/<prefix>/templates:<alias>-<YYYYmmddHHMMSS>
 6. Harness            CreateHarness (first build) or UpdateHarness (rebuild) named
                        e2b_<alias>, with the new image as its environment. We poll until
                        it is READY and serving the new image.
 7. template record     s3://<bucket>/templates/<alias>.json keeps the E2B-only settings
                        (start cmd, ready cmd, workdir) that Sandbox.create needs.

build() is just build_in_background() + a get_build_status() loop, so both paths share code.

Why a Harness and not a plain AgentCore Runtime? A Runtime requires the image to run an
HTTP server on :8080 (/ping, /invocations). A Harness uses the image only as the sandbox
environment, so any E2B-style template (e.g. bare Ubuntu with no server) works unchanged.
"""
from __future__ import annotations

import fnmatch
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
from e2b.template.utils import read_dockerignore

from ._config import Config, ensure_infra
from ._errors import aws_errors, not_supported

DOCKERFILE_NAME = "Dockerfile.e2b"   # name of the generated Dockerfile inside the context zip


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

        def log(msg, level="info"):
            on_build_logs and on_build_logs(LogEntry(datetime.now(timezone.utc), level, msg))

        build_id, image = start_image_build(cfg, template, alias, skip_cache, log)

        # Remember what this build is for, so get_build_status() can finish steps 6-7
        # later, even from another process that only has the BuildInfo.
        tpl = template._template
        cfg.put_json(_build_key(build_id), {
            "alias": alias, "harness_name": hname, "image": image, "codebuild_id": build_id,
            "start_cmd": tpl._start_cmd, "ready_cmd": tpl._ready_cmd, "workdir": _last(tpl, "WORKDIR"),
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
            "built_at": time.time()})
        entries.append(LogEntry(datetime.now(timezone.utc), "info", f"Harness {h['harnessName']} READY ({h['arn']})"))
        return resp(TemplateBuildStatus.READY)

    # ------------------------------------------------------------------ lookup / delete

    @classmethod
    @aws_errors
    def exists(cls, name: str, config: Optional[Config] = None, **_ignored) -> bool:
        return find_harness(config or Config(), harness_name(name)) is not None

    @classmethod
    def alias_exists(cls, alias: str, config: Optional[Config] = None, **_ignored) -> bool:
        return cls.exists(alias, config=config)

    @classmethod
    @aws_errors
    def delete(cls, name: str, config: Optional[Config] = None, **_ignored) -> bool:
        """Not in e2b.Template (E2B deletes via CLI); deletes the Harness and template record.
        ECR images are kept."""
        cfg = config or Config()
        h = find_harness(cfg, harness_name(name))
        if not h:
            return False
        cfg.client("bedrock-agentcore-control").delete_harness(harnessId=h["harnessId"])
        cfg.delete_json(_template_key(name))
        return True

    # Tags are E2B's template-version aliases; ECR tags exist but Harness has no tag pointer.
    assign_tags = classmethod(not_supported("Template.assign_tags", "a Harness points at one image; rebuild instead"))
    remove_tags = classmethod(not_supported("Template.remove_tags", "a Harness points at one image; rebuild instead"))
    get_tags = classmethod(not_supported("Template.get_tags", "a Harness points at one image; rebuild instead"))


# ---------------------------------------------------------------------- helpers

def start_image_build(cfg: Config, template, alias: str, skip_cache: bool = False, log=lambda *_: None,
                      include_start_cmd: bool = False):
    """Steps 1-5: Dockerfile -> context zip -> S3 -> CodeBuild arm64 -> ECR. Non-blocking.

    Returns (codebuild_build_id, image_uri). Also used by runtime/build.py, which only
    needs the image (for a plain AgentCore Runtime) and no Harness.
    """
    # Step 1: one-time infra (no-op after the first build).
    ensure_infra(cfg, log=log)

    # Step 2-3: E2B instruction list -> Dockerfile -> context zip.
    dockerfile = _render_dockerfile(template, include_start_cmd)
    body = _zip_context(template, dockerfile)
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


def _render_dockerfile(template, include_start_cmd: bool = False) -> str:
    """E2B instructions -> Dockerfile text.

    Like e2b.Template.to_dockerfile, but keeps COPY's owner/mode (--chown/--chmod, needs
    BuildKit). The start command is left out by default because Sandbox.create runs it
    (E2B semantics); include_start_cmd=True bakes it in as ENTRYPOINT instead, which a
    plain AgentCore Runtime image needs (its server must start with the container).
    """
    tpl = template._template
    if tpl._base_template is not None:
        raise TemplateException("from_template() bases live in E2B's cloud; use a Docker image or Dockerfile")
    if tpl._base_image is None:
        raise TemplateException("no base image: call from_image / from_ubuntu_image / from_dockerfile first")
    lines = [f"FROM {tpl._base_image}"]
    for ins in tpl._instructions:
        kind, args = ins["type"].value, ins["args"]
        if kind == "RUN":
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
    if include_start_cmd and tpl._start_cmd:
        lines.append(f"ENTRYPOINT {tpl._start_cmd}")
    return "\n".join(lines) + "\n"


def _env_quote(v: str) -> str:
    return v if re.fullmatch(r"[A-Za-z0-9_./:@,+-]*", v) else '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _zip_context(template, dockerfile: str) -> bytes:
    """Zip the generated Dockerfile plus only the files that COPY steps reference.

    Paths resolve against the template's file_context_path (E2B default: the directory of
    the script that created Template()), and .dockerignore / file_ignore_patterns apply.
    """
    tpl = template._template
    ctx = tpl._file_context_path
    ignore = list(tpl._file_ignore_patterns or []) + read_dockerignore(ctx)

    def ignored(rel):
        return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.rstrip("/") + "/*") for p in ignore)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        added = set()
        for ins in tpl._instructions:
            if ins["type"].value != "COPY":
                continue
            matches = glob.glob(os.path.join(ctx, ins["args"][0]))
            if not matches:
                raise TemplateException(f"COPY source not found under {ctx}: {ins['args'][0]}")
            for m in matches:
                files = [m] if os.path.isfile(m) else [
                    os.path.join(root, f) for root, _dirs, fs in os.walk(m) for f in fs]
                for full in files:
                    rel = os.path.relpath(full, ctx)
                    if rel not in added and not ignored(rel):
                        z.write(full, rel)
                        added.add(rel)
        z.writestr(DOCKERFILE_NAME, dockerfile)
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
    cp, token = cfg.client("bedrock-agentcore-control"), None
    while True:
        page = cp.list_harnesses(**({"nextToken": token} if token else {}))
        for h in page.get("harnesses", []):
            if h.get("harnessName") == name:
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
