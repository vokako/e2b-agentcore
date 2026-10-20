"""Full test of the Python provider: one or more checks for EVERY row of the support table.

    export E2B_AGENTCORE_EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/<harness-exec-role>
    python tests/test_full.py offline   # no resources created (a few read-only AWS calls)
    python tests/test_full.py build     # builds full-py, full-py-broken, full-py-del (~5 min)
    python tests/test_full.py run       # sandboxes on full-py (~4 min)
    python tests/test_full.py all       # offline + build + run

    E2B_AGENTCORE_PLATFORM_VERSION=V2 python tests/test_full.py all
                                        # same suite on platform V2 (plain Runtime with
                                        # platformVersion=V2); templates get a -v2 suffix

How completeness is enforced: tools/api_coverage.py generates the README's support table
from the installed e2b package. Every check() names the table rows it covers, and the run
fails if any row of the table is not covered by at least one check of the stages that ran
(`all` must cover every row). The checks test what the table SAYS:
  yes      the method works like E2B's
  partial  it works, and the difference written in the Notes column is real
  no       calling it raises NotSupportedException

Cross-language checks (Python <-> JS sharing templates and sandboxes) run when
js/dist/test/interop.js exists (cd js && npm run build).
"""
import io
import json
import os
import shutil
import subprocess
import sys
import time
import warnings
import zipfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
FIX = os.path.join(HERE, "fixtures", "full")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import boto3  # noqa: E402
import e2b  # noqa: E402
from e2b.sandbox.filesystem.filesystem import WriteEntry  # noqa: E402

import api_coverage  # noqa: E402
import e2b_agentcore as ac  # noqa: E402
from e2b_agentcore import Config, Sandbox, Template  # noqa: E402
from e2b_agentcore.sandbox import CommandHandle  # noqa: E402
from e2b_agentcore.template import _render_dockerfile, _zip_context, harness_name, mirror_image  # noqa: E402

PV = Config().platform_version            # V1 (Harness) or V2 (plain Runtime), from the env
SFX = "" if PV == "V1" else "-v2"         # V1 and V2 suites use separate templates
FULL, BROKEN = "full-py" + SFX, "full-py-broken" + SFX
# Unique per run: a deleted Harness stays DELETING for minutes and keeps its name taken.
DEL = f"full-py-del{SFX}-{int(time.time()) % 100000}"
INTEROP = os.path.join(ROOT, "js", "dist", "test", "interop.js")
ROWS = {name: (status, note) for name, status, note in api_coverage.rows()}
TEMPLATE_DSL_ROW = "Template.from_* / apt_install / copy / run_cmd / set_* ..."

covered, results = set(), {"pass": 0, "fail": 0}


def check(name, cond, detail="", covers=()):
    for c in covers:
        if c not in ROWS:
            raise KeyError(f"check {name!r} claims to cover unknown table row {c!r}")
        covered.add(c)
    results["pass" if cond else "fail"] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}".rstrip(), flush=True)


def raises(exc_type, fn):
    """The exception if fn() raised exc_type; otherwise an AssertionError describing what happened."""
    try:
        fn()
    except exc_type as exc:
        return exc
    except Exception as exc:  # wrong type
        return AssertionError(f"raised {type(exc).__name__}: {exc}")
    return AssertionError("did not raise")


def caught_warnings(fn):
    with warnings.catch_warnings(record=True) as wl:
        warnings.simplefilter("always")
        out = fn()
    return out, [str(w.message) for w in wl]


def full_template():
    """Uses every kind of E2B builder step, so the DSL row is tested end to end."""
    return (Template(file_context_path=FIX, file_ignore_patterns=["app/*.tmp"])
            .from_ubuntu_image("24.04")
            .set_envs({"APP_MODE": "demo mode", "QUOTED": 'say "hi" $x', "PIP_BREAK_SYSTEM_PACKAGES": "1"})
            .apt_install(["python3", "python3-pip", "git", "curl", "iproute2"])  # iproute2: ss, for wait_for_port
            .pip_install(["six"])
            .copy("app", "/opt/app")                                  # dir; .dockerignore drops *.env
            .copy("run.sh", "/usr/local/bin/run-ok", mode=0o755)
            .copy("app/keep.txt", "/opt/owned.txt", user="nobody")
            .make_dir("/opt/nb", mode=0o777)
            .run_cmd("whoami > /opt/nb/whoami", user="nobody")
            .make_symlink("/opt/app/keep.txt", "/opt/keep-link")
            .run_cmd("echo moved > /opt/before")
            .rename("/opt/before", "/opt/after")
            .run_cmd("touch /opt/gone")
            .remove("/opt/gone")
            .set_workdir("/work")
            .set_start_cmd("python3 -m http.server 8000", e2b.wait_for_port(8000)))


