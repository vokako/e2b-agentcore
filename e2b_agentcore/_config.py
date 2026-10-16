"""Config, one-time infrastructure and a tiny S3 registry for the AgentCore provider.

Everything is derived from the caller's AWS credentials (normal boto3 credential chain,
e.g. AWS_PROFILE) plus a few env vars, so the package works in any account:

  E2B_AGENTCORE_REGION          region for ECR / CodeBuild / AgentCore (default us-east-2)
  E2B_AGENTCORE_EXEC_ROLE_ARN   execution role the Harness runs as. It needs ECR pull
                                (ecr:GetAuthorizationToken, ecr:BatchGetImage,
                                ecr:GetDownloadUrlForLayer) and CloudWatch Logs write.
  E2B_AGENTCORE_MODEL_ID        model the Harness is configured with. The CreateHarness API
                                requires one, but this provider never calls the model: it only
                                uses the Harness as a sandbox (its microVM + shell commands).
  E2B_AGENTCORE_PREFIX          name prefix for every resource created here (default e2b-agentcore)
  E2B_AGENTCORE_DOCKERHUB_MIRROR  where Docker official images are pulled from during builds
                                (default public.ecr.aws/docker/library; "" = Docker Hub)

Resources created on the first Template.build (see ensure_infra):

  S3 bucket   <prefix>-src-<account>-<region>
                contexts/...   zipped build contexts (expire after 7 days)
                templates/...  one JSON record per template (start/ready cmd, workdir, image)
                sandboxes/...  one JSON record per live sandbox (E2B metadata, start time)
  ECR repo    <prefix>/templates            one image tag per build: <alias>-<timestamp>
  IAM role    <prefix>-codebuild            what CodeBuild runs as (S3 read, ECR push, logs)
  CodeBuild   <prefix>-builder              ARM_CONTAINER project that runs `docker build`
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from functools import cached_property
from typing import Iterator, Optional

import boto3
from botocore.exceptions import ClientError


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


@dataclass
class Config:
    region: str = os.environ.get("E2B_AGENTCORE_REGION", "us-east-2")
    exec_role_arn: Optional[str] = os.environ.get("E2B_AGENTCORE_EXEC_ROLE_ARN")
    model_id: str = os.environ.get("E2B_AGENTCORE_MODEL_ID", "us.anthropic.claude-sonnet-5")
    prefix: str = os.environ.get("E2B_AGENTCORE_PREFIX", "e2b-agentcore")
    # Harness lifecycle. E2B sets timeouts per sandbox; AgentCore sets them once per Harness,
    # so these apply to every sandbox of every template built with this config.
    idle_timeout_s: int = int(os.environ.get("E2B_AGENTCORE_IDLE_TIMEOUT", "900"))
    max_lifetime_s: int = int(os.environ.get("E2B_AGENTCORE_MAX_LIFETIME", "28800"))
    # Docker Hub rate-limits anonymous pulls (429), and CodeBuild hosts share IPs, so official
    # images (FROM ubuntu:24.04, python:3.12, ...) are pulled from AWS's mirror of them on ECR
    # Public instead. Images with a registry or a namespace (user/repo) are left alone.
    # Set E2B_AGENTCORE_DOCKERHUB_MIRROR="" to pull from Docker Hub directly.
    dockerhub_mirror: str = os.environ.get("E2B_AGENTCORE_DOCKERHUB_MIRROR", "public.ecr.aws/docker/library")

    @cached_property
    def session(self):
        return boto3.Session(region_name=self.region)

    def client(self, name):
        return self.session.client(name)

    @cached_property
    def account(self) -> str:
        return self.client("sts").get_caller_identity()["Account"]

    @property
    def bucket(self) -> str:
        return f"{self.prefix}-src-{self.account}-{self.region}"

    @property
    def ecr_repo(self) -> str:
        return f"{self.prefix}/templates"

    @property
    def registry(self) -> str:
        return f"{self.account}.dkr.ecr.{self.region}.amazonaws.com"

    @property
    def codebuild_project(self) -> str:
        return f"{self.prefix}-builder"

    @property
    def codebuild_role(self) -> str:
        return f"{self.prefix}-codebuild"

    # ------------------------------------------------------------------ S3 registry
    # AgentCore has no API to list Runtime sessions or to attach metadata to them, and a
    # Harness has nowhere to keep E2B template settings (start/ready command, workdir).
    # Both are kept as small JSON objects in the build bucket instead.

    def put_json(self, key: str, obj: dict) -> None:
        self.client("s3").put_object(Bucket=self.bucket, Key=key, Body=json.dumps(obj, default=str).encode(),
                                     ContentType="application/json")

    def get_json(self, key: str) -> Optional[dict]:
        try:
            return json.loads(self.client("s3").get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except ClientError as exc:
            if _code(exc) in ("NoSuchKey", "404", "NoSuchBucket"):
                return None
            raise

    def delete_json(self, key: str) -> None:
        self.client("s3").delete_object(Bucket=self.bucket, Key=key)

    def iter_json(self, prefix: str) -> Iterator[dict]:
        s3 = self.client("s3")
        try:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    rec = self.get_json(obj["Key"])
                    if rec is not None:
                        yield rec
        except ClientError as exc:
            if _code(exc) != "NoSuchBucket":
                raise


# CodeBuild buildspec. CodeBuild unzips the build context from S3 into its working
# directory, then runs these phases on a native arm64 machine (AgentCore microVMs are
# arm64-only, so building natively avoids slow, flaky QEMU emulation):
#   pre_build   log Docker in to our ECR registry (the role allows ecr:GetAuthorizationToken)
#   build       docker build. $DOCKERFILE is the Dockerfile we generated from the E2B
#               template (Dockerfile.e2b); --platform pins arm64 even for multi-arch bases.
#               $NO_CACHE is "--no-cache" when Template.build(skip_cache=True).
#   post_build  push only if the build succeeded (post_build runs even after a failure)
# $IMAGE, $REGISTRY, $DOCKERFILE and $NO_CACHE are passed per build by
# Template.build_in_background. DOCKER_BUILDKIT=1 enables `COPY --chmod`, which E2B's
# copy(mode=...) needs.
BUILDSPEC = """version: 0.2
env:
  variables:
    DOCKER_BUILDKIT: "1"
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $REGISTRY
  build:
    commands:
      - docker build $NO_CACHE --platform linux/arm64 -f $DOCKERFILE -t $IMAGE .
  post_build:
    commands:
      - if [ "$CODEBUILD_BUILD_SUCCEEDING" = "1" ]; then docker push $IMAGE; fi
