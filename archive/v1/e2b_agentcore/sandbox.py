"""E2B-style `Sandbox` on an AgentCore Harness session.

    from e2b_agentcore import Sandbox
    sbx = Sandbox.create("my-sandbox", envs={"FOO": "1"})
    print(sbx.commands.run("git --version").stdout)
    sbx.files.write("/tmp/a.txt", "hi"); print(sbx.files.read("/tmp/a.txt"))
    sbx.kill()

One sandbox = one runtimeSessionId (one microVM). Commands go through
InvokeAgentRuntimeCommand, which runs as root and streams stdout/stderr + exit code.
"""
from __future__ import annotations

import base64
import shlex
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from botocore.exceptions import ClientError

from ._config import Config
from .template import TemplateException, find_harness, harness_name

MAX_CMD = 60000  # API limit is 64 KB per command


class SandboxException(Exception):
    pass


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    error: Optional[str] = None


class CommandExitException(SandboxException):
    def __init__(self, result: CommandResult):
        super().__init__(f"exit code {result.exit_code}: {result.stderr.strip()[-500:]}")
        self.stdout, self.stderr, self.exit_code = result.stdout, result.stderr, result.exit_code


@dataclass
class EntryInfo:
    name: str
    path: str
    type: str  # "file" | "dir"


@dataclass
class _Background:
    pid: int
    sandbox: "Sandbox"

    def kill(self):
        return self.sandbox.commands.kill(self.pid)


class Commands:
    def __init__(self, sbx: "Sandbox"):
        self._s = sbx

    def run(self, cmd: str, background: bool = False, envs: Optional[Dict[str, str]] = None,
            user: Optional[str] = None, cwd: Optional[str] = None,
            on_stdout: Optional[Callable[[str], None]] = None,
            on_stderr: Optional[Callable[[str], None]] = None,
            timeout: Optional[float] = 60, **_ignored):
        env = {**self._s._envs, **(envs or {})}
        script = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())
        if cwd:
            script += f"cd {shlex.quote(cwd)}\n"
        if background:
            script += f"nohup bash -c {shlex.quote(cmd)} >/tmp/.e2b-bg-$$.log 2>&1 & echo $!\n"
        else:
            script += cmd + "\n"
        if user and user != "root":
            script = f"su {shlex.quote(user)} -s /bin/bash -c {shlex.quote(script)}"
        res = self._s._exec(script, timeout or 3600, on_stdout, on_stderr)
        if background:
            if res.exit_code != 0:
                raise CommandExitException(res)
            return _Background(int(res.stdout.strip().splitlines()[-1]), self._s)
        if res.exit_code != 0:
            raise CommandExitException(res)
        return res

    def kill(self, pid: int) -> bool:
        return self._s._exec(f"kill {int(pid)}", 30).exit_code == 0

    def list(self) -> List[dict]:
        out = self._s._exec("ps -eo pid=,args=", 30).stdout
        return [{"pid": int(l.split(None, 1)[0]), "cmd": l.split(None, 1)[1]}
                for l in out.splitlines() if l.strip() and len(l.split(None, 1)) == 2]