# =============================================================================== offline

def offline():
    print("\n== offline")
    missing = [n for n, (s, _) in ROWS.items() if s == "missing"]
    check("coverage table: no e2b method missing", not missing, ", ".join(missing))

    df = _render_dockerfile(full_template())
    check("render: ENV values quoted, $ escaped", 'QUOTED="say \\"hi\\" \\$x"' in df, covers=[TEMPLATE_DSL_ROW])
    check("render: COPY --chmod / --chown", "COPY --chmod=0755 run.sh /usr/local/bin/run-ok" in df
          and "COPY --chown=nobody app/keep.txt /opt/owned.txt" in df)
    check("render: run_cmd(user=) -> USER around RUN",
          "USER nobody\nRUN whoami > /opt/nb/whoami\nUSER root" in df)
    check("render: start cmd not baked in", "ENTRYPOINT" not in df and df.rstrip().endswith("USER root"))
    v2df = _render_dockerfile(full_template(), with_server=True)
    check("render V2: Go builder stage first, user image last",
          v2df.startswith("FROM golang:1.24-alpine AS e2b_agentcore_server\n")
          and v2df.count("\nFROM ") == 1 and "CGO_ENABLED=0" in v2df)
    check("render V2: static server is the ENTRYPOINT, start cmd not baked in",
          v2df.rstrip().endswith('COPY --from=e2b_agentcore_server /server /opt/e2b-agentcore/server\n'
                                 'ENTRYPOINT ["/opt/e2b-agentcore/server"]\nCMD []'))
    z = zipfile.ZipFile(io.BytesIO(_zip_context(full_template(), v2df, {ac.template.SERVER_FILE: ac.template.SERVER_GO})))
    check("zip V2: server source shipped in the build context",
          ac.template.SERVER_FILE in z.namelist() and b"0.0.0.0:8080" in z.read(ac.template.SERVER_FILE))
    e = raises(ValueError, lambda: Config(platform_version="V3"))
    check("Config(platform_version) accepts only V1 / V2", isinstance(e, ValueError)
          and Config(platform_version="v2").platform_version == "V2")
    check("render: include_start_cmd -> ENTRYPOINT",
          _render_dockerfile(full_template(), include_start_cmd=True).rstrip().endswith(
              "ENTRYPOINT python3 -m http.server 8000"))
    names = sorted(zipfile.ZipFile(io.BytesIO(_zip_context(full_template(), df))).namelist())
    check("zip: COPY sources only; .dockerignore and file_ignore_patterns applied",
          names == ["Dockerfile.e2b", "app/keep.txt", "app/sub/nested.txt", "run.sh"], str(names))
    check("harness name mapping", harness_name("a-b.c") == "e2b_a_b_c")
    m = "public.ecr.aws/docker/library"
    check("mirror: official images -> ECR Public; others unchanged",
          [mirror_image(i, m) for i in ["ubuntu:24.04", "library/python:3.12", "user/repo:1", "ghcr.io/a/b",
                                        "localhost:5000/x"]] == [f"{m}/ubuntu:24.04", f"{m}/python:3.12", "user/repo:1",
                                                                 "ghcr.io/a/b", "localhost:5000/x"]
          and mirror_image("ubuntu:24.04", "") == "ubuntu:24.04")
    check("render: FROM uses the configured mirror", _render_dockerfile(full_template(), mirror=m).startswith(
        f"FROM {m}/ubuntu:24.04\n") and Config().dockerhub_mirror == m)
    e = raises(e2b.TemplateException, lambda: harness_name("x" * 37))
    check("template name too long -> TemplateException", isinstance(e, e2b.TemplateException))
    e = raises(e2b.TemplateException, lambda: Template.build(Template().from_template("base"), "x"))
    check("from_template() base -> TemplateException", isinstance(e, e2b.TemplateException), str(e)[:70])
    e = raises(e2b.TemplateException, lambda: Template.build(full_template(), "x", config=Config(exec_role_arn=None)))
    check("build without exec role -> TemplateException", isinstance(e, e2b.TemplateException))

    # Every "no" row: calling it raises NotSupportedException.
    sbx = Sandbox(Config(), {"sandbox_id": "sbx-offline", "template": "t", "harness_arn": "arn", "started_at": 0,
                             "end_at": 0})
    objs = {"Sandbox": sbx, "Commands": sbx.commands, "Filesystem": sbx.files,
            "CommandHandle": CommandHandle(sbx, 1, "tag"), "Template": Template}
    for name, (status, _) in ROWS.items():
        if status != "no":
            continue
        label, attr = name.split(".", 1)
        e = raises(ac.NotSupportedException, lambda: getattr(objs[label], attr)("x"))
        check(f"{name} -> NotSupportedException", isinstance(e, ac.NotSupportedException), covers=[name])
    e = raises(ac.NotSupportedException, lambda: sbx.commands.run("cat", stdin=True))
    check("commands.run(stdin=True) -> NotSupportedException", isinstance(e, ac.NotSupportedException),
          covers=["Commands.run"])

    check("class constants match e2b", (Sandbox.default_template, Sandbox.default_sandbox_timeout,
                                        Sandbox.default_mcp_template, Sandbox.mcp_port)
          == (e2b.Sandbox.default_template, e2b.Sandbox.default_sandbox_timeout,
              e2b.Sandbox.default_mcp_template, e2b.Sandbox.mcp_port),
          covers=["Sandbox.default_template", "Sandbox.default_sandbox_timeout", "Sandbox.default_mcp_template",
                  "Sandbox.mcp_port"])
    check("sandbox_domain is None", sbx.sandbox_domain is None, covers=["Sandbox.sandbox_domain"])

    ac.install()
    check("install(): e2b.Sandbox/Template -> provider", e2b.Sandbox is Sandbox and e2b.Template is Template)
    ac.uninstall()
    check("uninstall() restores e2b", e2b.Sandbox is not Sandbox and e2b.Template is not Template)
    check("AsyncSandbox/AsyncTemplate stay e2b's (not provided)",
          ac.AsyncSandbox is e2b.AsyncSandbox and ac.AsyncTemplate is e2b.AsyncTemplate)

    # AWS errors become e2b exceptions.
    bad = Config()
    bad.__dict__["session"] = boto3.Session(aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
                                            aws_secret_access_key="x" * 40, region_name=bad.region)
    e = raises(e2b.AuthenticationException, lambda: Template.exists("anything", config=bad))
    check("bad credentials -> e2b.AuthenticationException", isinstance(e, e2b.AuthenticationException), str(e)[:60])
    e = raises(e2b.NotFoundException, lambda: Sandbox.create("no-such-template-xyz"))
    check("create(unknown template) -> NotFoundException", isinstance(e, e2b.NotFoundException))
    e = raises(e2b.SandboxNotFoundException, lambda: Sandbox.connect("sbx" + "0" * 32))
    check("connect(unknown id) -> SandboxNotFoundException", isinstance(e, e2b.SandboxNotFoundException))
    check("Sandbox.kill(unknown id) -> False", Sandbox.kill("sbx" + "0" * 32) is False)
    e = raises(e2b.TemplateException, lambda: Template.get_build_status(
        e2b.BuildInfo(template_id="x", build_id="nope:123", name="x", alias="x", tags=[])))
    check("get_build_status(unknown build) -> TemplateException", isinstance(e, e2b.TemplateException))
    check("exists(unknown) -> False", Template.exists("no-such-template-xyz") is False)


