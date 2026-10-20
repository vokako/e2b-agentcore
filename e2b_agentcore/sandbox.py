"""`Sandbox` for the AgentCore provider, with the same methods and return types as `e2b.Sandbox`.

    from e2b_agentcore import Sandbox
    with Sandbox.create("my-sandbox", envs={"FOO": "1"}, metadata={"user": "alice"}) as sbx:
        print(sbx.commands.run("git --version").stdout)
        sbx.files.write("/tmp/a.txt", "hi")
        h = sbx.commands.run("sleep 2; echo done", background=True)
        print(h.wait().stdout)

How the E2B model maps onto AgentCore
-------------------------------------
* One sandbox = one AgentCore Runtime session (one arm64 microVM) on the template's Harness.
  sandbox_id is used directly as the runtimeSessionId.
* Every command and file operation is one InvokeAgentRuntimeCommand call: a shell command
  run as root in the microVM, with stdout/stderr streamed back and an exit code at the end.
  Files are moved as base64 through such commands (no separate file API).
* Inside the microVM we keep a little state under /tmp/.e2b/:
      id     the sandbox_id, used to detect that a session was reaped (see connect)
      env    `export` lines for the sandbox-level envs passed to Sandbox.create
      p/     background processes: <tag>.sh / .pid / .out / .err / .rc
* Outside, s3://<bucket>/sandboxes/<sandbox_id>.json stores what AgentCore cannot: the
  template, E2B metadata and start time, so Sandbox.list / get_info / connect work.
  Envs are NOT written to S3 (they may hold secrets); they live only in the microVM.
* Data classes and exceptions are the official e2b ones (e2b.CommandResult, e2b.EntryInfo,
  e2b.SandboxInfo, e2b.CommandExitException, ...), so isinstance/except checks keep working.

Methods with no AgentCore equivalent (pause, snapshots, get_host, ...) exist and raise
NotSupportedException with the reason, so callers fail loudly instead of silently.
"""
from __future__ import annotations

import base64
import shlex
import time
import uuid
import warnings
from datetime import datetime, timezone
from typing import Callable, Dict, Iterator, List, Optional, Union

from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from e2b import (SandboxException, CommandExitException, CommandResult, EntryInfo, FileNotFoundException, FileType,
                 InvalidArgumentException, NotFoundException, ProcessInfo, SandboxInfo, SandboxMetrics,
                 SandboxNotFoundException, SandboxQuery, SandboxState, TimeoutException, WriteInfo)
from e2b import Git as _E2BGit

from ._config import Config
from ._errors import NotSupportedException, aws_errors, not_supported
from .template import load_template

# V2 runtimes reject an InvokeAgentRuntimeCommand timeout above 3600 s (V1 accepts any).
# _exec sends `limit + 30`, and `timeout -k 5` adds 5 s more, so one call can wait 3565 s.
V2_MAX_CALL_S = 3565
END_MARK = "@E2B-AGENTCORE-END"   # last line of SDK-internal replies: "<mark>:<line count>"
B64_LINE = 3000                   # base64 line width for reads (V1 drops lines over ~64 KB)
READ_CHUNK = 192 * 1024           # files.read fetches this many bytes per command
READ_RETRIES = 5
MAX_CMD = 65536                  # InvokeAgentRuntimeCommand limit on the command string
STATE = "/tmp/.e2b"              # in-VM state dir (see module docstring)
ENOENT = 44                      # exit code our scripts use for "path does not exist"
# find -printf fields -> EntryInfo: type size mode(octal) perms owner group mtime link path
STAT_FMT = r"%y\t%s\t%m\t%M\t%u\t%g\t%T@\t%l\t%p\n"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Either:
    """Like e2b's class_method_variant: `sbx.kill()` and `Sandbox.kill(sandbox_id)` both work.

    `missing` is what the class-level call returns for an unknown sandbox id instead of
    raising SandboxNotFoundException (E2B's Sandbox.kill(id) returns False)."""

    _RAISE = object()

    def __init__(self, fn, missing=_RAISE):
        self.fn, self.missing = fn, missing

    def __get__(self, obj, cls):
        if obj is not None:
            return lambda *a, **k: self.fn(obj, *a, **k)

        def on_class(sandbox_id, *a, **k):
            try:
                sbx = cls._from_registry(sandbox_id, k.get("config"))
            except SandboxNotFoundException:
                if self.missing is _Either._RAISE:
                    raise
                return self.missing
            return self.fn(sbx, *a, **k)
        return on_class


# ============================================================================ commands

