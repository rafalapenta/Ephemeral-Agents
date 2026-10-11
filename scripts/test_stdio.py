import os
import subprocess
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
py = sys.executable
env = dict(os.environ)
env["PYTHONPATH"] = str(ROOT)

p = subprocess.Popen(
    [py, "-m", "src.mcp_servers.semantic_router"],
    cwd=str(ROOT),
    env=env,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

req = b'{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}\n'
out, err = p.communicate(input=req, timeout=5)
print("STDOUT:", out.decode("utf-8", errors="replace"))
print("STDERR:", err.decode("utf-8", errors="replace"))
