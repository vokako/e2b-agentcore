/**
 * Config, one-time infrastructure and a tiny S3 registry for the AgentCore provider.
 *
 * Same resources, names and S3 record format as the Python package (e2b_agentcore), so a
 * template built from Python can be used from TypeScript and the other way round.
 *
 * Everything is derived from the caller's AWS credentials (the normal AWS SDK credential
 * chain, e.g. AWS_PROFILE) plus a few env vars, so the package works in any account:
 *
 *   E2B_AGENTCORE_REGION          region for ECR / CodeBuild / AgentCore (default us-east-2)
 *   E2B_AGENTCORE_EXEC_ROLE_ARN   execution role the Harness runs as. It needs ECR pull
 *                                 (ecr:GetAuthorizationToken, ecr:BatchGetImage,
 *                                 ecr:GetDownloadUrlForLayer) and CloudWatch Logs write.
 *   E2B_AGENTCORE_MODEL_ID        model the Harness is configured with. CreateHarness requires
 *                                 one, but this provider never calls the model: it only uses
 *                                 the Harness as a sandbox (its microVM + shell commands).
 *   E2B_AGENTCORE_PREFIX          name prefix for every resource created here (default e2b-agentcore)
 *   E2B_AGENTCORE_IDLE_TIMEOUT    Harness idle session timeout in seconds (default 900)
 *   E2B_AGENTCORE_MAX_LIFETIME    Harness max session lifetime in seconds (default 28800)
 *
 * Resources created on the first Template.build (see ensureInfra):
 *
 *   S3 bucket   <prefix>-src-<account>-<region>
 *                 contexts/...   zipped build contexts (expire after 7 days)
 *                 templates/...  one JSON record per template (start/ready cmd, workdir, image)
 *                 sandboxes/...  one JSON record per live sandbox (E2B metadata, start time)
 *                 builds/...     one JSON record per build (lets getBuildStatus finish the deploy)
 *   ECR repo    <prefix>/templates            one image tag per build: <alias>-<timestamp>
 *   IAM role    <prefix>-codebuild            what CodeBuild runs as (S3 read, ECR push, logs)
 *   CodeBuild   <prefix>-builder              ARM_CONTAINER project that runs `docker build`
 */
import {
  S3Client, GetObjectCommand, PutObjectCommand, DeleteObjectCommand, HeadBucketCommand,
  CreateBucketCommand, PutPublicAccessBlockCommand, PutBucketLifecycleConfigurationCommand,
  paginateListObjectsV2, type BucketLocationConstraint,
} from '@aws-sdk/client-s3'
import { ECRClient, CreateRepositoryCommand } from '@aws-sdk/client-ecr'
import { IAMClient, GetRoleCommand, CreateRoleCommand, PutRolePolicyCommand } from '@aws-sdk/client-iam'
import {
  CodeBuildClient, BatchGetProjectsCommand, CreateProjectCommand, UpdateProjectCommand,
} from '@aws-sdk/client-codebuild'
import { STSClient, GetCallerIdentityCommand } from '@aws-sdk/client-sts'
import { BedrockAgentCoreControlClient } from '@aws-sdk/client-bedrock-agentcore-control'
import { CloudWatchLogsClient } from '@aws-sdk/client-cloudwatch-logs'

export interface ConfigOpts {
  region?: string
  execRoleArn?: string
  modelId?: string
  prefix?: string
  idleTimeoutS?: number
  maxLifetimeS?: number
}