# =============================================================================== build

def wait_status(info, want, limit=900):
    offset, logs, t0 = 0, [], time.time()
    while time.time() - t0 < limit:
        st = Template.get_build_status(info, logs_offset=offset)
        logs += st.log_entries
        offset += len(st.log_entries)
        if st.status.value in want:
            return st, logs
        time.sleep(10)
    raise TimeoutError(f"build {info.build_id} not {want} after {limit}s")


def build():
    print("\n== build")
    cfg = Config()
    broken = Template().from_ubuntu_image("24.04").run_cmd("echo about-to-fail; exit 7")
    tiny = Template().from_ubuntu_image("24.04")
    bg_broken = Template.build_in_background(broken, BROKEN)
    bg_del = Template.build_in_background(tiny, DEL)
    check("build_in_background returns BuildInfo", isinstance(bg_broken, e2b.BuildInfo)
          and bg_broken.template_id == harness_name(BROKEN), covers=["Template.build_in_background"])

    path = "update" if Template.exists(FULL) else "create"
    logs, t0 = [], time.time()
    info = Template.build(full_template(), FULL, skip_cache=True, cpu_count=8, memory_mb=16384, min_free_disk_mb=1,
                          tags=["v1"], on_build_logs=logs.append)
    check(f"build ({path} {'Harness' if PV == 'V1' else 'V2 Runtime'}) -> BuildInfo", info.alias == FULL and info.tags == ["v1"],
          f"{time.time() - t0:.0f}s", covers=["Template.build"])
    check("build streams LogEntry lines", len(logs) > 20 and all(isinstance(x, e2b.LogEntry) for x in logs)
          and any("READY" in x.message for x in logs), f"{len(logs)} lines")
    env = {v["name"]: v["value"] for v in cfg.client("codebuild").batch_get_builds(ids=[info.build_id])["builds"][0]
           ["environment"]["environmentVariables"]}
    check("skip_cache -> docker build --no-cache", env.get("NO_CACHE") == "--no-cache")
    rec = ac.template.load_template(cfg, FULL)
    cp = cfg.client("bedrock-agentcore-control")
    if PV == "V2":
        rt = cp.get_agent_runtime(agentRuntimeId=rec["agent_runtime_id"])
        check("V2: template deployed as a Runtime with platformVersion=V2",
              rec["platform_version"] == "V2" and rt["platformVersion"] == "V2" and rt["status"] == "READY"
              and rt["agentRuntimeArn"] == rec["agent_runtime_arn"], f"{rt['agentRuntimeName']} {rt['platformVersion']}")
    else:
        h = cp.get_harness(harnessId=rec["harness_id"])["harness"]
        rt = cp.get_agent_runtime(agentRuntimeId=h["environment"]["agentCoreRuntimeEnvironment"]["agentRuntimeId"])
        check("V1: template deployed as a Harness (backing runtime is V1)",
              rec.get("platform_version", "V1") == "V1" and rt.get("platformVersion") == "V1", rt["agentRuntimeName"])
    # The template built by the other platform's suite, if that suite has run in this account.
    other = "full-py" if PV == "V2" else "full-py-v2"
    if ac.template.load_template(cfg, other):
        e = raises(e2b.TemplateException, lambda: Template.build(tiny, other))
        check("rebuilding a template on the other platform is refused before any build",
              isinstance(e, e2b.TemplateException) and "delete" in str(e), str(e)[:80])
    check("exists / alias_exists", Template.exists(FULL) and Template.alias_exists(FULL),
          covers=["Template.exists", "Template.alias_exists"])

    st, blogs = wait_status(bg_broken, ("error",))
    check("failed build -> get_build_status ERROR + reason + logs", "FAILED" in (st.reason.message if st.reason else "")
          and any("about-to-fail" in x.message for x in blogs), st.reason.message if st.reason else "",
          covers=["Template.get_build_status"])
    e = raises(e2b.BuildException, lambda: Template.build(broken, BROKEN))
    check("failed build -> Template.build raises BuildException", isinstance(e, e2b.BuildException), str(e)[:60],
          covers=["Template.build"])
    check("failed build leaves no template", not Template.exists(BROKEN))

    wait_status(bg_del, ("ready",))
    t0 = time.time()
    Template.build(tiny.run_cmd("echo v2 > /v2"), DEL)
    check("rebuild an existing template (UpdateHarness)", Template.exists(DEL), f"{time.time() - t0:.0f}s",
          covers=["Template.build"])
    check("Template.delete -> True", Template.delete(DEL) is True)
    check("exists -> False right after delete (Harness still DELETING)", not Template.exists(DEL))
    check("Template.delete(again, still DELETING) -> False", Template.delete(DEL) is False)