class CommandHandle:
    """Same surface as e2b.CommandHandle for a background command: pid, wait, kill, iterate.

    Output is read back by polling /tmp/.e2b/p/<tag>.out/.err (about once a second), so
    on_stdout/on_stderr callbacks arrive in chunks rather than line by line.
    """

    def __init__(self, sbx: "Sandbox", pid: int, tag: str):
        self._s, self._pid, self._tag = sbx, pid, tag
        self._out_off = self._err_off = 0
        self._stdout, self._stderr = [], []
        self._result: Optional[CommandResult] = None

    @property
    def pid(self) -> int:
        return self._pid

    def _poll_once(self):
        # Sections are separated by marker lines; base64 is wrapped because V1 drops output
        # lines longer than ~64 KB. Sent through _exec_ordered: the reply is verified, and a
        # lost or reordered reply just means "poll again" (offsets only move on success).
        d = f"{STATE}/p/{self._tag}"
        text = self._s._exec_ordered(
            f"echo @OUT; tail -c +{self._out_off + 1} {d}.out 2>/dev/null | base64\n"
            f"echo @ERR; tail -c +{self._err_off + 1} {d}.err 2>/dev/null | base64\n"
            f"echo @STATE; if [ -f {d}.rc ]; then cat {d}.rc; elif kill -0 {self._pid} 2>/dev/null; then echo run; "
            f"else echo gone; fi", 60)
        try:
            _, rest = text.split("@OUT\n", 1)
            out_b64, rest = rest.split("@ERR\n", 1)
            err_b64, state = rest.split("@STATE\n", 1)
        except (AttributeError, ValueError):
            return "", "", "run"   # reply lost or malformed: poll again
        out = base64.b64decode(out_b64).decode(errors="replace")
        err = base64.b64decode(err_b64).decode(errors="replace")
        self._out_off += len(base64.b64decode(out_b64))
        self._err_off += len(base64.b64decode(err_b64))
        return out, err, state.strip()

    def __iter__(self) -> Iterator[tuple]:
        """Yields (stdout, stderr, pty) chunks like e2b.CommandHandle; pty is always None."""
        while self._result is None:
            out, err, state = self._poll_once()
            self._stdout.append(out)
            self._stderr.append(err)
            if out or err:
                yield (out or None, err or None, None)
            if state == "run":
                time.sleep(1)
                continue
            code = int(state) if state.lstrip("-").isdigit() else -1   # "gone" = killed
            self._result = CommandResult(stderr="".join(self._stderr), stdout="".join(self._stdout),
                                         exit_code=code, error=None if code == 0 else "process exited")

    def wait(self, on_pty=None, on_stdout: Optional[Callable[[str], None]] = None,
             on_stderr: Optional[Callable[[str], None]] = None) -> CommandResult:
        """Block until the process exits. Raises e2b.CommandExitException on a non-zero exit."""
        for out, err, _ in self:
            out and on_stdout and on_stdout(out)
            err and on_stderr and on_stderr(err)
        if self._result.exit_code != 0:
            raise CommandExitException(stderr=self._result.stderr, stdout=self._result.stdout,
                                       exit_code=self._result.exit_code, error=self._result.error)
        return self._result

    def kill(self) -> bool:
        return self._s.commands.kill(self._pid)

    def disconnect(self) -> None:
        """Stop following output; the process keeps running (reattach with commands.connect)."""

    send_stdin = not_supported("CommandHandle.send_stdin", "InvokeAgentRuntimeCommand has no stdin channel")
    close_stdin = not_supported("CommandHandle.close_stdin", "InvokeAgentRuntimeCommand has no stdin channel")


