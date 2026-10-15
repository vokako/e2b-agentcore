"""Config and one-time infrastructure for building E2B-style templates on AgentCore.

Everything is derived from the caller's AWS credentials plus a few env vars, so the
package works in any account:

  E2B_AGENTCORE_REGION          region for ECR / CodeBuild / AgentCore (default us-east-2)
  E2B_AGENTCORE_EXEC_ROLE_ARN   harness execution role (needs ECR pull + logs)
  E2B_AGENTCORE_MODEL_ID        model the harness is configured with (required by the API,
                                not used: we only use the harness as a sandbox)
  E2B_AGENTCORE_PREFIX          resource name prefix (default e2b-agentcore)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from functools import cached_property

import boto3
from botocore.exceptions import ClientError


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


@dataclass
class Config:
    region: str = os.environ.get("E2B_AGENTCORE_REGION", "us-east-2")
    exec_role_arn: str | None = os.environ.get("E2B_AGENTCORE_EXEC_ROLE_ARN")
    model_id: str = os.environ.get("E2B_AGENTCORE_MODEL_ID", "us.anthropic.claude-sonnet-5")
    prefix: str = os.environ.get("E2B_AGENTCORE_PREFIX", "e2b-agentcore")

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


BUILDSPEC = """version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $REGISTRY
  build:
    commands:
      - docker build --platform linux/arm64 -f $DOCKERFILE -t $IMAGE .
  post_build:
    commands:
      - if [ "$CODEBUILD_BUILD_SUCCEEDING" = "1" ]; then docker push $IMAGE; fi
"""


def ensure_infra(cfg: Config, log=print) -> None:
    """Idempotently create the S3 source bucket, ECR repo, CodeBuild role and ARM project."""
    s3, ecr, iam, cb = (cfg.client(n) for n in ("s3", "ecr", "iam", "codebuild"))

    try:
        s3.head_bucket(Bucket=cfg.bucket)
    except ClientError:
        kw = {} if cfg.region == "us-east-1" else {
            "CreateBucketConfiguration": {"LocationConstraint": cfg.region}}
        s3.create_bucket(Bucket=cfg.bucket, **kw)
        s3.put_public_access_block(Bucket=cfg.bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_lifecycle_configuration(Bucket=cfg.bucket, LifecycleConfiguration={"Rules": [{
            "ID": "expire-build-contexts", "Status": "Enabled", "Filter": {"Prefix": ""},
            "Expiration": {"Days": 7}}]})
        log(f"created bucket {cfg.bucket}")

    try:
        ecr.create_repository(repositoryName=cfg.ecr_repo,
                              imageScanningConfiguration={"scanOnPush": True})
        log(f"created ECR repo {cfg.ecr_repo}")
    except ClientError as exc:
        if _code(exc) != "RepositoryAlreadyExistsException":
            raise

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

    try:
        cb.batch_get_projects(names=[cfg.codebuild_project])["projects"][0]
    except IndexError:
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