class Files:
    def __init__(self, sbx: "Sandbox"):
        self._s = sbx

    def _ok(self, script: str, timeout=120) -> CommandResult:
        r = self._s._exec(script, timeout)
        if r.exit_code != 0:
            raise CommandExitException(r)
        return r

    def write(self, path: str, data, **_ignored):
        raw = data.encode() if isinstance(data, str) else (data.read() if hasattr(data, "read") else data)
        q = shlex.quote(path)
        self._ok(f"mkdir -p \"$(dirname {q})\" && : > {q}")
        b64 = base64.b64encode(raw).decode()
        step = MAX_CMD - 200
        for i in range(0, len(b64), step):  # chunk past the 64 KB command limit
            self._ok(f"echo {b64[i:i + step]} | base64 -d >> {q}")
        return EntryInfo(name=path.rsplit("/", 1)[-1], path=path, type="file")

    def write_files(self, files: List[dict]):
        return [self.write(f["path"], f["data"]) for f in files]

    def read(self, path: str, format: str = "text", **_ignored):
        out = self._ok(f"base64 -w0 {shlex.quote(path)}").stdout.strip()
        raw = base64.b64decode(out)
        return raw if format in ("bytes", "bytearray") else raw.decode()

    def list(self, path: str, depth: int = 1, **_ignored) -> List[EntryInfo]:
        out = self._ok(f"find {shlex.quote(path)} -mindepth 1 -maxdepth {int(depth)} -printf '%y\\t%p\\n'").stdout
        res = []
        for line in out.splitlines():
            t, p = line.split("\t", 1)
            res.append(EntryInfo(name=p.rsplit("/", 1)[-1], path=p, type="dir" if t == "d" else "file"))
        return res

    def exists(self, path: str, **_ignored) -> bool:
        return self._s._exec(f"test -e {shlex.quote(path)}", 30).exit_code == 0

    def make_dir(self, path: str, **_ignored) -> bool:
        existed = self.exists(path)
        self._ok(f"mkdir -p {shlex.quote(path)}")
        return not existed

    def remove(self, path: str, **_ignored) -> None:
        self._ok(f"rm -rf {shlex.quote(path)}")

    def rename(self, old_path: str, new_path: str, **_ignored) -> EntryInfo:
        self._ok(f"mv {shlex.quote(old_path)} {shlex.quote(new_path)}")
        return EntryInfo(name=new_path.rsplit("/", 1)[-1], path=new_path, type="file")


class Sandbox:
    def __init__(self, cfg: Config, harness: dict, session_id: str, envs: Dict[str, str]):
        self._cfg, self._h, self._session, self._envs = cfg, harness, session_id, envs
        self._data = cfg.client("bedrock-agentcore")
        self.commands, self.files = Commands(self), Files(self)

    @property
    def sandbox_id(self) -> str:
        # Encodes the template so Sandbox.connect(sandbox_id) can find the harness again.
        return f"{self._h['harnessName']}.{self._session}"

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def create(cls, template: str, timeout: Optional[int] = None, metadata: Optional[dict] = None,
               envs: Optional[Dict[str, str]] = None, config: Optional[Config] = None, **_ignored):
        cfg = config or Config()
        h = find_harness(cfg, harness_name(template))
        if not h:
            raise TemplateException(f"template {template!r} not found; run Template.build first")
        sbx = cls(cfg, h, "e2b-" + uuid.uuid4().hex, envs or {})
        sbx._exec("true", 60)  # provision the microVM now, like E2B does on create
        return sbx

    @classmethod
    def connect(cls, sandbox_id: str, config: Optional[Config] = None, **_ignored):
        cfg = config or Config()
        name, session = sandbox_id.split(".", 1)
        h = find_harness(cfg, name)
        if not h:
            raise SandboxException(f"no template for sandbox {sandbox_id}")
        return cls(cfg, h, session, {})

    def kill(self) -> bool:
        try:
            self._data.stop_runtime_session(agentRuntimeArn=self._h["arn"], runtimeSessionId=self._session,
                                            qualifier="DEFAULT")
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return False
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.kill()

    # ------------------------------------------------------------ transport

    def _exec(self, script: str, timeout: float, on_stdout=None, on_stderr=None) -> CommandResult:
        b64 = base64.b64encode(script.encode()).decode()
        cmd = f"/bin/sh -c 'echo {b64} | base64 -d > /tmp/.e2b-$$.sh && bash /tmp/.e2b-$$.sh; rc=$?; rm -f /tmp/.e2b-$$.sh; exit $rc'"
        if len(cmd) > MAX_CMD + 4000:
            raise SandboxException("command too long (64 KB limit); write it with files.write first")
        for attempt in range(10):
            try:
                resp = self._data.invoke_agent_runtime_command(
                    agentRuntimeArn=self._h["arn"], runtimeSessionId=self._session, qualifier="DEFAULT",
                    contentType="application/json", accept="application/vnd.amazon.eventstream",
                    body={"command": cmd, "timeout": int(timeout)})
                break
            except ClientError as exc:
                if exc.response["Error"]["Code"] in ("RetryableConflictException", "ThrottlingException") \
                        and attempt < 9:
                    time.sleep(5)
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
        return CommandResult("".join(out), "".join(err), -1 if code is None else int(code))
