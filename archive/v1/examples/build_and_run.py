"""Same code shape as E2B's docs (template build + sandbox), running on AgentCore.

    E2B_AGENTCORE_EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/<harness-exec-role> \
        python examples/build_and_run.py Dockerfile.fixed ubuntu24-base
"""
import sys
import time

from e2b_agentcore import Sandbox, Template

dockerfile = sys.argv[1] if len(sys.argv) > 1 else "Dockerfile"
name = sys.argv[2] if len(sys.argv) > 2 else "ubuntu24-base"

t0 = time.time()
template = Template().from_dockerfile(dockerfile)
info = Template.build(template, name, on_build_logs=lambda e: print(e, flush=True))
print(f"\nBUILD OK in {time.time() - t0:.0f}s: {info}\n", flush=True)

t1 = time.time()
sbx = Sandbox.create(name, envs={"GREETING": "hello-from-e2b-shim"})
print(f"sandbox {sbx.sandbox_id} ready in {time.time() - t1:.1f}s")
try:
    for cmd in ["uname -m", "cat /etc/os-release | head -2", "git --version", "echo $GREETING $TZ"]:
        t = time.time()
        r = sbx.commands.run(cmd)
        print(f"$ {cmd}  [{time.time() - t:.2f}s]\n{r.stdout.strip()}")
    sbx.files.write("/root/work/hello.py", "print('hi from file')\n")
    print([e.path for e in sbx.files.list("/root/work")])
    print(sbx.commands.run("python3 hello.py || echo no-python", cwd="/root/work").stdout.strip())
    try:
        sbx.commands.run("exit 3")
    except Exception as exc:
        print("non-zero exit raised:", type(exc).__name__, exc.exit_code)
    again = Sandbox.connect(sbx.sandbox_id)
    print("connect() sees file:", again.files.read("/root/work/hello.py").strip())
finally:
    print("kill:", sbx.kill())