/** Error-code helper for AWS SDK v3 errors (they carry `name`, sometimes `Code`). */
export function awsCode(err: unknown): string {
  const e = err as { name?: string; Code?: string; $metadata?: { httpStatusCode?: number } }
  return e?.Code ?? e?.name ?? ''
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
export { sleep }

export class Config {
  readonly region: string
  readonly execRoleArn?: string
  readonly modelId: string
  readonly prefix: string
  // Harness lifecycle. E2B sets timeouts per sandbox; AgentCore sets them once per Harness,
  // so these apply to every sandbox of every template built with this config.
  readonly idleTimeoutS: number
  readonly maxLifetimeS: number

  private _account?: Promise<string>
  private _clients = new Map<string, unknown>()

  constructor(opts: ConfigOpts = {}) {
    const env = process.env
    this.region = opts.region ?? env.E2B_AGENTCORE_REGION ?? 'us-east-2'
    this.execRoleArn = opts.execRoleArn ?? env.E2B_AGENTCORE_EXEC_ROLE_ARN
    this.modelId = opts.modelId ?? env.E2B_AGENTCORE_MODEL_ID ?? 'us.anthropic.claude-sonnet-5'
    this.prefix = opts.prefix ?? env.E2B_AGENTCORE_PREFIX ?? 'e2b-agentcore'
    this.idleTimeoutS = opts.idleTimeoutS ?? Number(env.E2B_AGENTCORE_IDLE_TIMEOUT ?? 900)
    this.maxLifetimeS = opts.maxLifetimeS ?? Number(env.E2B_AGENTCORE_MAX_LIFETIME ?? 28800)
  }

  /** One cached client per service, all in this config's region. */
  private client<T>(key: string, make: () => T): T {
    if (!this._clients.has(key)) this._clients.set(key, make())
    return this._clients.get(key) as T
  }

  get s3() { return this.client('s3', () => new S3Client({ region: this.region })) }
  get ecr() { return this.client('ecr', () => new ECRClient({ region: this.region })) }
  get iam() { return this.client('iam', () => new IAMClient({ region: this.region })) }
  get codebuild() { return this.client('cb', () => new CodeBuildClient({ region: this.region })) }
  get logs() { return this.client('logs', () => new CloudWatchLogsClient({ region: this.region })) }
  get control() { return this.client('ctl', () => new BedrockAgentCoreControlClient({ region: this.region })) }

  account(): Promise<string> {
    this._account ??= new STSClient({ region: this.region }).send(new GetCallerIdentityCommand({}))
      .then((r) => r.Account!)
    return this._account
  }

  async bucket() { return `${this.prefix}-src-${await this.account()}-${this.region}` }
  get ecrRepo() { return `${this.prefix}/templates` }
  async registry() { return `${await this.account()}.dkr.ecr.${this.region}.amazonaws.com` }
  get codebuildProject() { return `${this.prefix}-builder` }
  get codebuildRole() { return `${this.prefix}-codebuild` }

  // ------------------------------------------------------------------ S3 registry
  // AgentCore has no API to list Runtime sessions or to attach metadata to them, and a
  // Harness has nowhere to keep E2B template settings (start/ready command, workdir).
  // Both are kept as small JSON objects in the build bucket instead. Keys and field names
  // (snake_case) match the Python package.

  async putJson(key: string, obj: unknown): Promise<void> {
    await this.s3.send(new PutObjectCommand({
      Bucket: await this.bucket(), Key: key, Body: JSON.stringify(obj), ContentType: 'application/json',
    }))
  }

  async getJson<T = Record<string, any>>(key: string): Promise<T | undefined> {
    try {
      const r = await this.s3.send(new GetObjectCommand({ Bucket: await this.bucket(), Key: key }))
      return JSON.parse(await r.Body!.transformToString()) as T
    } catch (err) {
      if (['NoSuchKey', 'NotFound', 'NoSuchBucket'].includes(awsCode(err))) return undefined
      throw err
    }
  }

  async deleteJson(key: string): Promise<void> {
    await this.s3.send(new DeleteObjectCommand({ Bucket: await this.bucket(), Key: key }))
  }

  async listJson<T = Record<string, any>>(prefix: string): Promise<T[]> {
    const out: T[] = []
    try {
      for await (const page of paginateListObjectsV2({ client: this.s3 }, { Bucket: await this.bucket(), Prefix: prefix })) {
        for (const obj of page.Contents ?? []) {
          const rec = await this.getJson<T>(obj.Key!)
          if (rec !== undefined) out.push(rec)
        }
      }
    } catch (err) {
      if (awsCode(err) !== 'NoSuchBucket') throw err
    }
    return out
  }
}

/**
 * CodeBuild buildspec. CodeBuild unzips the build context from S3 into its working
 * directory, then runs these phases on a native arm64 machine (AgentCore microVMs are
 * arm64-only, so building natively avoids slow, flaky QEMU emulation):
 *   pre_build   log Docker in to our ECR registry (the role allows ecr:GetAuthorizationToken)
 *   build       docker build. $DOCKERFILE is the Dockerfile we generated from the E2B
 *               template (Dockerfile.e2b); --platform pins arm64 even for multi-arch bases.
 *               $NO_CACHE is "--no-cache" when Template.build(..., { skipCache: true }).
 *   post_build  push only if the build succeeded (post_build runs even after a failure)
 * $IMAGE, $REGISTRY, $DOCKERFILE and $NO_CACHE are passed per build by startImageBuild.
 * DOCKER_BUILDKIT=1 enables `COPY --chmod`, which E2B's copy(..., { mode }) needs.
 * Identical to the Python BUILDSPEC: both SDKs share one CodeBuild project.
 */
export const BUILDSPEC = `version: 0.2
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
`

/**
 * Idempotently create the S3 bucket, ECR repo, CodeBuild role and ARM project.
 *
 * Safe to call before every build: each step first checks whether the resource exists.
 * Nothing here is ever deleted automatically.
 */
export async function ensureInfra(cfg: Config, log: (msg: string) => void = console.log): Promise<void> {
  const bucket = await cfg.bucket()
  const account = await cfg.account()

  // 1. S3 bucket: private, and build contexts under contexts/ expire after 7 days.
  try {
    await cfg.s3.send(new HeadBucketCommand({ Bucket: bucket }))
  } catch {
    await cfg.s3.send(new CreateBucketCommand({
      Bucket: bucket,
      ...(cfg.region === 'us-east-1' ? {} : {
        CreateBucketConfiguration: { LocationConstraint: cfg.region as BucketLocationConstraint },
      }),
    }))
    await cfg.s3.send(new PutPublicAccessBlockCommand({
      Bucket: bucket, PublicAccessBlockConfiguration: {
        BlockPublicAcls: true, IgnorePublicAcls: true, BlockPublicPolicy: true, RestrictPublicBuckets: true,
      },
    }))
    log(`created bucket ${bucket}`)
  }
  // Re-applied every time: only build contexts expire; template/sandbox records must stay.
  await cfg.s3.send(new PutBucketLifecycleConfigurationCommand({
    Bucket: bucket, LifecycleConfiguration: {
      Rules: [{
        ID: 'expire-build-contexts', Status: 'Enabled', Filter: { Prefix: 'contexts/' }, Expiration: { Days: 7 },
      }],
    },
  }))

  // 2. ECR repo with scan-on-push. All templates share one repo, one tag per build.
  try {
    await cfg.ecr.send(new CreateRepositoryCommand({
      repositoryName: cfg.ecrRepo, imageScanningConfiguration: { scanOnPush: true },
    }))
    log(`created ECR repo ${cfg.ecrRepo}`)
  } catch (err) {
    if (awsCode(err) !== 'RepositoryAlreadyExistsException') throw err
  }

  // 3. CodeBuild service role, least privilege: read build contexts from our bucket,
  //    push to our one ECR repo, write its own log group.
  const roleArn = `arn:aws:iam::${account}:role/${cfg.codebuildRole}`
  try {
    await cfg.iam.send(new GetRoleCommand({ RoleName: cfg.codebuildRole }))
  } catch {
    await cfg.iam.send(new CreateRoleCommand({
      RoleName: cfg.codebuildRole, AssumeRolePolicyDocument: JSON.stringify({
        Version: '2012-10-17', Statement: [{
          Effect: 'Allow', Action: 'sts:AssumeRole', Principal: { Service: 'codebuild.amazonaws.com' },
        }],
      }),
    }))
    const repoArn = `arn:aws:ecr:${cfg.region}:${account}:repository/${cfg.ecrRepo}`
    await cfg.iam.send(new PutRolePolicyCommand({
      RoleName: cfg.codebuildRole, PolicyName: 'build', PolicyDocument: JSON.stringify({
        Version: '2012-10-17', Statement: [
          { Effect: 'Allow', Action: 'ecr:GetAuthorizationToken', Resource: '*' },
          {
            Effect: 'Allow', Resource: repoArn, Action: [
              'ecr:BatchCheckLayerAvailability', 'ecr:InitiateLayerUpload', 'ecr:UploadLayerPart',
              'ecr:CompleteLayerUpload', 'ecr:PutImage', 'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'],
          },
          { Effect: 'Allow', Action: ['s3:GetObject', 's3:GetObjectVersion'], Resource: `arn:aws:s3:::${bucket}/*` },
          {
            Effect: 'Allow', Action: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
            Resource: `arn:aws:logs:${cfg.region}:${account}:log-group:/aws/codebuild/${cfg.codebuildProject}*`,
          },
        ],
      }),
    }))
    log(`created role ${cfg.codebuildRole}; waiting for IAM propagation`)
    await sleep(15000)
  }

  // 4. CodeBuild project. ARM_CONTAINER = native Graviton build host; privilegedMode is
  //    required to run the Docker daemon inside CodeBuild. The source location is a
  //    placeholder: every build overrides it with that build's context zip.
  const source = { type: 'S3' as const, location: `${bucket}/placeholder.zip`, buildspec: BUILDSPEC }
  const existing = await cfg.codebuild.send(new BatchGetProjectsCommand({ names: [cfg.codebuildProject] }))
  if (!existing.projects?.length) {
    for (let attempt = 0; ; attempt++) {
      try {
        await cfg.codebuild.send(new CreateProjectCommand({
          name: cfg.codebuildProject, source, artifacts: { type: 'NO_ARTIFACTS' },
          environment: {
            type: 'ARM_CONTAINER', computeType: 'BUILD_GENERAL1_LARGE',
            image: 'aws/codebuild/amazonlinux-aarch64-standard:3.0', privilegedMode: true,
          },
          serviceRole: roleArn, timeoutInMinutes: 60, logsConfig: { cloudWatchLogs: { status: 'ENABLED' } },
        }))
        log(`created CodeBuild project ${cfg.codebuildProject}`)
        break
      } catch (err) {
        if (awsCode(err) === 'InvalidInputException' && attempt < 5) { // role not assumable yet
          await sleep(10000)
          continue
        }
        throw err
      }
    }
  } else {
    // Keep an existing project's buildspec current.
    await cfg.codebuild.send(new UpdateProjectCommand({ name: cfg.codebuildProject, source }))
  }
}
