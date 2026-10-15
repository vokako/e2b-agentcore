"""Deploy an image as an AgentCore Runtime (HTTP protocol) and probe it.

usage: E2B_AGENTCORE_EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/<role> python rt_test.py <runtime_name> <image_uri>
"""
import json
import os
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-2")
# Execution role the runtime runs as (needs ECR pull + CloudWatch Logs).
ROLE = os.environ["E2B_AGENTCORE_EXEC_ROLE_ARN"]
name, image = sys.argv[1], sys.argv[2]
cp = boto3.client("bedrock-agentcore-control", region_name=REGION)
dp = boto3.client("bedrock-agentcore", region_name=REGION)

rt = next((r for r in cp.list_agent_runtimes()["agentRuntimes"] if r["agentRuntimeName"] == name), None)
spec = dict(agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}}, roleArn=ROLE,
            networkConfiguration={"networkMode": "PUBLIC"}, protocolConfiguration={"serverProtocol": "HTTP"},
            lifecycleConfiguration={"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600})
t0 = time.time()
if rt:
    r = cp.update_agent_runtime(agentRuntimeId=rt["agentRuntimeId"], **spec)
else:
    r = cp.create_agent_runtime(agentRuntimeName=name, **spec)
rid, arn = r["agentRuntimeId"], r["agentRuntimeArn"]
while True:
    g = cp.get_agent_runtime(agentRuntimeId=rid)
    if g["status"] in ("READY",) or g["status"].endswith("FAILED"):
        break
    time.sleep(5)
print(f"runtime {name} {g['status']} v{g.get('agentRuntimeVersion')} in {time.time()-t0:.0f}s {g.get('failureReason','')}")
print("arn", arn)
if g["status"] != "READY":
    sys.exit(1)

sid = "rt-test-" + uuid.uuid4().hex
print("session", sid)

# 1) agent invocation (/invocations)
t = time.time()
try:
    resp = dp.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sid, qualifier="DEFAULT",
                                   payload=json.dumps({"prompt": "ping"}).encode())
    body = resp["response"].read().decode()
    print(f"InvokeAgentRuntime OK [{time.time()-t:.1f}s] status={resp.get('statusCode')} body={body[:300]}")
except ClientError as exc:
    print(f"InvokeAgentRuntime FAILED [{time.time()-t:.1f}s]: {exc}")

# 2) shell exec
for cmd in ["uname -m", "git --version", "echo TZ=$TZ; grep ^URIs /etc/apt/sources.list.d/ubuntu.sources | head -1"]:
    t = time.time()
    try:
        resp = dp.invoke_agent_runtime_command(agentRuntimeArn=arn, runtimeSessionId=sid, qualifier="DEFAULT",
                                               contentType="application/json",
                                               accept="application/vnd.amazon.eventstream",
                                               body={"command": f"/bin/sh -c '{cmd}'", "timeout": 60})
        out, code = [], None
        for ev in resp["stream"]:
            c = ev.get("chunk") or {}
            d = c.get("contentDelta") or {}
            out.append(d.get("stdout", "") + d.get("stderr", ""))
            if c.get("contentStop"):
                code = c["contentStop"].get("exitCode")
        print(f"$ {cmd} -> exit {code} [{time.time()-t:.2f}s]: {''.join(out).strip()}")
    except ClientError as exc:
        print(f"$ {cmd} -> FAILED [{time.time()-t:.1f}s]: {exc}")

try:
    dp.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=sid, qualifier="DEFAULT")
    print("session stopped")
except ClientError as exc:
    print("stop:", exc)
