"""E2B-style `Template` that builds a Dockerfile into an AgentCore Harness.

    from e2b_agentcore import Template
    t = Template().from_dockerfile("Dockerfile")
    info = Template.build(t, "my-sandbox", on_build_logs=print)

Build path: zip context -> S3 -> CodeBuild (native arm64, docker build) -> ECR ->
CreateHarness/UpdateHarness with the image as its environment -> wait READY.
"""
from __future__ import annotations

import io
import os
import re
import time
import uuid
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

from botocore.exceptions import ClientError

from ._config import Config, ensure_infra


class TemplateException(Exception):
    pass


@dataclass
class BuildInfo:
    alias: str
    template_id: str      # harness id
    build_id: str         # CodeBuild build id
    image: str            # ECR image URI
    harness_arn: str


@dataclass
class LogEntry:
    timestamp: float
    level: str
    message: str

    def __str__(self):
        return f"[{self.level}] {self.message}"


def harness_name(alias: str) -> str:
    """E2B aliases allow '-'; harness names only [a-zA-Z][a-zA-Z0-9_]{0,39}."""
    n = "e2b_" + re.sub(r"[^a-zA-Z0-9_]", "_", alias)
    if len(n) > 40:
        raise TemplateException(f"template name too long for a harness: {alias!r}")
    return n


class TemplateBuilder:
    def __init__(self, dockerfile: str, context_dir: Optional[str]):
        self.dockerfile = dockerfile
        self.context_dir = context_dir

    def to_dockerfile(self) -> str:
        return self.dockerfile


class Template:
    """Subset of e2b.Template: from_dockerfile / from_image, build, exists, delete."""

    def from_dockerfile(self, dockerfile_content_or_path: str) -> TemplateBuilder:
        if os.path.isfile(dockerfile_content_or_path):
            path = os.path.abspath(dockerfile_content_or_path)
            with open(path) as f:
                return TemplateBuilder(f.read(), os.path.dirname(path))
        return TemplateBuilder(dockerfile_content_or_path, None)

    def from_image(self, image: str) -> TemplateBuilder:
        return TemplateBuilder(f"FROM {image}\n", None)

    def from_ubuntu_image(self, variant: str = "latest") -> TemplateBuilder:
        return self.from_image(f"ubuntu:{variant}")

    # ------------------------------------------------------------------ build

    @staticmethod
    def build(template: TemplateBuilder, name: Optional[str] = None, *, alias: Optional[str] = None,
              cpu_count: Optional[int] = None, memory_mb: Optional[int] = None,
              skip_cache: bool = False,
              on_build_logs: Optional[Callable[[LogEntry], None]] = None,
              config: Optional[Config] = None, **_ignored) -> BuildInfo:
        alias = name or alias
        if not alias:
            raise TemplateException("template name is required")
        cfg = config or Config()
        if not cfg.exec_role_arn:
            raise TemplateException("set E2B_AGENTCORE_EXEC_ROLE_ARN (harness execution role)")
        log = (lambda m, lvl="info": on_build_logs(LogEntry(time.time(), lvl, m))) if on_build_logs \
            else (lambda m, lvl="info": None)
        # cpu_count / memory_mb: AgentCore sizes the microVM itself; accepted for API parity.

        ensure_infra(cfg, log=log)
        tag = f"{re.sub(r'[^a-zA-Z0-9_.-]', '-', alias)}-{time.strftime('%Y%m%d%H%M%S')}"
        image = f"{cfg.registry}/{cfg.ecr_repo}:{tag}"

        key = f"contexts/{alias}/{uuid.uuid4().hex}.zip"
        cfg.client("s3").put_object(Bucket=cfg.bucket, Key=key, Body=_zip_context(template))
        log(f"uploaded build context s3://{cfg.bucket}/{key}")

        build_id = _codebuild(cfg, key, image, skip_cache, log)
        harness = _upsert_harness(cfg, harness_name(alias), image, log)
        return BuildInfo(alias=alias, template_id=harness["harnessId"], build_id=build_id,
                         image=image, harness_arn=harness["arn"])

    @staticmethod
    def exists(name: str, config: Optional[Config] = None) -> bool:
        return find_harness(config or Config(), harness_name(name)) is not None

    @staticmethod
    def delete(name: str, config: Optional[Config] = None) -> bool:
        cfg = config or Config()
        h = find_harness(cfg, harness_name(name))
        if not h:
            return False
        cfg.client("bedrock-agentcore-control").delete_harness(harnessId=h["harnessId"])
        return True


# ---------------------------------------------------------------------- helpers

def _zip_context(t: TemplateBuilder) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if t.context_dir:
            ignore = {".git", "__pycache__", "node_modules", ".venv"}
            for root, dirs, files in os.walk(t.context_dir):
                dirs[:] = [d for d in dirs if d not in ignore]
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, t.context_dir))
        z.writestr("Dockerfile.e2b", t.dockerfile)
    return buf.getvalue()


def _codebuild(cfg: Config, key: str, image: str, skip_cache: bool, log) -> str:
    cb, logs = cfg.client("codebuild"), cfg.client("logs")
    env = [{"name": "IMAGE", "value": image}, {"name": "REGISTRY", "value": cfg.registry},
           {"name": "DOCKERFILE", "value": "Dockerfile.e2b"}]
    b = cb.start_build(projectName=cfg.codebuild_project, sourceTypeOverride="S3",
                       sourceLocationOverride=f"{cfg.bucket}/{key}",
                       environmentVariablesOverride=env)["build"]
    build_id = b["id"]
    log(f"CodeBuild {build_id} started (arm64)")
    token, status = None, "IN_PROGRESS"
    while True:
        b = cb.batch_get_builds(ids=[build_id])["builds"][0]
        status = b["buildStatus"]
        lg = b.get("logs", {})
        if lg.get("groupName") and lg.get("streamName"):
            try:
                kw = {"nextToken": token} if token else {"startFromHead": True}
                r = logs.get_log_events(logGroupName=lg["groupName"], logStreamName=lg["streamName"], **kw)
                for e in r["events"]:
                    log(e["message"].rstrip())
                token = r["nextForwardToken"]
            except ClientError:
                pass
        if status != "IN_PROGRESS":
            break
        time.sleep(5)
    if status != "SUCCEEDED":
        raise TemplateException(f"build {build_id} {status}")
    log(f"pushed {image}")
    return build_id


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


def _upsert_harness(cfg: Config, name: str, image: str, log):
    cp = cfg.client("bedrock-agentcore-control")
    spec = dict(executionRoleArn=cfg.exec_role_arn,
                environmentArtifact={"containerConfiguration": {"containerUri": image}},
                model={"bedrockModelConfig": {"modelId": cfg.model_id}},
                environment={"agentCoreRuntimeEnvironment": {"lifecycleConfiguration": {
                    "idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}}})
    existing = find_harness(cfg, name)
    if existing:
        h = cp.update_harness(harnessId=existing["harnessId"], **spec)["harness"]
        log(f"updating harness {name} -> {image}")
    else:
        h = cp.create_harness(harnessName=name, **spec)["harness"]
        log(f"creating harness {name}")
    deadline = time.time() + 1200
    while time.time() < deadline:
        h = cp.get_harness(harnessId=h["harnessId"])["harness"]
        if h["status"] == "READY":
            log(f"harness {name} READY ({h['arn']})")
            return h
        if h["status"].endswith("FAILED"):
            raise TemplateException(f"harness {name} {h['status']}: {h.get('failureReason')}")
        time.sleep(10)
    raise TemplateException(f"harness {name} not READY after 20 min")