class Commands:
    """sbx.commands: run / list / kill / connect, same signatures as e2b's Commands."""

    def __init__(self, sbx: "Sandbox"):
        self._s = sbx

    def run(self, cmd: str, background: Optional[bool] = None, envs: Optional[Dict[str, str]] = None,
            user: Optional[str] = None, cwd: Optional[str] = None,
            on_stdout: Optional[Callable[[str], None]] = None,
            on_stderr: Optional[Callable[[str], None]] = None,
            stdin: Optional[bool] = None, timeout: Optional[float] = 60,
            request_timeout: Optional[float] = None) -> Union[CommandResult, CommandHandle]:
        """Run `cmd` with bash.

        Foreground: returns e2b.CommandResult, raises e2b.CommandExitException on non-zero exit
        and e2b.TimeoutException after `timeout` seconds (0 = no limit, as in E2B).
        background=True: returns a CommandHandle immediately.
        envs/cwd apply to this command only; sandbox envs from Sandbox.create always apply.
        cwd defaults to the template's WORKDIR. user defaults to root (E2B defaults to "user").
        """
        if stdin:
            raise NotSupportedException("stdin=True is not supported on AgentCore: no stdin channel")
        body = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in (envs or {}).items())
        wd = cwd or self._s._workdir
        if wd:
            body += f"cd {shlex.quote(wd)} || exit 1\n"
        body += cmd + "\n"
        if user and user != "root":
            body = f"exec runuser -u {shlex.quote(user)} -- bash -c {shlex.quote(body)}\n"

        limit = timeout if timeout else self._s._cfg.max_lifetime_s
        if not background and self._s._v2 and limit > V2_MAX_CALL_S:
            # Too long for one V2 call: run it as a background command and follow its output,
            # so timeout (and timeout=0 = no limit) behave exactly as on V1.
            wrapped = f"timeout -k 5 {int(timeout)} bash -c {shlex.quote(body)}\n" if timeout else body
            start = time.time()
            try:
                return self._spawn(wrapped, cmd).wait(on_stdout=on_stdout, on_stderr=on_stderr)
            except CommandExitException as exc:
                if exc.exit_code in (124, 137) and timeout and time.time() - start >= timeout - 1:
                    raise TimeoutException(f"command timed out after {timeout}s: {cmd[:200]}") from None
                raise

        if background:
            return self._spawn(body, cmd)

        start = time.time()
        res = self._s._exec(body, limit, on_stdout, on_stderr)
        if res.exit_code in (124, 137) and timeout and time.time() - start >= timeout - 1:
            raise TimeoutException(f"command timed out after {timeout}s: {cmd[:200]}")
        if res.exit_code != 0:
            raise CommandExitException(stderr=res.stderr, stdout=res.stdout, exit_code=res.exit_code,
                                       error=res.error)
        return res

    def _spawn(self, body: str, cmd: str) -> CommandHandle:
        """Start `body` detached in the microVM; output and exit code go to files we poll."""
        tag = uuid.uuid4().hex[:12]
        d = f"{STATE}/p/{tag}"
        spawn = (f"mkdir -p {STATE}/p\n"
                 f"echo {base64.b64encode(body.encode()).decode()} | base64 -d > {d}.sh\n"
                 f"echo {shlex.quote(cmd)} > {d}.cmd\n"
                 # setsid: own process group, so kill() can take children down too.
                 f"setsid nohup bash -c '[ -f {STATE}/env ] && . {STATE}/env; bash {d}.sh; echo $? > {d}.rc' "
                 f"> {d}.out 2> {d}.err < /dev/null &\n"
                 f"echo $! | tee {d}.pid\n")
        r = self._s._exec(spawn, 60, raw=True)
        if r.exit_code != 0:
            raise CommandExitException(stderr=r.stderr, stdout=r.stdout, exit_code=r.exit_code, error="spawn failed")
        return CommandHandle(self._s, int(r.stdout.strip().splitlines()[-1]), tag)

    def list(self, request_timeout: Optional[float] = None) -> List[ProcessInfo]:
        """Running background commands started through this SDK (like E2B: only envd-started ones)."""
        out = self._s._exec(
            f"for f in {STATE}/p/*.pid; do [ -e \"$f\" ] || continue; t=$(basename $f .pid); p=$(cat $f); "
            f"[ -f {STATE}/p/$t.rc ] && continue; kill -0 $p 2>/dev/null || continue; "
            f"printf '%s\\t%s\\t%s\\t%s\\n' \"$p\" \"$t\" \"$(readlink /proc/$p/cwd)\" \"$(cat {STATE}/p/$t.cmd)\"; done",
            30, raw=True).stdout
        procs = []
        for line in out.splitlines():
            pid, tag, cwd, cmd = (line.split("\t", 3) + ["", "", "", ""])[:4]
            procs.append(ProcessInfo(pid=int(pid), tag=tag, cmd="/bin/bash", args=["-l", "-c", cmd], envs={}, cwd=cwd))
        return procs

    def kill(self, pid: int, request_timeout: Optional[float] = None) -> bool:
        """SIGKILL the process group (falls back to the single pid). True if it existed."""
        p = int(pid)
        return self._s._exec(f"kill -0 {p} 2>/dev/null || exit 1; kill -KILL -- -{p} 2>/dev/null || kill -KILL {p}",
                             30, raw=True).exit_code == 0

    def connect(self, pid: int, timeout: Optional[float] = 60, request_timeout: Optional[float] = None) -> CommandHandle:
        """Reattach to a background command started with run(background=True)."""
        r = self._s._exec(f"grep -lx {int(pid)} {STATE}/p/*.pid 2>/dev/null | head -1", 30, raw=True)
        path = r.stdout.strip()
        if not path:
            raise NotFoundException(f"no background command with pid {pid}")
        return CommandHandle(self._s, int(pid), path.rsplit("/", 1)[-1][:-4])

    send_stdin = not_supported("commands.send_stdin", "InvokeAgentRuntimeCommand has no stdin channel")
    close_stdin = not_supported("commands.close_stdin", "InvokeAgentRuntimeCommand has no stdin channel")


# ============================================================================ files