"""


def ensure_infra(cfg: Config, log=print) -> None:
    """Idempotently create the S3 bucket, ECR repo, CodeBuild role and ARM project.

    Safe to call before every build: each step first checks whether the resource exists.
    Nothing here is ever deleted automatically.
    """
    s3, ecr, iam, cb = (cfg.client(n) for n in ("s3", "ecr", "iam", "codebuild"))

    # 1. S3 bucket: private, and build contexts under contexts/ expire after 7 days.
    try:
        s3.head_bucket(Bucket=cfg.bucket)
    except ClientError:
        kw = {} if cfg.region == "us-east-1" else {
            "CreateBucketConfiguration": {"LocationConstraint": cfg.region}}
        s3.create_bucket(Bucket=cfg.bucket, **kw)
        s3.put_public_access_block(Bucket=cfg.bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        log(f"created bucket {cfg.bucket}")
    # Re-applied every time so buckets created by v1 (which expired everything) get fixed:
    # only build contexts expire; template/sandbox records must stay.
    s3.put_bucket_lifecycle_configuration(Bucket=cfg.bucket, LifecycleConfiguration={"Rules": [{
        "ID": "expire-build-contexts", "Status": "Enabled", "Filter": {"Prefix": "contexts/"},
        "Expiration": {"Days": 7}}]})

    # 2. ECR repo with scan-on-push. All templates share one repo, one tag per build.
    try:
        ecr.create_repository(repositoryName=cfg.ecr_repo,
                              imageScanningConfiguration={"scanOnPush": True})
        log(f"created ECR repo {cfg.ecr_repo}")
    except ClientError as exc:
        if _code(exc) != "RepositoryAlreadyExistsException":
            raise

    # 3. CodeBuild service role, least privilege: read build contexts from our bucket,
    #    push to our one ECR repo, write its own log group.
    role_arn = f"arn:aws:iam::{cfg.account}:role/{cfg.codebuild_role}"
    try:
        iam.get_role(RoleName=cfg.codebuild_role)
    except ClientError:
        iam.create_role(RoleName=cfg.codebuild_role, AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                     "Principal": {"Service": "codebuild.amazonaws.com"}}]}))
        repo_arn = f"arn:aws:ecr:{cfg.region}:{cfg.account}:repository/{cfg.ecr_repo}"
        iam.put_role_policy(RoleName=cfg.codebuild_role, PolicyName="build", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
                {"Effect": "Allow", "Resource": repo_arn, "Action": [
                    "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer"]},
                {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                 "Resource": f"arn:aws:s3:::{cfg.bucket}/*"},
                {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                 "Resource": f"arn:aws:logs:{cfg.region}:{cfg.account}:log-group:/aws/codebuild/{cfg.codebuild_project}*"},
            ]}))
        log(f"created role {cfg.codebuild_role}; waiting for IAM propagation")
        time.sleep(15)

    # 4. CodeBuild project. ARM_CONTAINER = native Graviton build host; privilegedMode is
    #    required to run the Docker daemon inside CodeBuild. The source location is a
    #    placeholder: every build overrides it with that build's context zip.
    if not cb.batch_get_projects(names=[cfg.codebuild_project])["projects"]:
        for attempt in range(6):
            try:
                cb.create_project(
                    name=cfg.codebuild_project,
                    source={"type": "S3", "location": f"{cfg.bucket}/placeholder.zip", "buildspec": BUILDSPEC},
                    artifacts={"type": "NO_ARTIFACTS"},
                    environment={"type": "ARM_CONTAINER", "computeType": "BUILD_GENERAL1_LARGE",
                                 "image": "aws/codebuild/amazonlinux-aarch64-standard:3.0",
                                 "privilegedMode": True},
                    serviceRole=role_arn, timeoutInMinutes=60,
                    logsConfig={"cloudWatchLogs": {"status": "ENABLED"}})
                log(f"created CodeBuild project {cfg.codebuild_project}")
                break
            except ClientError as exc:
                if _code(exc) == "InvalidInputException" and attempt < 5:  # role not assumable yet
                    time.sleep(10)
                    continue
                raise
    else:
        # Keep an existing project's buildspec current (v1 had no $NO_CACHE).
        cb.update_project(name=cfg.codebuild_project,
                          source={"type": "S3", "location": f"{cfg.bucket}/placeholder.zip", "buildspec": BUILDSPEC})
