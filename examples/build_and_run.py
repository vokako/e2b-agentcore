"""The E2B quick-start, running on AgentCore. Only the import line differs from E2B's docs.

    E2B_AGENTCORE_EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/<harness-exec-role> \
        python examples/build_and_run.py Dockerfile.fixed ubuntu24-base

Build: the Dockerfile is parsed by the official E2B SDK, built natively for arm64 on
CodeBuild, pushed to ECR and deployed as AgentCore Harness `e2b_<name>` (first build about
3-5 min, mostly apt). Re-running rebuilds and updates the same Harness.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from e2b_agentcore import Sandbox, Template, default_build_logger  # was: from e2b import ...

dockerfile = sys.argv[1] if len(sys.argv) > 1 else "Dockerfile"
name = sys.argv[2] if len(sys.argv) > 2 else "ubuntu24-base"

# ---- build: same call as e2b.Template.build -------------------------------------------
t0 = time.time()
Template.build(Template().from_dockerfile(dockerfile), name, on_build_logs=default_build_logger())
print(f"\nbuilt {name} in {time.time() - t0:.0f}s\n")

# ---- use: same calls as e2b.Sandbox -----------------------------------------------------
with Sandbox.create(name, envs={"GREETING": "hello"}) as sbx:
    print(sbx.sandbox_id)
    print(sbx.commands.run("uname -m; git --version; echo $GREETING $TZ").stdout)
    sbx.files.write("/root/work/hello.txt", "hi\n")
    print([e.name for e in sbx.files.list("/root/work")])
