"""Minimal AgentCore Runtime HTTP contract: GET /ping, POST /invocations on port 8080.

Shell access goes through InvokeAgentRuntimeCommand (IAM-authenticated by AgentCore), so
/invocations only reports environment info and echoes the payload.
"""
import json
import os
import platform
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STARTED = time.time()


def _ver(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception as exc:  # tool missing
        return f"unavailable: {exc}"


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ping":
            return self._json(200, {"status": "Healthy", "time_of_last_update": int(STARTED)})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/invocations":
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"raw": raw.decode(errors="replace")}
        self._json(200, {
            "echo": payload,
            "arch": platform.machine(),
            "tz": os.environ.get("TZ"),
            "git": _ver(["git", "--version"]),
            "session": self.headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"),
            "uptime_s": round(time.time() - STARTED, 1),
        })

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    # 0.0.0.0 inside the microVM is the Runtime contract; AgentCore fronts it with IAM auth.
    print("listening on :8080", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
