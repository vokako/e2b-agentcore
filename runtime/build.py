"""Build runtime/Dockerfile into an arm64 image in ECR, for a plain AgentCore Runtime.

    AWS_PROFILE=default python runtime/build.py          # prints the image URI on the last line
    python runtime/rt_test.py sbx_ubuntu24 <image-uri>   # then deploy + smoke-test it

Use this path when the image runs its OWN server (here runtime/server.py on :8080). For a
plain sandbox image with no server, use e2b_agentcore.Template.build instead: it deploys
to a Harness, which needs no server in the image.

What it does, step by step
--------------------------
1. Parse runtime/Dockerfile with the official E2B parser (Template().from_dockerfile).
   file_context_path=runtime/ so `COPY server.py ...` resolves next to the Dockerfile.
2. start_image_build() (shared with Template.build, see e2b_agentcore/template.py):
     a. ensure_infra: first run only, creates the S3 bucket, ECR repo e2b-agentcore/templates,
        CodeBuild role e2b-agentcore-codebuild and ARM project e2b-agentcore-builder
     b. render the Dockerfile again from the parsed steps. include_start_cmd=True keeps the
        CMD as ENTRYPOINT: a Runtime container must start its server by itself.
        (Template.build leaves it out, because Sandbox.create starts it, as E2B does.)
     c. zip the Dockerfile plus only the files COPY needs (here: server.py)
     d. upload the zip to s3://e2b-agentcore-src-<account>-<region>/contexts/runtime/
     e. start CodeBuild: native arm64 `docker build`, push to
        <account>.dkr.ecr.<region>.amazonaws.com/e2b-agentcore/templates:runtime-ubuntu24-<ts>
3. wait_image_build(): poll CodeBuild every 5 s and print its log until it ends.

Region and prefix come from E2B_AGENTCORE_REGION (default us-east-2) / E2B_AGENTCORE_PREFIX.
No AgentCore resource is created or changed here; rt_test.py does that.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from e2b_agentcore import Config, Template  # noqa: E402
from e2b_agentcore.template import start_image_build, wait_image_build  # noqa: E402

cfg = Config()

# Step 1: parse. Any Dockerfile the E2B SDK can parse works here.
template = Template(file_context_path=HERE).from_dockerfile(os.path.join(HERE, "Dockerfile"))

# Step 2: upload + start the arm64 build (returns as soon as CodeBuild accepted it).
build_id, image = start_image_build(cfg, template, "runtime-ubuntu24", log=print, include_start_cmd=True)

# Step 3: follow the build; raises e2b.BuildException if docker build or push failed.
wait_image_build(cfg, build_id, log=print)
print(image)
