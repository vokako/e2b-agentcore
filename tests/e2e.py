"""End-to-end test of the AgentCore provider against a real account.

    E2B_AGENTCORE_EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/<harness-exec-role> \
        python tests/e2e.py build      # build the two templates (about 3-5 min)
    ... python tests/e2e.py run        # exercise every supported method

Template "dsl-demo" uses the official E2B builder DSL (apt_install, copy, set_envs,
set_workdir, set_start_cmd + wait_for_port). Template "ubuntu24-base" is the customer
Dockerfile via from_dockerfile.
"""
import os
import sys
import time
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import e2b  # noqa: E402
from e2b.sandbox.filesystem.filesystem import WriteEntry  # noqa: E402
import e2b_agentcore as ac  # noqa: E402
from e2b_agentcore import Sandbox, Template  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}".rstrip(), flush=True)


def raises(exc_type, fn):
    try:
        fn()
    except exc_type as exc:
        return exc
    except Exception as exc:  # wrong type
        return AssertionError(f"raised {type(exc).__name__}: {exc}")
    return AssertionError("did not raise")


def build():
    dsl = (Template(file_context_path=HERE)
           .from_ubuntu_image("24.04")
           .apt_install(["python3", "git", "iproute2", "curl"])
           .copy("fixture.txt", "/opt/fixture.txt", mode=0o644)
           .set_envs({"APP_MODE": "demo mode"})
           .set_workdir("/work")
           .set_start_cmd("python3 -m http.server 8000", e2b.wait_for_port(8000)))
    t0 = time.time()
    info = Template.build_in_background(dsl, "dsl-demo")        # non-blocking path
    print("background build started:", info)
    customer = Template().from_dockerfile(os.path.join(HERE, "..", "Dockerfile.fixed"))
    Template.build(customer, "ubuntu24-base", on_build_logs=e2b.default_build_logger())  # blocking path
    print(f"ubuntu24-base built in {time.time() - t0:.0f}s")
    while True:
        st = Template.get_build_status(info)
        if st.status.value in ("ready", "error"):
            print("dsl-demo:", st.status, st.reason)
            break
        time.sleep(10)
    check("Template.exists", Template.exists("dsl-demo") and Template.alias_exists("ubuntu24-base"))


