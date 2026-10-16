"""Cross-language helper, driven by js/test/full.ts. Prints one JSON line on stdout.

    python tests/interop.py connect <sandbox_id> <path>   read <path> and $GREETING, write /work/from-py.txt
    python tests/interop.py create <template>             create a sandbox (FROM=py), write /work/from-py.txt
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from e2b_agentcore import Sandbox  # noqa: E402

cmd, a = sys.argv[1], sys.argv[2]
if cmd == "connect":
    sbx = Sandbox.connect(a)
    out = {"read": sbx.files.read(sys.argv[3]), "env": sbx.commands.run("echo -n $GREETING").stdout}
    sbx.files.write("/work/from-py.txt", "hello from python")
elif cmd == "create":
    sbx = Sandbox.create(a, envs={"FROM": "py"})
    sbx.files.write("/work/from-py.txt", "made by python")
    out = {"id": sbx.sandbox_id}
else:
    sys.exit(__doc__)
print(json.dumps(out))