class Files:
    """sbx.files: same methods and return types as e2b's Filesystem. Needs GNU find + base64."""

    def __init__(self, sbx: "Sandbox"):
        self._s = sbx

    def _run(self, script: str, path: str, timeout: float = 120) -> CommandResult:
        r = self._s._exec(script, timeout, raw=True)
        if r.exit_code == ENOENT:
            raise FileNotFoundException(f"path '{path}' does not exist")
        if r.exit_code != 0:
            raise CommandExitException(stderr=r.stderr, stdout=r.stdout, exit_code=r.exit_code, error=r.error)
        return r

    @staticmethod
    def _must_exist(q: str) -> str:
        return f"[ -e {q} ] || [ -L {q} ] || exit {ENOENT}\n"

    def _as_user(self, script: str, user: Optional[str]) -> str:
        if user and user != "root":
            return f"runuser -u {shlex.quote(user)} -- bash -c {shlex.quote(script)}"
        return script

    def read(self, path: str, format: str = "text", user: Optional[str] = None,
             request_timeout: Optional[float] = None, gzip: bool = False,
             stream_idle_timeout: Optional[float] = None):
        """format="text" -> str, "bytes" -> bytearray, "stream" -> iterator of bytes (as E2B)."""
        q = shlex.quote(path)
        size = int(self._run(self._must_exist(q) + self._as_user(f"stat -L -c %s {q}", user), path).stdout.split()[-1])
        raw = bytearray()
        while len(raw) < size:
            raw += self._read_chunk(q, len(raw), min(READ_CHUNK, size - len(raw)), user, path)
        if format == "bytes":
            return bytearray(raw)
        if format == "stream":
            return iter([raw])
        return raw.decode()

    def _read_chunk(self, q: str, off: int, n: int, user: Optional[str], path: str) -> bytes:
        """`n` bytes at `off`, verified (see Sandbox._exec_ordered); refetched if incomplete."""
        script = self._as_user(f"tail -c +{off + 1} {q} | head -c {n} | base64 -w {B64_LINE}", user)
        for _ in range(READ_RETRIES):
            text = self._s._exec_ordered(script, 120)
            if text is not None:
                data = base64.b64decode(text)
                if len(data) == n:
                    return data
        raise SandboxException(f"files.read({path!r}): output kept arriving incomplete at offset {off}")

    def write(self, path: str, data: Union[str, bytes, "IO"], user: Optional[str] = None,
              request_timeout: Optional[float] = None, gzip: bool = False,
              use_octet_stream: Optional[bool] = None, metadata: Optional[Dict[str, str]] = None) -> WriteInfo:
        """Create or overwrite a file (parent dirs are created). Large files are sent in ~48 KB
        base64 chunks, one command each, so expect roughly 100 KB/s."""
        raw = data.encode() if isinstance(data, str) else (data.read() if hasattr(data, "read") else bytes(data))
        q = shlex.quote(path)
        tmp = shlex.quote(f"{path}.e2b-part")
        self._run(f"mkdir -p \"$(dirname {q})\" && : > {tmp}", path)
        b64 = base64.b64encode(raw).decode()
        # _exec base64-encodes the whole script again (x4/3), so a chunk must stay under
        # ~3/4 of the 64 KB command limit: 45 000 chars -> ~60 KB on the wire.
        step = 45000
        for i in range(0, len(b64), step):
            self._run(f"echo {b64[i:i + step]} | base64 -d >> {tmp}", path)
        owner = f" && chown {shlex.quote(user)} {q}" if user and user != "root" else ""
        self._run(f"mv -f {tmp} {q}{owner}", path)   # atomic replace: readers never see half a file
        return WriteInfo(name=path.rsplit("/", 1)[-1], type=FileType.FILE, path=path, metadata=metadata)

    def write_files(self, files: List, user: Optional[str] = None, request_timeout: Optional[float] = None,
                    **_ignored) -> List[WriteInfo]:
        """files: list of e2b.WriteEntry or {"path": ..., "data": ...} dicts."""
        return [self.write(f["path"] if isinstance(f, dict) else f.path,
                           f["data"] if isinstance(f, dict) else f.data, user=user) for f in files]

    def _entries(self, out: str) -> List[EntryInfo]:
        res = []
        for line in out.splitlines():
            if not line:
                continue
            t, size, mode, perms, owner, group, mtime, link, p = line.split("\t", 8)
            ftype = {"d": FileType.DIR, "l": FileType.SYMLINK}.get(t, FileType.FILE)
            res.append(EntryInfo(name=p.rstrip("/").rsplit("/", 1)[-1] or "/", type=ftype, path=p, size=int(size),
                                 mode=int(mode, 8), permissions=perms, owner=owner, group=group,
                                 modified_time=datetime.fromtimestamp(float(mtime), timezone.utc),
                                 symlink_target=link or None))
        return res

    def list(self, path: str, depth: Optional[int] = 1, user: Optional[str] = None,
             request_timeout: Optional[float] = None) -> List[EntryInfo]:
        q = shlex.quote(path)
        out = self._run(self._must_exist(q) + f"find {q} -mindepth 1 -maxdepth {int(depth or 1)} -printf '{STAT_FMT}'",
                        path).stdout
        return self._entries(out)

    def get_info(self, path: str, user: Optional[str] = None, request_timeout: Optional[float] = None) -> EntryInfo:
        q = shlex.quote(path)
        return self._entries(self._run(self._must_exist(q) + f"find {q} -maxdepth 0 -printf '{STAT_FMT}'",
                                       path).stdout)[0]

    def exists(self, path: str, user: Optional[str] = None, request_timeout: Optional[float] = None) -> bool:
        q = shlex.quote(path)
        return self._s._exec(f"[ -e {q} ] || [ -L {q} ]", 30, raw=True).exit_code == 0

    def make_dir(self, path: str, user: Optional[str] = None, request_timeout: Optional[float] = None) -> bool:
        """True if created, False if it already existed (same as E2B)."""
        q = shlex.quote(path)
        r = self._s._exec(f"[ -d {q} ] && exit 3; mkdir -p {q}", 30, raw=True)
        if r.exit_code not in (0, 3):
            raise CommandExitException(stderr=r.stderr, stdout=r.stdout, exit_code=r.exit_code, error=r.error)
        return r.exit_code == 0

    def remove(self, path: str, user: Optional[str] = None, request_timeout: Optional[float] = None) -> None:
        q = shlex.quote(path)
        self._run(self._must_exist(q) + f"rm -rf {q}", path)

    def rename(self, old_path: str, new_path: str, user: Optional[str] = None,
               request_timeout: Optional[float] = None) -> EntryInfo:
        o, n = shlex.quote(old_path), shlex.quote(new_path)
        self._run(self._must_exist(o) + f"mv {o} {n}", old_path)
        return self.get_info(new_path)

    watch_dir = not_supported("files.watch_dir", "no file-event stream from the microVM; poll files.list instead")