def run():
    t = time.time()
    sbx = Sandbox.create("dsl-demo", envs={"GREETING": "hi there"}, metadata={"suite": "e2e"})
    check("create + start_cmd ready", True, f"{time.time() - t:.1f}s id={sbx.sandbox_id}")
    try:
        c = sbx.commands
        r = c.run("uname -m; pwd; echo $GREETING; echo $APP_MODE")
        check("run: arch/cwd/envs", r.stdout.split("\n")[:4] == ["aarch64", "/work", "hi there", "demo mode"], repr(r.stdout))
        check("run: per-command envs+cwd", c.run("echo $X; pwd", envs={"X": "1"}, cwd="/tmp").stdout == "1\n/tmp\n")
        streamed = []
        c.run("echo a; echo b >&2", on_stdout=streamed.append, on_stderr=streamed.append)
        check("run: on_stdout/on_stderr", "".join(streamed).replace("\n", "") in ("ab", "ba"))
        e = raises(e2b.CommandExitException, lambda: c.run("echo oops >&2; exit 3"))
        check("run: non-zero -> e2b.CommandExitException", getattr(e, "exit_code", None) == 3 and "oops" in e.stderr)
        e = raises(e2b.TimeoutException, lambda: c.run("sleep 10", timeout=2))
        check("run: timeout -> e2b.TimeoutException", isinstance(e, e2b.TimeoutException), str(e)[:80])
        check("start_cmd serving :8000", c.run("curl -s -o /dev/null -w '%{http_code}' localhost:8000/").stdout == "200")

        h = c.run("for i in 1 2 3; do echo tick $i; sleep 1; done; exit 0", background=True)
        check("background: pid", h.pid > 0, f"pid={h.pid}")
        check("commands.list sees it", any(p.pid == h.pid for p in c.list()))
        got = []
        res = c.connect(h.pid).wait(on_stdout=got.append)
        check("connect(pid).wait streams", res.exit_code == 0 and "tick 3" in res.stdout and "".join(got) == res.stdout)
        h2 = c.run("sleep 300", background=True)
        check("commands.kill", c.kill(h2.pid) and not any(p.pid == h2.pid for p in c.list()))
        check("kill unknown pid -> False", c.kill(999999) is False)

        f = sbx.files
        w = f.write("/work/sub/a.txt", "hello\n")
        check("files.write -> e2b.WriteInfo", isinstance(w, e2b.WriteInfo) and w.type == e2b.FileType.FILE)
        check("files.read text", f.read("/work/sub/a.txt") == "hello\n")
        blob = os.urandom(200_000)
        t = time.time()
        f.write("/work/blob.bin", blob)
        tw = time.time() - t
        check("files.write/read 200 KB bytes", bytes(f.read("/work/blob.bin", format="bytes")) == blob, f"write {tw:.1f}s")
        f.write_files([{"path": "/work/m/1.txt", "data": "1"}, WriteEntry(path="/work/m/2.txt", data=b"2")])
        ls = f.list("/work/m")
        check("write_files + list -> e2b.EntryInfo", sorted(e.name for e in ls) == ["1.txt", "2.txt"]
              and all(isinstance(e, e2b.EntryInfo) and e.size == 1 for e in ls))
        gi = f.get_info("/opt/fixture.txt")
        check("get_info: COPY mode/owner", gi.permissions == "-rw-r--r--" and gi.owner == "root", f"{gi.permissions} {gi.mode:o}")
        check("list depth=2 includes nested", any(e.path == "/work/sub/a.txt" for e in f.list("/work", depth=2)))
        check("make_dir new/existing", f.make_dir("/work/d") is True and f.make_dir("/work/d") is False)
        check("exists", f.exists("/work/d") and not f.exists("/work/nope"))
        check("rename -> EntryInfo", f.rename("/work/sub/a.txt", "/work/b.txt").name == "b.txt")
        f.remove("/work/b.txt")
        e = raises(e2b.FileNotFoundException, lambda: f.read("/work/b.txt"))
        check("read missing -> e2b.FileNotFoundException", isinstance(e, e2b.FileNotFoundException))

        r = sbx.git.init("/work/repo")
        check("sbx.git (official helper)", c.run("git -C /work/repo rev-parse --is-inside-work-tree").stdout.strip() == "true")

        info = sbx.get_info()
        check("get_info -> e2b.SandboxInfo", isinstance(info, e2b.SandboxInfo) and info.metadata == {"suite": "e2e"}
              and info.cpu_count > 0, f"cpu={info.cpu_count} mem={info.memory_mb}MB")
        m = sbx.get_metrics()[0]
        check("get_metrics", m.mem_total > 0 and m.disk_total > 0, f"cpu%={m.cpu_used_pct} mem={m.mem_used >> 20}MB")
        p = Sandbox.list(query=e2b.SandboxQuery(metadata={"suite": "e2e"}))
        ids = [i.sandbox_id for i in p.next_items()]
        check("Sandbox.list(query) paginator", sbx.sandbox_id in ids and p.has_next is False)
        check("is_running", sbx.is_running())

        again = Sandbox.connect(sbx.sandbox_id)
        check("Sandbox.connect keeps state + envs", again.files.read("/work/m/1.txt") == "1"
              and again.commands.run("echo $GREETING").stdout == "hi there\n")
        with warnings.catch_warnings(record=True) as wl:
            warnings.simplefilter("always")
            sbx.set_timeout(600)
        check("set_timeout warns", len(wl) == 1)
        for name, fn in [("pause", sbx.pause), ("get_host", lambda: sbx.get_host(8000)),
                         ("watch_dir", lambda: f.watch_dir("/work")), ("pty", lambda: sbx.pty)]:
            e = raises(ac.NotSupportedException, fn)
            check(f"{name} -> NotSupportedException", isinstance(e, ac.NotSupportedException))
    finally:
        check("Sandbox.kill(sandbox_id)", Sandbox.kill(sbx.sandbox_id) is True)
    e = raises(e2b.SandboxNotFoundException, lambda: Sandbox.connect(sbx.sandbox_id))
    check("connect after kill -> SandboxNotFoundException", isinstance(e, e2b.SandboxNotFoundException))

    # Provider switch: unchanged `from e2b import Sandbox` code, customer Dockerfile template.
    ac.install()
    from e2b import Sandbox as S
    with S.create("ubuntu24-base") as s2:
        out = s2.commands.run("git --version; grep -h ^URIs /etc/apt/sources.list.d/ubuntu.sources; echo $TZ").stdout
        check("install(): from e2b import Sandbox -> AgentCore", "git version" in out and "aliyun" in out, repr(out))
    ac.uninstall()
    print(f"\n{ok} passed, {fail} failed")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    {"build": build, "run": run}[sys.argv[1]]()