# =============================================================================== run

def run():
    print("\n== run")
    cfg = Config()
    ignored = dict(timeout=60, network={"x": 1}, lifecycle={"x": 1}, allow_internet_access=False, mcp={"x": 1},
                   volume_mounts={"x": "y"})
    t0 = time.time()
    sbx, wl = caught_warnings(lambda: Sandbox.create(FULL, envs={"GREETING": "hi there", "SECRET_TOKEN": "s3cr3t"},
                                                     metadata={"suite": "full-py"}, **ignored))
    check("create + start cmd ready", sbx.sandbox_id.startswith("sbx"), f"{time.time() - t0:.1f}s",
          covers=["Sandbox.create", "Sandbox.sandbox_id"])
    check("create: 6 per-sandbox options warn, no effect", len(wl) == 6 and all("no per-sandbox effect" in w for w in wl),
          f"{len(wl)} warnings", covers=["Sandbox.create"])
    raw = cfg.client("s3").get_object(Bucket=cfg.bucket, Key=f"sandboxes/{sbx.sandbox_id}.json")["Body"].read()
    check("envs are not written to S3", b"s3cr3t" not in raw and b"SECRET_TOKEN" not in raw)
    sbx2 = None
    try:
        c, f = sbx.commands, sbx.files
        check("commands / files objects", isinstance(c, ac.Commands) and isinstance(f, ac.Files),
              covers=["Sandbox.commands", "Sandbox.files"])

        # ---- template DSL steps, as seen inside the sandbox
        out = c.run("python3 -c 'import six; print(six.__name__)'; echo \"$QUOTED\"; echo $APP_MODE; run-ok; "
                    "cat /opt/nb/whoami; stat -c %U /opt/owned.txt; readlink /opt/keep-link; cat /opt/after; "
                    "test -e /opt/gone || echo gone-ok; ls /opt/app /opt/app/sub; pwd").stdout.split("\n")
        expect = ["six", 'say "hi" $x', "demo mode", "run-ok", "nobody", "nobody", "/opt/app/keep.txt", "moved",
                  "gone-ok"]
        check("DSL: pip/env/copy mode+chown/run_cmd user/symlink/rename/remove", out[:9] == expect, repr(out[:9]),
              covers=[TEMPLATE_DSL_ROW])
        check("DSL: copy dir honours .dockerignore + ignore patterns, workdir",
              "keep.txt" in out and "secret.env" not in out and "skip.tmp" not in out and "nested.txt" in out
              and out[-2] == "/work", repr(out[9:]))
        check("start cmd serving :8000", c.run("curl -s -o /dev/null -w '%{http_code}' localhost:8000/").stdout == "200")

        # ---- commands.run
        r = c.run("uname -m; whoami; echo $GREETING")
        check("run: arm64, root by default, sandbox envs", r.stdout == "aarch64\nroot\nhi there\n" and
              isinstance(r, e2b.CommandResult), repr(r.stdout), covers=["Commands.run"])
        check("run: per-command envs + cwd", c.run("echo $X; pwd", envs={"X": "1"}, cwd="/tmp").stdout == "1\n/tmp\n")
        check("run: user=", c.run("whoami", user="nobody").stdout == "nobody\n")
        got = []
        c.run("echo a; echo b >&2", on_stdout=got.append, on_stderr=got.append)
        check("run: on_stdout / on_stderr", "".join(got).replace("\n", "") in ("ab", "ba"))
        e = raises(e2b.CommandExitException, lambda: c.run("echo oops >&2; exit 3"))
        check("run: non-zero -> CommandExitException", getattr(e, "exit_code", None) == 3 and "oops" in e.stderr)
        e = raises(e2b.TimeoutException, lambda: c.run("sleep 10", timeout=2))
        check("run: timeout -> TimeoutException", isinstance(e, e2b.TimeoutException))
        check("run: timeout=0 means no limit", c.run("sleep 2; echo ok", timeout=0).stdout == "ok\n")
        got = []
        r = c.run("sleep 1; echo long; exit 0", timeout=4000, on_stdout=got.append)
        check("run: timeout above one V2 call (4000 s) still works, streams output",
              r.stdout == "long\n" and "".join(got) == "long\n", f"platform {PV}")
        e = raises(e2b.CommandExitException, lambda: c.run("exit 4", timeout=4000))
        check("run: timeout above one V2 call keeps non-zero -> CommandExitException",
              getattr(e, "exit_code", None) == 4)
        big = c.run("python3 -c \"print('a' * 70000)\"").stdout
        if PV == "V1":
            check("run: V1: one output line > 64 KB is dropped (documented AgentCore limit)", "a" * 70000 not in big,
                  f"got {len(big)} chars", covers=["Commands.run"])
        else:
            check("run: V2: one output line > 64 KB arrives (no line limit on V2)", "a" * 70000 in big,
                  f"got {len(big)} chars", covers=["Commands.run"])
        check("run: same output wrapped arrives whole",
              c.run("python3 -c \"print('a' * 70000)\" | fold -w 1000").stdout.replace("\n", "") == "a" * 70000)

        # ---- background commands
        h = c.run("for i in 1 2 3; do echo t$i; sleep 1.5; done", background=True)
        check("background -> CommandHandle with pid", isinstance(h, CommandHandle) and h.pid > 0, f"pid={h.pid}",
              covers=["CommandHandle.pid"])
        check("commands.list sees it", any(p.pid == h.pid and "t$i" in p.args[-1] for p in c.list()),
              covers=["Commands.list"])
        chunks = [o for o, _e, _p in h if o]
        check("background output arrives in chunks", len(chunks) >= 2 and "".join(chunks) == "t1\nt2\nt3\n",
              f"{len(chunks)} chunks", covers=["Commands.run"])
        h = c.run("sleep 3; echo late", background=True)
        h.disconnect()
        got = []
        res = c.connect(h.pid).wait(on_stdout=got.append)
        check("disconnect, then commands.connect(pid).wait", res.stdout == "late\n" and "".join(got) == "late\n",
              covers=["CommandHandle.disconnect", "Commands.connect", "CommandHandle.wait"])
        h = c.run("seq 1 20000", background=True)
        check("background: 20000 lines arrive complete and in order",
              h.wait().stdout.split() == [str(i) for i in range(1, 20001)], covers=["CommandHandle.wait"])
        h = c.run("exit 4", background=True)
        e = raises(e2b.CommandExitException, h.wait)
        check("handle.wait on non-zero -> CommandExitException", getattr(e, "exit_code", None) == 4)
        h = c.run("sleep 300", background=True)
        check("handle.kill", h.kill() is True and not any(p.pid == h.pid for p in c.list()),
              covers=["CommandHandle.kill"])
        h = c.run("sleep 300", background=True)
        check("commands.kill", c.kill(h.pid) is True and not any(p.pid == h.pid for p in c.list()),
              covers=["Commands.kill"])
        check("commands.kill(unknown) -> False", c.kill(999999) is False)
        c.run("nohup sleep 120 > /dev/null 2>&1 &")
        check("commands.list: only SDK-started commands", not any("sleep 120" in " ".join(p.args) for p in c.list()),
              covers=["Commands.list"])
        e = raises(e2b.NotFoundException, lambda: c.connect(999999))
        check("commands.connect(unknown) -> NotFoundException", isinstance(e, e2b.NotFoundException))

        # ---- files
        w = f.write("/work/sub/a.txt", "hello\n")
        check("write str -> WriteInfo", isinstance(w, e2b.WriteInfo) and w.type == e2b.FileType.FILE
              and w.path == "/work/sub/a.txt", covers=["Filesystem.write"])
        check("read text", f.read("/work/sub/a.txt") == "hello\n", covers=["Filesystem.read"])
        blob = os.urandom(200_000)
        t0 = time.time()
        f.write("/work/blob.bin", blob)
        dt = time.time() - t0
        check("write bytes 200 KB at >= 30 KB/s (doc: ~100 KB/s)", 200 / dt >= 30, f"{200 / dt:.0f} KB/s",
              covers=["Filesystem.write"])
        check("read bytes -> bytearray", f.read("/work/blob.bin", format="bytes") == bytearray(blob))
        # Large reads used to lose their tail now and then (AgentCore cuts off big outputs);
        # read the same 1 MB file several times to make sure chunked, verified reads hold.
        mb = os.urandom(1_000_000)
        f.write("/work/mb.bin", mb)
        check("read 1 MB x5, every byte intact", all(f.read("/work/mb.bin", format="bytes") == mb for _ in range(5)),
              covers=["Filesystem.read"])
        f.write("/work/empty", b"")
        check("read empty file", f.read("/work/empty") == "")
        stream = list(f.read("/work/blob.bin", format="stream"))
        check("read stream -> one chunk", len(stream) == 1 and stream[0] == blob, covers=["Filesystem.read"])
        f.write("/work/io.txt", io.BytesIO(b"from io"), gzip=True, use_octet_stream=True)
        check("write file-like; gzip/use_octet_stream ignored", f.read("/work/io.txt") == "from io",
              covers=["Filesystem.write"])
        ws = f.write_files([{"path": "/work/m/1.txt", "data": "1"}, WriteEntry(path="/work/m/2.txt", data=b"2")])
        check("write_files (dict + WriteEntry)", [x.name for x in ws] == ["1.txt", "2.txt"],
              covers=["Filesystem.write_files"])
        ls = {x.name: x for x in f.list("/opt")}
        ok = (ls["app"].type == e2b.FileType.DIR and ls["keep-link"].type == e2b.FileType.SYMLINK
              and ls["keep-link"].symlink_target == "/opt/app/keep.txt" and ls["owned.txt"].owner == "nobody"
              and ls["after"].size == 6 and ls["after"].permissions == "-rw-r--r--" and ls["after"].mode == 0o644
              and ls["after"].group == "root" and isinstance(ls["after"].modified_time, datetime))
        check("list -> EntryInfo (type/size/mode/perms/owner/group/mtime/symlink)", ok, covers=["Filesystem.list"])
        check("list depth=1 vs depth=2", not any(x.path == "/opt/app/sub/nested.txt" for x in f.list("/opt/app"))
              and any(x.path == "/opt/app/sub/nested.txt" for x in f.list("/opt/app", depth=2)))
        gi = f.get_info("/usr/local/bin/run-ok")
        check("get_info: COPY mode 0755", isinstance(gi, e2b.EntryInfo) and gi.permissions == "-rwxr-xr-x",
              gi.permissions, covers=["Filesystem.get_info"])
        check("exists (file / symlink / missing)", f.exists("/work/m/1.txt") and f.exists("/opt/keep-link")
              and not f.exists("/work/nope"), covers=["Filesystem.exists"])
        check("make_dir new -> True, existing -> False", f.make_dir("/work/d/e") is True and f.make_dir("/work/d/e")
              is False, covers=["Filesystem.make_dir"])
        mv = f.rename("/work/sub/a.txt", "/work/b.txt")
        check("rename -> EntryInfo of new path", mv.path == "/work/b.txt" and not f.exists("/work/sub/a.txt"),
              covers=["Filesystem.rename"])
        f.remove("/work/b.txt")
        f.remove("/work/d")
        check("remove file and dir", not f.exists("/work/b.txt") and not f.exists("/work/d"),
              covers=["Filesystem.remove"])
        for name, fn in [("read", lambda: f.read("/work/b.txt")), ("remove", lambda: f.remove("/work/b.txt")),
                         ("list", lambda: f.list("/work/nope")), ("get_info", lambda: f.get_info("/work/nope"))]:
            check(f"{name}(missing) -> FileNotFoundException",
                  isinstance(raises(e2b.FileNotFoundException, fn), e2b.FileNotFoundException))
        f.write("/work/nb.txt", "mine", user="nobody")
        check("write(user=) sets owner", f.get_info("/work/nb.txt").owner == "nobody")
        f.write("/root/private.txt", "root only")
        c.run("chmod 600 /root/private.txt")
        e = raises(e2b.CommandExitException, lambda: f.read("/root/private.txt", user="nobody"))
        check("read(user=) uses that user's permissions", isinstance(e, e2b.CommandExitException))

        # ---- git (official e2b helper over our commands)
        g = sbx.git
        g.configure_user("Test", "test@example.com")
        g.init("/work/repo", initial_branch="main")
        f.write("/work/repo/x.txt", "x")
        g.add("/work/repo")
        g.commit("/work/repo", "first")
        st = g.status("/work/repo")
        check("git: configure_user/init/add/commit/status", st.is_clean and st.current_branch == "main",
              covers=["Sandbox.git"])

        # ---- info, metrics, timeout
        info = sbx.get_info()
        ok = (isinstance(info, e2b.SandboxInfo) and info.metadata == {"suite": "full-py"} and info.template_id == FULL
              and info.state == e2b.SandboxState.RUNNING and info.cpu_count == 2)
        check("get_info -> SandboxInfo; cpu_count=8 at build ignored", ok, f"cpu={info.cpu_count} mem={info.memory_mb}",
              covers=["Sandbox.get_info", "Template.build"])
        check("Sandbox.get_info(sandbox_id)", Sandbox.get_info(sbx.sandbox_id).sandbox_id == sbx.sandbox_id,
              covers=["Sandbox.get_info"])
        m = sbx.get_metrics()
        check("get_metrics -> one live sample", len(m) == 1 and isinstance(m[0], e2b.SandboxMetrics)
              and m[0].mem_total > m[0].mem_used > 0 and m[0].disk_total > 0, covers=["Sandbox.get_metrics"])
        check("Sandbox.get_metrics(sandbox_id)", len(Sandbox.get_metrics(sbx.sandbox_id)) == 1,
              covers=["Sandbox.get_metrics"])
        end_before = sbx.get_info().end_at
        _, w1 = caught_warnings(lambda: sbx.set_timeout(600))
        _, w2 = caught_warnings(lambda: Sandbox.set_timeout(sbx.sandbox_id, 600))
        check("set_timeout warns and has no effect", len(w1) == 1 and len(w2) == 1
              and Sandbox.get_info(sbx.sandbox_id).end_at == end_before, covers=["Sandbox.set_timeout"])

        # ---- list, is_running, connect
        t_mid = datetime.now(timezone.utc)
        sbx2 = Sandbox.create(FULL, metadata={"suite": "full-py"})
        q = e2b.SandboxQuery(metadata={"suite": "full-py"})
        p = Sandbox.list(query=q, limit=1)
        first, second = p.next_items(), p.next_items()
        check("list(limit=1) pages, newest first", [i.sandbox_id for i in first + second] ==
              [sbx2.sandbox_id, sbx.sandbox_id] and p.has_next is False, covers=["Sandbox.list"])
        asc = Sandbox.list(query=q, order="asc").next_items()
        check("list(order='asc')", [i.sandbox_id for i in asc] == [sbx.sandbox_id, sbx2.sandbox_id])
        ids = lambda query: [i.sandbox_id for i in Sandbox.list(query=query).next_items()]  # noqa: E731
        check("list(query.started_after)", ids(e2b.SandboxQuery(metadata={"suite": "full-py"}, started_after=t_mid))
              == [sbx2.sandbox_id])
        check("list(query.template)", sbx.sandbox_id in ids(e2b.SandboxQuery(template=FULL))
              and sbx.sandbox_id not in ids(e2b.SandboxQuery(template="nope")))
        check("list(query.state=[PAUSED]) -> none", sbx.sandbox_id not in ids(
            e2b.SandboxQuery(state=[e2b.SandboxState.PAUSED])))
        check("is_running", sbx.is_running() is True, covers=["Sandbox.is_running"])
        again = Sandbox.connect(sbx.sandbox_id)
        check("Sandbox.connect(id) keeps files + envs", again.files.read("/work/m/1.txt") == "1"
              and again.commands.run("echo $GREETING").stdout == "hi there\n", covers=["Sandbox.connect"])
        check("sbx.connect()", sbx.connect() is sbx, covers=["Sandbox.connect"])

        # A session reaped by AgentCore (simulated: stopped behind the SDK's back).
        boto3.Session(region_name=cfg.region).client("bedrock-agentcore").stop_runtime_session(
            agentRuntimeArn=sbx2._arn, runtimeSessionId=sbx2.sandbox_id, qualifier="DEFAULT")
        check("reaped sandbox still listed until noticed", sbx2.sandbox_id in ids(q), covers=["Sandbox.list"])
        check("is_running probes the microVM -> False", sbx2.is_running() is False, covers=["Sandbox.is_running"])
        check("...and then drops out of list", sbx2.sandbox_id not in ids(q))
        e = raises(e2b.SandboxNotFoundException, lambda: Sandbox.connect(sbx2.sandbox_id))
        check("connect(reaped) -> SandboxNotFoundException", isinstance(e, e2b.SandboxNotFoundException))
        sbx2 = None

        interop(sbx)
    finally:
        check("Sandbox.kill(sandbox_id) -> True", Sandbox.kill(sbx.sandbox_id) is True, covers=["Sandbox.kill"])
        if sbx2:
            sbx2.kill()
    check("Sandbox.kill(again) -> False", Sandbox.kill(sbx.sandbox_id) is False, covers=["Sandbox.kill"])
    e = raises(e2b.SandboxNotFoundException, lambda: Sandbox.connect(sbx.sandbox_id))
    check("connect after kill -> SandboxNotFoundException", isinstance(e, e2b.SandboxNotFoundException))
    with Sandbox.create(FULL) as s3:
        sid = s3.sandbox_id
        check("context manager", s3.commands.run("echo in").stdout == "in\n")
    check("context manager exit -> killed (sbx.kill())", Sandbox.kill(sid) is False, covers=["Sandbox.kill"])