# ============================================================================ sandbox

class Sandbox:
    """Drop-in for e2b.Sandbox (sync API). See the module docstring for the mapping."""

    # Same class constants as e2b.Sandbox. default_sandbox_timeout is informational only:
    # the real limits are Config.idle_timeout_s / max_lifetime_s on the Harness.
    default_template = "base"
    default_sandbox_timeout = 300
    default_mcp_template = "mcp-gateway"
    mcp_port = 50005

    def __init__(self, cfg: Config, record: dict):
        self._cfg, self._rec = cfg, record
        # The ARN every command is sent to: a Harness ARN (V1) or a Runtime ARN (V2).
        # Older records only have "harness_arn".
        self._arn = record.get("agent_runtime_arn") or record["harness_arn"]
        self._workdir = record.get("workdir")
        self._v2 = record.get("platform_version") == "V2"
        # read_timeout must outlast the longest command: botocore's default is 60 s, and a
        # command that prints nothing for longer would otherwise fail with ReadTimeoutError.
        self._data = cfg.session.client("bedrock-agentcore", config=BotoConfig(
            read_timeout=cfg.max_lifetime_s + 120, retries={"mode": "standard", "max_attempts": 3}))
        self._commands, self._files = Commands(self), Files(self)
        self._git = _E2BGit(self._commands)   # official e2b Git helper, runs over our commands

    # --------------------------------------------------------------- properties (as e2b)

    @property
    def sandbox_id(self) -> str:
        return self._rec["sandbox_id"]

    @property
    def sandbox_domain(self) -> Optional[str]:
        return None

    @property
    def commands(self) -> Commands:
        return self._commands

    @property
    def files(self) -> Files:
        return self._files

    @property
    def git(self):
        return self._git

    @property
    def pty(self):
        raise NotSupportedException(
            "sbx.pty is not supported by this provider yet. AgentCore has an interactive shell over "
            "WebSocket: bedrock_agentcore.runtime AgentCoreRuntimeClient.open_shell(runtime_arn, session_id)")

    # --------------------------------------------------------------- lifecycle

    @classmethod
    @aws_errors
    def create(cls, template: Optional[str] = None, timeout: Optional[int] = None,
               metadata: Optional[Dict[str, str]] = None, envs: Optional[Dict[str, str]] = None,
               secure: Optional[bool] = None, allow_internet_access: Optional[bool] = None,
               mcp=None, network=None, lifecycle=None, volume_mounts=None,
               config: Optional[Config] = None, **_e2b_opts) -> "Sandbox":
        """Start a microVM from a template built with Template.build.

        Like E2B: the default template is "base"; envs apply to every command; the template's
        start command (set_start_cmd) is started and its ready command polled before returning.
        Not per-sandbox on AgentCore (accepted, warned, no effect): timeout, network,
        allow_internet_access, lifecycle. Set those on the Harness via Config instead.
        """
        cfg = config or Config()
        alias = template or "base"
        tpl = load_template(cfg, alias)
        if not tpl:
            raise NotFoundException(f"template '{alias}' not found; build it with e2b_agentcore.Template.build")
        for name, val in (("timeout", timeout), ("network", network), ("lifecycle", lifecycle),
                          ("allow_internet_access", allow_internet_access), ("mcp", mcp),
                          ("volume_mounts", volume_mounts)):
            if val is not None:
                warnings.warn(f"Sandbox.create({name}=...) has no per-sandbox effect on AgentCore", stacklevel=2)

        started = time.time()
        rec = {"sandbox_id": "sbx" + uuid.uuid4().hex,          # 35 chars; runtimeSessionId needs >= 33
               "template": alias, "agent_runtime_arn": tpl.get("agent_runtime_arn") or tpl["harness_arn"],
               "harness_arn": tpl.get("agent_runtime_arn") or tpl["harness_arn"],   # read by older SDKs
               "platform_version": tpl.get("platform_version", "V1"), "workdir": tpl.get("workdir"),
               "metadata": metadata or {}, "started_at": started, "end_at": started + cfg.max_lifetime_s}
        sbx = cls(cfg, rec)

        # First command provisions the microVM (cold start ~4 s). It also writes the in-VM
        # state (id marker, sandbox envs) and reports the VM size for get_info().
        env_lines = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in (envs or {}).items())
        r = sbx._exec(f"mkdir -p {STATE}/p && chmod 700 {STATE}\n"
                      f"echo {rec['sandbox_id']} > {STATE}/id\n"
                      f"echo {base64.b64encode(env_lines.encode()).decode()} | base64 -d > {STATE}/env\n"
                      f"chmod 600 {STATE}/env\n"
                      f"nproc; awk '/MemTotal/ {{print int($2/1024)}}' /proc/meminfo", 120, raw=True)
        nums = [int(x) for x in r.stdout.split() if x.isdigit()] + [0, 0]
        rec["cpu_count"], rec["memory_mb"] = nums[0], nums[1]

        # E2B runs set_start_cmd when the sandbox starts and waits for set_ready_cmd.
        if tpl.get("start_cmd"):
            h = sbx.commands.run(tpl["start_cmd"], background=True)
            rec["start_pid"] = h.pid
            ready = tpl.get("ready_cmd")
            deadline = time.time() + 120
            while ready and sbx._exec(ready, 30).exit_code != 0:
                if time.time() > deadline:
                    sbx.kill()
                    raise TimeoutException(f"ready command did not succeed within 120 s: {ready}")
                time.sleep(1)
        cfg.put_json(f"sandboxes/{rec['sandbox_id']}.json", rec)
        return sbx

    @classmethod
    def _from_registry(cls, sandbox_id: str, config: Optional[Config] = None) -> "Sandbox":
        cfg = config or Config()
        rec = cfg.get_json(f"sandboxes/{sandbox_id}.json")
        if not rec:
            raise SandboxNotFoundException(f"sandbox {sandbox_id} not found")
        return cls(cfg, rec)

    @aws_errors
    def _connect(self, timeout: Optional[int] = None, **_ignored) -> "Sandbox":
        # Invoking a reaped session id silently boots a NEW empty microVM on AgentCore. E2B
        # would say "not found" instead, so check the id marker and undo the accidental boot.
        r = self._exec(f"cat {STATE}/id 2>/dev/null", 60, raw=True)
        if r.stdout.strip() != self.sandbox_id:
            self._stop()
            self._cfg.delete_json(f"sandboxes/{self.sandbox_id}.json")
            raise SandboxNotFoundException(f"sandbox {self.sandbox_id} is no longer running "
                                           f"(idle timeout {self._cfg.idle_timeout_s}s or max lifetime reached)")
        return self

    connect = _Either(_connect)     # Sandbox.connect(sandbox_id) or sbx.connect()

    def _stop(self) -> bool:
        try:
            self._data.stop_runtime_session(agentRuntimeArn=self._arn, runtimeSessionId=self.sandbox_id,
                                            qualifier="DEFAULT")
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return False
            raise

    @aws_errors
    def _kill(self, **_ignored) -> bool:
        stopped = self._stop()
        self._cfg.delete_json(f"sandboxes/{self.sandbox_id}.json")
        return stopped

    kill = _Either(_kill, missing=False)   # sbx.kill() or Sandbox.kill(sandbox_id); unknown id -> False

    def _set_timeout(self, timeout: int, **_ignored) -> None:
        warnings.warn("set_timeout has no effect on AgentCore: idle timeout / max lifetime are per Harness "
                      f"(now {self._cfg.idle_timeout_s}s / {self._cfg.max_lifetime_s}s)", stacklevel=3)

    set_timeout = _Either(_set_timeout)

    def is_running(self, request_timeout: Optional[float] = None) -> bool:
        """Registry check, then the same id-marker probe as connect(). A reaped sandbox is
        cleaned up and reported as not running."""
        if time.time() > self._rec["end_at"] or not self._cfg.get_json(f"sandboxes/{self.sandbox_id}.json"):
            return False
        try:
            self._connect()
            return True
        except SandboxNotFoundException:
            return False

    def _get_info(self, **_ignored) -> SandboxInfo:
        r = self._rec
        return SandboxInfo(sandbox_id=r["sandbox_id"], sandbox_domain=None, template_id=r["template"],
                           name=r["template"], metadata=r.get("metadata", {}),
                           started_at=datetime.fromtimestamp(r["started_at"], timezone.utc),
                           end_at=datetime.fromtimestamp(r["end_at"], timezone.utc), state=SandboxState.RUNNING,
                           cpu_count=r.get("cpu_count", 0), memory_mb=r.get("memory_mb", 0), envd_version="agentcore")

    get_info = _Either(_get_info)

    def _get_metrics(self, start=None, end=None, **_ignored) -> List[SandboxMetrics]:
        """One live sample (E2B returns a time series; AgentCore keeps none per session)."""
        out = self._exec(
            "read -r _ a b c d e f g h _ < /proc/stat; t1=$((a+b+c+d+e+f+g+h)); i1=$((d+e)); sleep 0.5\n"
            "read -r _ a b c d e f g h _ < /proc/stat; t2=$((a+b+c+d+e+f+g+h)); i2=$((d+e))\n"
            "echo $(nproc) $(( (100*((t2-t1)-(i2-i1)))/((t2-t1)>0?(t2-t1):1) ))\n"
            "awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}/^Cached/{c=$2}END{print t*1024,(t-a)*1024,c*1024}' /proc/meminfo\n"
            "df -B1 --output=size,used / | tail -1", 30, raw=True).stdout.split()
        n = [int(x) for x in out] + [0] * 7
        return [SandboxMetrics(cpu_count=n[0], cpu_used_pct=float(n[1]), mem_total=n[2], mem_used=n[3],
                               mem_cache=n[4], disk_total=n[5], disk_used=n[6], timestamp=_now())]

    get_metrics = _Either(_get_metrics)

    @classmethod
    def list(cls, query: Optional[SandboxQuery] = None, limit: Optional[int] = None,
             next_token: Optional[str] = None, order=None, config: Optional[Config] = None,
             **_ignored) -> "SandboxPaginator":
        """Same as e2b.Sandbox.list: returns a paginator (has_next / next_items()).
        Source is the S3 registry, filtered by query.metadata / query.template / started_after.
        Sandboxes reaped by the idle timeout stay listed until connect/is_running notices."""
        return SandboxPaginator(config or Config(), query, limit or 100, next_token, order)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.kill()

    # --------------------------------------------------------------- E2B features with no AgentCore equivalent

    pause = not_supported("Sandbox.pause", "no memory snapshot; session storage (Preview) keeps only the filesystem")
    beta_pause = not_supported("Sandbox.beta_pause", "no memory snapshot")
    create_snapshot = not_supported("Sandbox.create_snapshot", "no VM snapshot API")
    fork = not_supported("Sandbox.fork", "no VM snapshot API")
    list_snapshots = not_supported("Sandbox.list_snapshots", "no VM snapshot API")
    delete_snapshot = classmethod(not_supported("Sandbox.delete_snapshot", "no VM snapshot API"))
    get_host = not_supported("Sandbox.get_host", "AgentCore exposes no inbound port of a session")
    upload_url = not_supported("Sandbox.upload_url", "no direct HTTP endpoint into the session; use files.write")
    download_url = not_supported("Sandbox.download_url", "no direct HTTP endpoint into the session; use files.read")
    update_network = not_supported("Sandbox.update_network", "network mode is set per Harness")
    get_mcp_url = not_supported("Sandbox.get_mcp_url", "use AgentCore Gateway for MCP")
    get_mcp_token = not_supported("Sandbox.get_mcp_token", "use AgentCore Gateway for MCP")

    # --------------------------------------------------------------- transport

    def _exec_ordered(self, script: str, timeout: float) -> Optional[str]:
        """Run an SDK-internal, read-only `script` and return its stdout verified, or None.

        AgentCore streams stdout one line per event, and big outputs are not delivered
        reliably: on V1 and V2 the last lines can be lost when the command exits right after
        printing, and on V2 lines can also arrive out of order. So every line is numbered in
        the microVM (awk) and followed by an end line carrying the count; here the lines are
        put back in order and the reply is accepted only if every line 1..count is present.
        Callers retry on None. User commands (commands.run) cannot be fixed this way, since
        their output is the user's own.
        """
        r = self._exec(f"{{ {script}\n}} | awk '{{print NR\":\"$0}} END{{print \"{END_MARK}:\"NR}}'",
                       timeout, raw=True)
        lines, total = {}, None
        for line in r.stdout.split("\n"):
            head, sep, body = line.partition(":")
            if not sep:
                continue
            if head == END_MARK and body.isdigit():
                total = int(body)
            elif head.isdigit():
                lines[int(head)] = body
        if total is None or len(lines) != total or any(k not in lines for k in range(1, total + 1)):
            return None
        return "".join(lines[k] + "\n" for k in range(1, total + 1))

    def _exec(self, script: str, timeout: float, on_stdout=None, on_stderr=None, raw: bool = False) -> CommandResult:
        """Run `script` with bash in the microVM; one InvokeAgentRuntimeCommand call.

        The script travels base64-encoded (no quoting issues), is written to a temp file and
        run under coreutils `timeout`. Sandbox envs are sourced first unless raw=True
        (internal helpers that don't need them).
        """
        pre = "" if raw else f"[ -f {STATE}/env ] && . {STATE}/env\n"
        b64 = base64.b64encode((pre + script).encode()).decode()
        t = max(1, int(timeout))
        if self._v2:   # one V2 call can't wait longer; Commands.run goes background past this
            t = min(t, V2_MAX_CALL_S)
        cmd = (f"/bin/sh -c 'f=$(mktemp); echo {b64} | base64 -d > $f; "
               f"timeout -k 5 {t} bash $f; rc=$?; rm -f $f; exit $rc'")
        if len(cmd) > MAX_CMD:
            raise InvalidArgumentException("command longer than the 64 KB AgentCore limit; "
                                           "write it to a file with files.write and run that")
        for attempt in range(10):
            try:
                resp = self._data.invoke_agent_runtime_command(
                    agentRuntimeArn=self._arn, runtimeSessionId=self.sandbox_id, qualifier="DEFAULT",
                    contentType="application/json", accept="application/vnd.amazon.eventstream",
                    body={"command": cmd, "timeout": t + 30})
                break
            except ClientError as exc:
                # RetryableConflict: the session is still starting or busy with a previous call.
                if exc.response["Error"]["Code"] in ("RetryableConflictException", "ThrottlingException") \
                        and attempt < 9:
                    time.sleep(3)
                    continue
                raise
        out, err, code = [], [], None
        for event in resp.get("stream", []):
            chunk = event.get("chunk") or {}
            delta = chunk.get("contentDelta") or {}
            if delta.get("stdout"):
                out.append(delta["stdout"])
                on_stdout and on_stdout(delta["stdout"])
            if delta.get("stderr"):
                err.append(delta["stderr"])
                on_stderr and on_stderr(delta["stderr"])
            if chunk.get("contentStop"):
                code = chunk["contentStop"].get("exitCode")
        code = -1 if code is None else int(code)
        return CommandResult(stderr="".join(err), stdout="".join(out), exit_code=code,
                             error=None if code == 0 else f"exit status {code}")


class SandboxPaginator:
    """Same shape as e2b.SandboxPaginator: `while p.has_next: for info in p.next_items(): ...`"""

    def __init__(self, cfg: Config, query: Optional[SandboxQuery], limit: int, next_token: Optional[str], order):
        self._cfg, self._q, self._limit, self._order = cfg, query, limit, order
        self._offset = int(next_token or 0)
        self._items: Optional[List[SandboxInfo]] = None
        self.has_next = True
        self.next_token: Optional[str] = next_token

    def _all(self) -> List[SandboxInfo]:
        if self._items is None:
            now, q, items = time.time(), self._q, []
            for rec in self._cfg.iter_json("sandboxes/"):
                if rec.get("end_at", 0) < now:
                    continue
                if q and q.metadata and any(rec.get("metadata", {}).get(k) != v for k, v in q.metadata.items()):
                    continue
                if q and getattr(q, "template", None) and rec.get("template") != q.template:
                    continue
                if q and getattr(q, "started_after", None) and \
                        datetime.fromtimestamp(rec["started_at"], timezone.utc) <= q.started_after:
                    continue
                if q and q.state and SandboxState.RUNNING not in q.state:
                    continue
                items.append(Sandbox(self._cfg, rec)._get_info())
            items.sort(key=lambda i: i.started_at, reverse=(self._order != "asc"))
            self._items = items
        return self._items

    def next_items(self) -> List[SandboxInfo]:
        items = self._all()
        page = items[self._offset:self._offset + self._limit]
        self._offset += len(page)
        self.has_next = self._offset < len(items)
        self.next_token = str(self._offset) if self.has_next else None
        return page