def interop(sbx):
    """Python and JS share templates and sandboxes (same S3 records and in-VM layout)."""
    if not os.path.exists(INTEROP) or not shutil.which("node"):
        print("SKIP  interop: build the JS package first (cd js && npm run build)")
        return
    sbx.files.write("/work/interop.txt", "from python")

    def node(*args):
        r = subprocess.run(["node", INTEROP, *args], capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise RuntimeError(r.stderr[-500:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    out = node("connect", sbx.sandbox_id, "/work/interop.txt")
    check("JS connects to a Python sandbox (files + envs)", out == {"read": "from python", "env": "hi there"}, str(out))
    check("Python reads what JS wrote", sbx.files.read("/work/from-js.txt") == "hello from js")
    made = node("create", FULL)
    other = Sandbox.connect(made["id"])
    check("Python connects to a JS sandbox on a Python-built template",
          other.files.read("/work/from-js.txt") == "made by js" and other.commands.run("echo $FROM").stdout == "js\n")
    other.kill()


# =============================================================================== main

if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else ""
    stages = {"offline": [offline], "build": [build], "run": [run], "all": [offline, build, run]}
    if stage not in stages:
        sys.exit(__doc__)
    for fn in stages[stage]:
        fn()
    if stage == "all":
        for name in ROWS:
            if name not in covered:
                check(f"table row has a test: {name}", False)
    print(f"\n{results['pass']} passed, {results['fail']} failed; "
          f"{len(covered)}/{len(ROWS)} table rows covered by this stage")
    sys.exit(1 if results["fail"] else 0)
